"""Allocator provenance for the explicit saved-Codex fresh-worktree recovery."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SID = '12345678-1234-1234-1234-123456789abc'
OTHER = '22345678-1234-1234-1234-123456789abc'
NEW = '32345678-1234-1234-1234-123456789abc'
TOKEN = 'a' * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in ('HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'CODEX_HOME'):
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.setenv(name, str(folder))
    spec = importlib.util.spec_from_file_location('fresh_resume_test', ROOT / 'libexec/t_lifecycle.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    home = Path(os.environ['CODEX_HOME'])
    locks = home / 'thread-writer-locks'
    locks.mkdir()
    rollout = home / 'selected.jsonl'
    rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(
        id=SID, cwd=str(tmp_path / 'original'), cli_version='0.160.0'))) + '\n')
    workspace = tmp_path / 'fresh'
    marker = tmp_path / '.fresh.history-reservation'
    marker.write_text(TOKEN)
    marker.chmod(0o600)
    state = SimpleNamespace(engine=engine, home=home, locks=locks, rollout=rollout,
                            workspace=workspace, marker=marker, pid=4321, birth='Fri Nov 20 10:00:00 2026',
                            bad_probe=False, handles=[])
    def run(argv, **kwargs):
        if state.bad_probe:
            return SimpleNamespace(returncode=1, stdout='', stderr='unavailable')
        if argv[0] == 'lsof' and '-t' in argv:
            return SimpleNamespace(returncode=0, stdout=str(state.pid) + '\n', stderr='')
        if argv[0] == 'ps':
            return SimpleNamespace(returncode=0, stdout=state.birth + ' codex app-server\n', stderr='')
        raise AssertionError(argv)
    engine.run = run
    monkeypatch.setitem(engine.OWNERSHIP['_other_cwd_owner'].__globals__, '_WRITER_SETTLE_SECONDS', 0)
    monkeypatch.setitem(engine.OWNERSHIP['_other_cwd_owner'].__globals__, '_open_writer_cwds',
                        lambda path, sid, run: (set(), (state.pid,)))
    def hold(sid):
        handle = (locks / (sid + '.lock')).open('w')
        fcntl.flock(handle, fcntl.LOCK_EX)
        state.handles.append(handle)
        return handle
    state.hold = hold
    state.hold(OTHER)
    yield state
    for handle in state.handles:
        handle.close()


def allocate(state):
    state.engine.begin_fresh_resume(SID, str(state.rollout), str(state.workspace), token=TOKEN)
    state.engine.finish_fresh_resume(TOKEN, SID, str(state.workspace))


def preflight(state, **kwargs):
    state.engine.native_preflight('codex', SID, str(state.workspace),
                                 rollout=str(state.rollout), fresh_resume=TOKEN, **kwargs)


def test_fresh_proof_allows_only_preallocation_sibling_and_cleans_authority(setup):
    s = setup
    assert s.engine.verify_fresh_resume_source(SID, str(s.rollout)) == {OTHER}
    allocate(s)
    record = s.engine._fresh_resume_path(TOKEN)
    value = json.loads(record.read_text())
    assert record.stat().st_mode & 0o777 == 0o600
    assert record.parent.stat().st_mode & 0o777 == 0o700
    assert value['identity'] == [s.workspace.stat().st_dev, s.workspace.stat().st_ino]
    assert value['writers'][OTHER]['holders'][0][0] == s.pid
    preflight(s)
    s.engine.discard_fresh_resume(TOKEN)
    s.engine.discard_fresh_resume(TOKEN)
    with pytest.raises(FileNotFoundError):
        preflight(s)


@pytest.mark.parametrize('change', ['new-unknown', 'pid', 'birth', 'inode', 'probe', 'selected',
                                    'directory', 'symlink', 'reservation', 'state-dir', 'lock-dir',
                                    'source-version', 'known-conflict', 'corrupt-index'])
def test_fresh_proof_rejects_changed_ownership_or_allocator_evidence(setup, change):
    s = setup
    allocate(s)
    if change == 'new-unknown':
        s.hold(NEW)
    elif change == 'pid':
        s.pid += 1
    elif change == 'birth':
        s.birth = 'Fri Nov 20 10:00:01 2026'
    elif change == 'inode':
        (s.locks / (OTHER + '.lock')).rename(s.locks / 'old')
        s.hold(OTHER)
    elif change == 'probe':
        s.bad_probe = True
    elif change == 'selected':
        s.hold(SID)
    elif change in ('directory', 'symlink'):
        old = s.workspace.with_name('old')
        s.workspace.rename(old)
        if change == 'directory':
            s.workspace.mkdir()
        else:
            s.workspace.symlink_to(old, target_is_directory=True)
    elif change == 'reservation':
        s.marker.write_text('b' * 32)
    elif change == 'state-dir':
        s.home.rename(s.home.with_name('old-home'))
        s.home.mkdir()
    elif change == 'lock-dir':
        s.locks.rename(s.locks.with_name('old-locks'))
        s.locks.mkdir()
    elif change == 'source-version':
        s.rollout.write_text(s.rollout.read_text().replace('0.160.0', '0.158.0'))
    elif change == 'known-conflict':
        with sqlite3.connect(s.home / 'state_5.sqlite') as db:
            db.execute('create table threads(id text,cwd text,rollout_path text,history_mode text)')
            db.execute('insert into threads(id,cwd) values(?,?)', (OTHER, str(s.workspace)))
    else:
        (s.home / 'state_5.sqlite').write_text('corrupt')
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        preflight(s)


@pytest.mark.parametrize('failure', ['existing', 'missing-reservation', 'wrong-reservation',
                                     'symlink-reservation', 'public-reservation', 'held-selected',
                                     'unverifiable-holder', 'old-contract', 'unsafe-proof-dir'])
def test_begin_requires_private_reservation_absent_path_and_native_release(setup, failure):
    s = setup
    if failure == 'existing':
        s.workspace.mkdir()
    elif failure == 'missing-reservation':
        s.marker.unlink()
    elif failure == 'wrong-reservation':
        s.marker.write_text('wrong')
    elif failure == 'symlink-reservation':
        target = s.marker.with_name('saved-marker')
        s.marker.rename(target)
        s.marker.symlink_to(target)
    elif failure == 'public-reservation':
        s.marker.chmod(0o644)
    elif failure == 'held-selected':
        s.hold(SID)
    elif failure == 'unverifiable-holder':
        s.bad_probe = True
    elif failure == 'old-contract':
        s.rollout.write_text(s.rollout.read_text().replace('0.160.0', '0.158.0'))
    else:
        folder = s.engine._fresh_resume_folder()
        folder.chmod(0o755)
    with pytest.raises((ValueError, OSError)):
        s.engine.begin_fresh_resume(SID, str(s.rollout), str(s.workspace), token=TOKEN)
    assert not list(s.engine.state_home().glob('fresh-resumes/*.json'))


def test_proof_cannot_be_rebound_or_used_before_allocator_completion(setup):
    s = setup
    s.engine.begin_fresh_resume(SID, str(s.rollout), str(s.workspace), token=TOKEN)
    with pytest.raises(ValueError, match='has not completed'):
        preflight(s)
    s.engine.finish_fresh_resume(TOKEN, SID, str(s.workspace))
    with pytest.raises(ValueError, match='already completed'):
        s.engine.finish_fresh_resume(TOKEN, SID, str(s.workspace))
    with pytest.raises(ValueError, match='identity changed'):
        s.engine.native_preflight('codex', NEW, str(s.workspace), fresh_resume=TOKEN)


def test_engine_passes_sixth_resume_argument_to_claimed_preflight(setup, monkeypatch):
    s = setup
    allocate(s)
    monkeypatch.setattr(s.engine, 'selected_rollout', lambda sid: s.rollout)
    monkeypatch.setattr(s.engine, 'verify_ready', lambda *args: None)
    events = []
    adapter = SimpleNamespace(call=lambda action, *args: events.append(action) or '', output=lambda text: None)
    s.engine.execute('resume', ['dev-repo-1', str(s.workspace), SID, 'codex', 'history', TOKEN], adapter)
    assert events == ['preflight', 'resume', 'ready', 'record']
    s.pid += 1
    with pytest.raises(ValueError, match='writers changed'):
        s.engine.execute('resume', ['dev-repo-2', str(s.workspace), SID, 'codex', 'history', TOKEN], adapter)


@pytest.mark.parametrize('kind', ['unknown', 'known-conflict'])
def test_writer_appearing_during_holder_probe_is_rescanned(setup, monkeypatch, kind):
    s = setup
    allocate(s)
    original = s.engine._fresh_holder
    def appeared(path):
        value = original(path)
        s.hold(NEW)
        if kind == 'known-conflict':
            with sqlite3.connect(s.home / 'state_5.sqlite') as db:
                db.execute('create table threads(id text,cwd text,rollout_path text,history_mode text)')
                db.execute('insert into threads(id,cwd) values(?,?)', (NEW, str(s.workspace)))
        return value
    monkeypatch.setattr(s.engine, '_fresh_holder', appeared)
    with pytest.raises(ValueError, match='writers changed|worktree is still loaded'):
        preflight(s)


def test_finish_rejects_directory_replaced_after_exclusive_creation(setup):
    s = setup
    s.engine.begin_fresh_resume(SID, str(s.rollout), str(s.workspace), token=TOKEN)
    s.workspace.rename(s.workspace.with_name('original-allocation'))
    s.workspace.mkdir()
    with pytest.raises(ValueError, match='workspace identity changed'):
        s.engine.finish_fresh_resume(TOKEN, SID, str(s.workspace))


def test_competing_creator_cannot_supply_allocator_directory(setup, monkeypatch):
    s = setup
    original = s.engine.os.mkdir
    def race(path, *args, **kwargs):
        if str(path) == str(s.workspace):
            original(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(s.engine.os, 'mkdir', race)
    with pytest.raises(FileExistsError):
        s.engine.begin_fresh_resume(SID, str(s.rollout), str(s.workspace), token=TOKEN)
    assert s.workspace.is_dir()
    assert not s.engine._fresh_resume_path(TOKEN).exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public'])
def test_private_proof_record_cannot_be_replaced_with_unsafe_file(setup, kind):
    s = setup
    allocate(s)
    record = s.engine._fresh_resume_path(TOKEN)
    if kind == 'public':
        record.chmod(0o644)
    else:
        target = record.with_name('retained.json')
        record.rename(target)
        if kind == 'symlink':
            record.symlink_to(target)
        else:
            record.hardlink_to(target)
    with pytest.raises((ValueError, OSError)):
        preflight(s)


def test_corrupt_integrity_store_is_a_lifecycle_error_for_normal_preflight(setup):
    s = setup
    (s.home / 'state_5.sqlite').write_text('corrupt')
    with pytest.raises(s.engine.LifecycleError, match='ownership index'):
        s.engine.native_preflight('codex', SID, str(s.workspace), rollout=str(s.rollout))
