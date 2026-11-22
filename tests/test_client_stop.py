"""Exact-client termination and managed failed-close recovery use isolated evidence."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SID = '12345678-1234-1234-1234-123456789abc'


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location('client_stop_test', ROOT / 'libexec/t_client_stop.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def owner(helper, monkeypatch, tmp_path):
    pane = dict(socket='/isolated/socket', session='$1', name='dev-repo-1', workspace=str(tmp_path), pane='%1', pid=20)
    process = dict(pid=21, birth=['seconds', 'micros'], parent=20, comm='/bin/codex', argv=['codex'], cwd=str(tmp_path))
    monkeypatch.setattr(helper, 'pane', lambda *a: pane)
    monkeypatch.setattr(helper, 'pane_client', lambda *a: process)
    monkeypatch.setattr(helper, 'process', lambda pid: process)
    monkeypatch.setattr(helper, 'cwd', lambda pid: str(tmp_path))
    token = helper.capture('dev-repo-1', str(tmp_path), '$1', '21', 'codex')
    return token, pane, process


def test_graceful_stop_signals_client_before_pane_is_removed(helper, owner, monkeypatch):
    token, pane, process = owner
    signals = []
    monkeypatch.setattr(helper.os, 'kill', lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(helper.ClientStopPending):
        helper.stop(token, timeout=0)
    assert signals == [(21, signal.SIGTERM)]
    assert token['owner'] == pane  # helper has no pane destruction operation


@pytest.mark.parametrize('change', ['birth', 'cwd', 'pane', 'service', 'second-client'])
def test_changed_or_ambiguous_owner_is_never_signalled(helper, owner, monkeypatch, change):
    token, pane, process = owner
    if change == 'birth':
        monkeypatch.setattr(helper, 'process', lambda pid: {**process, 'birth': ['new', 'birth']})
    elif change == 'cwd':
        monkeypatch.setattr(helper, 'cwd', lambda pid: '/different')
    elif change == 'pane':
        monkeypatch.setattr(helper, 'pane', lambda *a: {**pane, 'pane': '%2'})
    elif change == 'service':
        monkeypatch.setattr(helper, 'process', lambda pid: {**process, 'argv': ['codex', 'app-server']})
    else:
        monkeypatch.setattr(helper, 'pane_client', lambda *a: (_ for _ in ()).throw(ValueError('multiple clients')))
    signals = []
    monkeypatch.setattr(helper.os, 'kill', lambda *a: signals.append(a))
    with pytest.raises(ValueError):
        helper.stop(token, force=True, timeout=0)
    assert not signals


def test_natural_exit_without_tmux_is_success(helper, owner, monkeypatch):
    token, _, _ = owner
    monkeypatch.setattr(helper, 'process', lambda pid: None)
    monkeypatch.setattr(helper, 'pane', lambda *a: None)
    monkeypatch.setattr(helper.os, 'kill', lambda *a: pytest.fail('exited process signalled'))
    helper.stop(token)


def receipt(tmp_path, **updates):
    value = dict(verb='close', phase='failed', pending='stop', updated=time.time(),
                 arguments=['dev-repo-1', str(tmp_path), SID, 'codex'],
                 receipts={'inspect': '$1'}, error='stop failed: t close: agent has not exited; no release recorded')
    value.update(updates)
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


@pytest.mark.parametrize('invalid', [None, 'old', 'completed', 'released', 'other-sid', 'other-cwd', 'wrong-error', 'ambiguous', 'symlink', 'public'])
def test_legacy_failed_close_requires_exact_recent_private_receipt(helper, tmp_path, invalid):
    value = receipt(tmp_path)
    if invalid == 'old':
        receipt(tmp_path, updated=time.time() - 86401)
    elif invalid == 'completed':
        receipt(tmp_path, phase='complete')
    elif invalid == 'released':
        receipt(tmp_path, receipts={'inspect': '$1', 'released': True})
    elif invalid == 'other-sid':
        receipt(tmp_path, arguments=['dev-repo-1', str(tmp_path), 'other', 'codex'])
    elif invalid == 'other-cwd':
        receipt(tmp_path, arguments=['dev-repo-1', '/other', SID, 'codex'])
    elif invalid == 'wrong-error':
        receipt(tmp_path, error='unknown failure')
    elif invalid == 'ambiguous':
        second = tmp_path / 'second.json'; second.write_text(json.dumps(value)); second.chmod(0o600)
    elif invalid == 'symlink':
        (tmp_path / 'receipt.json').rename(tmp_path / 'data'); (tmp_path / 'receipt.json').symlink_to(tmp_path / 'data')
    elif invalid == 'public':
        (tmp_path / 'receipt.json').chmod(0o644)
    assert bool(helper.failed_close(str(tmp_path), SID, directory=tmp_path)) == (invalid is None)


@pytest.mark.parametrize('case', ['good', 'live-tmux', 'shared', 'other-cwd', 'live-terminal', 'changed'])
def test_stranded_requires_native_sole_standalone_writer_without_terminal(helper, tmp_path, monkeypatch, case):
    receipt(tmp_path)
    lock = tmp_path / (SID + '.lock'); lock.touch()
    monkeypatch.setattr(helper, 'missing_session', lambda r: case != 'live-tmux')
    monkeypatch.setitem(helper.OWNERSHIP, '_lock_dir', lambda home: str(tmp_path))
    monkeypatch.setitem(helper.OWNERSHIP, '_lock_held', lambda path: True)
    monkeypatch.setitem(helper.OWNERSHIP, '_open_writer_cwds', lambda *a: ({str(tmp_path)}, (21, 22) if case == 'shared' else (21,)))
    monkeypatch.setitem(helper.OWNERSHIP, '_standalone_writer_cwd', lambda *a: '/other' if case == 'other-cwd' else str(tmp_path))
    monkeypatch.setitem(helper.OWNERSHIP, '_writer_fds', lambda *a: [])
    process = dict(pid=21, parent=1, started=time.time() - 60, birth='native', comm='/bin/codex', argv=['codex', '--no-daemon'])
    count = iter([process, {**process, 'birth': 'changed'}]) if case == 'changed' else None
    monkeypatch.setattr(helper, 'process', lambda pid: next(count) if count else process)
    monkeypatch.setattr(helper, 'process_socket', lambda pid: '/isolated/socket')
    monkeypatch.setattr(helper, 'cwd', lambda pid: str(tmp_path))
    monkeypatch.setattr(helper, 'run', lambda *a, **k: subprocess.CompletedProcess([], 0, 'ttys001' if case == 'live-terminal' else '??', ''))
    assert bool(helper.stranded(str(tmp_path), SID, directory=tmp_path)) == (case == 'good')


def test_deleted_linux_executable_still_identifies_exact_client(helper, monkeypatch):
    monkeypatch.setattr(helper.sys, 'platform', 'linux')
    assert helper.client({'comm': '/bin/codex (deleted)', 'argv': ['codex', '--no-daemon']}) == 'codex'
    assert helper.client({'comm': '/bin/codex (deleted)', 'argv': ['codex', 'app-server']}) is None


def test_missing_tmux_never_treats_permission_denied_as_absent(helper, monkeypatch):
    monkeypatch.setattr(helper, 'run', lambda *a, **k: subprocess.CompletedProcess([], 1, '', 'error connecting to /socket (Permission denied)'))
    assert not helper.missing_session({'socket': '/socket', 'immutable': '$1', 'session': 'dev-repo-1'})
