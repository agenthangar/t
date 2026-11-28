"""Background service commands use a private launchd fixture and no desktop APIs."""
import os
import re
import subprocess

import pytest

from conftest import REPO_ROOT, load_script


@pytest.fixture
def launchd(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    backend = load_script(REPO_ROOT / 'libexec/t_background.py', 'background_agents')
    monkeypatch.setattr(backend.sys, 'platform', 'darwin')
    jobs = set()
    overrides = {}
    calls = []

    def run(action, *args):
        calls.append((action, *args))
        domain = backend.domain()
        if action == 'print-disabled':
            values = ['disabled services = {', *[f' "{label}" => {value}' for label, value in overrides.items()], '}']
            return subprocess.CompletedProcess(args, 0, '\n'.join(values), '')
        if action == 'bootstrap':
            assert args[0] == domain
            jobs.add(os.path.basename(args[1]).removesuffix('.plist'))
        else:
            label = args[0].removeprefix(domain + '/')
            if action == 'print':
                if label in jobs:
                    return subprocess.CompletedProcess(args, 0, ' state = running\n pid = 1234\n', '')
                return subprocess.CompletedProcess(args, 113, '', 'Could not find service')
            if action == 'bootout':
                jobs.remove(label)
            elif action == 'kickstart':
                jobs.add(label)
            else:
                raise AssertionError(action)
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(backend, 'run', run)
    return backend, jobs, overrides, calls


def test_background_routes(t_mod):
    parser = t_mod.build_parser()
    prefix = ['agent', 'background']
    for action in ('status', 'install', 'start', 'stop', 'restart'):
        args = prefix + [action] + ([] if action == 'status' else ['cleanup'])
        parsed = parser.parse_args(args)
        assert parsed.resource == 'background' and parsed.action == action
        assert t_mod._command_readonly(args) == (action == 'status')
    assert parser.parse_args(prefix + ['install', 'all', '--reinstall']).reinstall
    for action in ('install', 'start', 'stop', 'restart'):
        with pytest.raises(SystemExit):
            parser.parse_args(prefix + [action])
    assert t_mod._REMOTE['policy'](prefix + ['status']) == 'query'
    assert t_mod._REMOTE['policy'](prefix + ['stop']) != 'query'


def test_cleanup_service_lifecycle(launchd, capsys):
    backend, jobs, overrides, calls = launchd
    label = backend.CLEANUP_LABEL
    assert backend.status('cleanup') == ('Not installed', 'Stopped', 'Disabled')
    assert backend.command('install', 'cleanup', False, '15') == 0
    assert backend.status('cleanup') == ('Installed', 'Running', 'Enabled')
    _, path = backend.specification('cleanup')
    original = path.read_bytes()
    assert backend.command('install', 'cleanup', False, '15') == 0
    assert path.read_bytes() == original
    assert backend.command('stop', 'cleanup', False, '15') == 0
    assert backend.status('cleanup') == ('Installed', 'Stopped', 'Enabled')
    assert path.read_bytes() == original
    assert backend.command('install', 'cleanup', True, '15') == 0
    assert label not in jobs
    assert backend.status('cleanup') == ('Installed', 'Stopped', 'Enabled')
    assert backend.command('start', 'cleanup', False, '15') == 0
    assert backend.command('restart', 'cleanup', False, '15') == 0
    assert backend.status('cleanup') == ('Installed', 'Running', 'Enabled')
    overrides[label] = 'disabled'
    assert backend.status('cleanup') == ('Installed', 'Running', 'Disabled')
    capsys.readouterr()
    assert backend.command('status', None, False, '30') == 0
    output = capsys.readouterr().out
    assert re.search(r'Finished session cleanup.*Installed.*Running.*Disabled.*Every 30 min', output)
    assert re.search(r'Automatic recovery.*Not installed.*Stopped.*Disabled.*continuous', output)
    assert all(action in ('print', 'print-disabled') for action, *_ in calls[-2:])


def test_service_rejects_unowned_registration(launchd, tmp_path):
    backend, jobs, _, _ = launchd
    _, path = backend.specification('cleanup')
    path.parent.mkdir(parents=True)
    foreign = tmp_path / 'other.plist'
    foreign.write_text('custom')
    path.symlink_to(foreign)
    assert backend.command('install', 'cleanup', True, '15') == 1
    assert foreign.read_text() == 'custom'
    assert backend.status('cleanup') == ('Unverified', 'Unverified', 'Unverified')
    assert not jobs


def test_cache_login_lifecycle_preserves_stop(launchd, monkeypatch, capsys):
    backend, jobs, _, _ = launchd
    monkeypatch.setattr(backend, 'cache_source', lambda: REPO_ROOT)
    monkeypatch.setattr(backend, 'cache_request', lambda op: {
        'pid': 418, 'bytes_written_last_minute': 321})
    assert backend.command('install', 'cache', False, '15') == 0
    registration = backend.read_service('cache')
    assert registration['ProgramArguments'][1:] == [str(REPO_ROOT/'libexec/t_session_cache.py'), 'serve-managed']
    assert registration['RunAtLoad'] and registration['KeepAlive']
    backend.command('status', 'cache', False, '15')
    assert 'PID 418, 321 bytes written in the last minute' in capsys.readouterr().out
    assert backend.command('stop', 'cache', False, '15') == 0
    assert backend.command('install', 'cache', True, '15') == 0
    assert backend.status('cache') == ('Installed', 'Stopped', 'Enabled')
    assert backend.CACHE_LABEL not in jobs
    assert backend.command('start', 'cache', False, '15') == 0
    assert backend.command('restart', 'cache', False, '15') == 0
    assert backend.CACHE_LABEL in jobs


def test_discovery_database_has_one_runtime_owner():
    # All shipped Python, shell and native clients, including the bundled backend,
    # go through the API. Only the service module may name its SQLite storage.
    owner = REPO_ROOT / 'libexec/t_session_cache.py'
    for folder in ('bin', 'zsh', 'libexec', 'apps/rnnr/Sources'):
        for path in (REPO_ROOT/folder).rglob('*'):
            if (not path.is_file() or path == owner or path.suffix in ('.pyc', '.db')
                    or '__pycache__' in path.parts):
                continue
            content = path.read_bytes()
            assert b'discovery/cache.sqlite3' not in content and b"'cache.sqlite3'" not in content, path
            assert b'from cache_records' not in content and b'into cache_records' not in content, path
