"""Update reconciliation talks only to the intended existing private listeners."""
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from test_session_cache import cache  # noqa: F401


@pytest.fixture
def installation(cache, tmp_path, monkeypatch):
    source = tmp_path / 'canonical'
    source.mkdir()
    monkeypatch.setattr(cache, 'SOURCE', source)
    monkeypatch.setattr(cache, '_revision', lambda: [1, 2, 3, 4, 5])
    # Keep real Unix socket paths short on macOS. All paths remain test-private.
    monkeypatch.setattr(cache, '_identity_runtime', lambda identity:
                        Path(cache._test_runtime.name) / cache._digest(identity)[:8])
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: pytest.fail('started a service'))
    monkeypatch.setattr(cache.os, 'kill', lambda *a: pytest.fail('signalled a process'))
    return source


def listener(cache, identity, revision):
    root = cache._private(cache._identity_runtime(identity))
    server = cache._Server(str(root / 'service.sock'), cache._Handler)
    (root / 'service.sock').chmod(0o600)
    server.revision = revision
    server.memory = cache._Memory(submit=lambda *a: pytest.fail('started collection'))
    server.memory.restore({})
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    return server, thread


def close_listener(server, thread):
    if thread.is_alive():
        server.shutdown()
    thread.join(2)
    server.server_close()


@pytest.mark.parametrize('raw', ['', 'symlink'])
def test_update_retires_obsolete_namespace_and_preserves_current(cache, installation, tmp_path, monkeypatch, raw):
    config = tmp_path / 'config/t/local.zsh'
    config.parent.mkdir(parents=True)
    config.write_text('# same configuration')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    if raw == 'symlink':
        alias = tmp_path / 'selected.zsh'
        alias.symlink_to(config)
        monkeypatch.setenv('T_LOCAL_RC', str(alias))
        raw_key = str(alias)
    else:
        monkeypatch.delenv('T_LOCAL_RC', raising=False)
        raw_key = ''
    current = cache._identity()
    obsolete = list(current); obsolete[7] = raw_key
    current_server, current_thread = listener(cache, current, cache._revision())
    old_server, old_thread = listener(cache, obsolete, cache._revision())
    try:
        cache.retire_obsolete()
        old_thread.join(2)
        assert not old_thread.is_alive()
        assert current_thread.is_alive()
        assert current_server.memory.generations == {}
    finally:
        close_listener(old_server, old_thread)
        close_listener(current_server, current_thread)


def test_update_stale_current_revision_retires_without_startup(cache, installation):
    server, thread = listener(cache, cache._identity(), ['old'])
    try:
        cache.retire_obsolete()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        close_listener(server, thread)


def test_previous_removed_package_source_retires_but_other_source_is_untouched(cache, installation, tmp_path):
    previous = tmp_path / 'removed-keg/libexec'
    old = cache._identity(); old[0] = str(previous)
    other = cache._identity(); other[0] = str(tmp_path / 'other-install')
    old_server, old_thread = listener(cache, old, cache._revision())
    other_server, other_thread = listener(cache, other, ['unrelated'])
    try:
        cache.retire_obsolete([previous])
        old_thread.join(2)
        assert not old_thread.is_alive()
        assert other_thread.is_alive()
    finally:
        close_listener(old_server, old_thread)
        close_listener(other_server, other_thread)


def test_retirement_preserves_worktrees_and_different_explicit_config(cache, installation, tmp_path, monkeypatch):
    worktree = tmp_path / 'worktree'; worktree.mkdir()
    (worktree / '.git').write_text('gitdir: elsewhere')
    explicit = tmp_path / 'custom.zsh'; explicit.write_text('# custom')
    monkeypatch.setenv('T_LOCAL_RC', str(explicit))
    identities = list(cache._retirement_identities(installation))
    assert all(identity[7] for identity in identities)
    calls = []
    monkeypatch.setattr(cache, '_existing_status', lambda *a: calls.append(a))
    cache.retire_obsolete([worktree])
    assert {call[0] for call in calls} == {cache._identity_runtime(identity) for identity in identities}
    assert not ({call[0] for call in calls} & {
        cache._identity_runtime(identity) for identity in cache._retirement_identities(worktree)})
    calls.clear()
    monkeypatch.setattr(cache, 'SOURCE', worktree)
    cache.retire_obsolete()
    assert not calls


@pytest.mark.parametrize('unsafe', ['directory-link', 'socket-link', 'public-directory', 'public-socket'])
def test_retirement_rejects_unsafe_endpoints(cache, installation, tmp_path, monkeypatch, unsafe):
    root = cache._private(cache._identity_runtime(cache._identity()))
    address = root / 'service.sock'
    connection = socket.socket(socket.AF_UNIX)
    connection.bind(str(address)); address.chmod(0o600)
    try:
        if unsafe == 'directory-link':
            relocated = root.with_name('moved'); root.rename(relocated); root.symlink_to(relocated)
        elif unsafe == 'socket-link':
            target = root / 'other.sock'; address.rename(target); address.symlink_to(target)
        elif unsafe == 'public-directory':
            root.chmod(0o755)
        else:
            address.chmod(0o666)
        monkeypatch.setattr(cache.socket, 'socket', lambda *a: pytest.fail('contacted unsafe endpoint'))
        cache.retire_obsolete()
    finally:
        connection.close()
        if root.is_symlink():
            root.unlink()


def test_retirement_stalled_listener_is_bounded(cache, installation):
    root = cache._private(cache._identity_runtime(cache._identity()))
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(root / 'service.sock'))
        (root / 'service.sock').chmod(0o600)
        server.listen()
        started = time.monotonic()
        cache.retire_obsolete()
        assert time.monotonic() - started < .8


def test_retirement_with_no_helper_leaves_no_runtime_files(cache, installation, monkeypatch):
    roots = [cache._identity_runtime(identity) for identity in cache._retirement_identities(installation)]
    monkeypatch.setattr(cache.socket, 'socket', lambda *a: pytest.fail('opened an absent helper'))
    cache.retire_obsolete()
    assert not any(root.exists() for root in roots)


def test_installer_recognizes_only_related_package_sources(cache, installation, tmp_path, monkeypatch):
    releases = tmp_path / 'releases'; releases.mkdir()
    old, new = releases / 'v0.1.1', releases / 'v0.1.2'
    for source in (old, new):
        source.mkdir(); (source / '.t-release-version').write_text(source.name)
    monkeypatch.setattr(cache, 'SOURCE', new)
    assert cache._same_installation(old)
    assert not cache._same_installation(installation)
    (old / '.git').write_text('gitdir: /other/worktree')
    assert not cache._same_installation(old)
    (old / '.git').unlink()
    old.rename(tmp_path / 'old-cellar')
    old = tmp_path / 'old-cellar'
    marker = dict(formula='agenthangar/tap/t', opt_libexec='/opt/t/libexec')
    for source in (old, new):
        (source / '.t-homebrew').write_text(json.dumps(marker))
    assert cache._same_installation(old)
    link = tmp_path / 't'; link.symlink_to(old / 'bin/t')
    assert cache.linked_source(link) == str(old)
    assert cache.linked_source(tmp_path / 'missing') == ''


def test_old_client_links_only_upgrade_retires_previous_release(cache, tmp_path, monkeypatch):
    from _install_helpers import _seed_worktree, run_install
    import shutil
    old, new = tmp_path / 'releases/v0.1.1', tmp_path / 'releases/v0.1.2'
    for source in (old, new):
        _seed_worktree(source)
        (source / '.t-release-version').write_text(source.name)
    home = tmp_path / 'home'
    (home / 'bin').mkdir(parents=True)
    (home / 'bin/t').symlink_to(old / 'bin/t')
    for key, value in dict(HOME=home, XDG_CONFIG_HOME=home / '.config',
                           CODEX_HOME=home / '.codex', CLAUDE_CONFIG_DIR=home / '.claude',
                           XDG_RUNTIME_DIR='/tmp').items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv('T_LOCAL_RC', raising=False)
    monkeypatch.setattr(cache, 'SOURCE', old)
    # The subprocess installer uses the genuine namespace, not a fixture map.
    identity = cache._identity()
    root = cache._identity_runtime(identity)
    server, thread = listener(cache, identity, cache._revision())
    try:
        outcome = run_install(new, home, T_LINKS_ONLY='1', T_NO_CODEX_HOOKS='1')
        assert outcome.returncode == 0, outcome.stderr
        thread.join(2)
        assert not thread.is_alive()
        assert (home / 'bin/t').resolve() == new / 'bin/t'
    finally:
        close_listener(server, thread)
        shutil.rmtree(root)


@pytest.fixture
def brew_install(cache, tmp_path, monkeypatch):
    rack = tmp_path / 'Cellar/t'
    opt = tmp_path / 'opt/t'
    opt.parent.mkdir()
    sources = []
    for version in ('1.2.3', '1.2.4'):
        source = rack / version / 'libexec'
        source.mkdir(parents=True)
        (source / '.t-release-version').write_text(version)
        (source / '.t-homebrew').write_text(json.dumps(dict(
            formula='agenthangar/tap/t', opt_libexec=str(opt / 'libexec'), brew=cache.sys.executable)))
        sources.append(source)
    opt.symlink_to(sources[-1].parent)
    monkeypatch.setattr(cache, 'SOURCE', sources[-1])
    monkeypatch.setattr(cache, '_revision', lambda: [1, 2, 3])
    cache._real_brew_process_sources = cache._brew_process_sources
    monkeypatch.setattr(cache, '_brew_process_sources', lambda brew: [])
    monkeypatch.setattr(cache, '_identity_runtime', lambda identity:
                        Path(cache._test_runtime.name) / cache._digest(identity)[:8])
    return sources, opt


def test_brew_stable_identity_and_physical_cache_fallback(cache, brew_install, monkeypatch):
    sources, opt = brew_install
    current = cache._identity()
    assert current[0] == str(opt / 'libexec')
    identities = list(cache._snapshot_identities())
    assert identities[0] == current
    assert [str(source) for source in reversed(sources)] == list(dict.fromkeys(
        identity[0] for identity in identities if identity[0] != current[0]))
    monkeypatch.setattr(cache, 'SOURCE', sources[0])
    assert cache._identity() == current
    monkeypatch.setenv('T_LOCAL_RC', '/different-config')
    assert cache._identity() != current


def test_brew_migration_retires_physical_helpers_preserves_stable(cache, brew_install):
    sources, opt = brew_install
    identity = cache._identity()
    legacy = list(identity); legacy[0] = str(sources[0])
    current_server, current_thread = listener(cache, identity, cache._revision())
    old_server, old_thread = listener(cache, legacy, ['old'])
    try:
        cache.retire_obsolete()
        old_thread.join(2)
        assert not old_thread.is_alive()
        assert current_thread.is_alive()
    finally:
        close_listener(old_server, old_thread)
        close_listener(current_server, current_thread)


def test_brew_pruned_keg_candidate_is_recovered(cache, brew_install, monkeypatch):
    sources, opt = brew_install
    import shutil
    old = sources[0]
    shutil.rmtree(old.parent)
    monkeypatch.setattr(cache, '_brew_process_sources', lambda brew: [old, old.parents[2] / 'other/1/libexec'])
    assert old in cache._brew_sources(cache._homebrew())
    assert all(source.parent.parent == old.parent.parent for source in cache._brew_sources(cache._homebrew()))


def test_brew_current_daemon_rejects_stale_revision_without_exit(cache, brew_install):
    sources, opt = brew_install
    identity = cache._identity()
    server, thread = listener(cache, identity, cache._revision())
    server.homebrew = cache._homebrew()
    try:
        root = cache._identity_runtime(identity)
        cache._existing_status(root, ['stale'], time.monotonic() + 1)
        assert thread.is_alive()
        # The captured marker remains sufficient after the old keg is deleted.
        opt.unlink(); opt.symlink_to(sources[0].parent)
        cache._existing_status(root, ['new'], time.monotonic() + 1)
        thread.join(2)
        assert not thread.is_alive()
    finally:
        close_listener(server, thread)


@pytest.mark.parametrize('unsafe', ['marker-link', 'wrong-opt', 'worktree'])
def test_brew_identity_rejects_untrusted_markers(cache, brew_install, unsafe):
    sources, opt = brew_install
    source = sources[-1]
    if unsafe == 'marker-link':
        marker = source / '.t-homebrew'
        saved = source / 'saved'
        marker.rename(saved); marker.symlink_to(saved)
    elif unsafe == 'wrong-opt':
        marker = json.loads((source / '.t-homebrew').read_text())
        marker['opt_libexec'] = '/unrelated/libexec'
        (source / '.t-homebrew').write_text(json.dumps(marker))
    else:
        (source / '.git').write_text('gitdir: /elsewhere')
    assert cache._homebrew() is None
    assert cache._identity()[0] == str(source)


def test_brew_process_evidence_accepts_only_exact_helpers(cache, brew_install, monkeypatch):
    sources, opt = brew_install
    old = sources[0]
    commands = [
        f'/usr/bin/python3 {old}/libexec/t_session_cache.py serve',
        f'/opt/homebrew/Frameworks/Python.app/Contents/MacOS/Python {old}/libexec/t_session_cache.py serve',
        f'/usr/bin/python3 {old}/libexec/t_session_cache.py serve --extra',
        f'/usr/bin/python3 /unrelated/Cellar/t/1/libexec/libexec/t_session_cache.py serve',
        f'/bin/sh {old}/libexec/t_session_cache.py serve',
        f'/usr/bin/python3 -c "{old}/libexec/t_session_cache.py serve"',
    ]
    def run(argv, **kwargs):
        assert argv == ['ps', '-ww', '-u', str(os.getuid()), '-o', 'command=']
        assert kwargs['timeout'] == .2
        kwargs['stdout'].write(('\n'.join(commands)).encode())
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(cache.subprocess, 'run', run)
    assert cache._real_brew_process_sources(cache._homebrew()) == [old, old]


@pytest.mark.parametrize('stable', [False, True])
def test_brew_restores_physical_snapshot_but_prefers_stable(cache, brew_install, stable):
    import sqlite3
    sources, opt = brew_install
    memory = cache._Memory(submit=lambda *a: None)
    memory.restore({})
    disk = cache._Persistence(memory)
    legacy = cache._identity(); legacy[0] = str(sources[0])
    with disk._open() as db:
        db.execute('insert into snapshots values (?,?,?)',
                   (cache._digest(legacy), cache.VERSION, json.dumps({'artifacts': {'test': 'legacy'}})))
        if stable:
            db.execute('insert into snapshots values (?,?,?)',
                       (disk.namespace, cache.VERSION, json.dumps({'artifacts': {'test': 'stable'}})))
    disk.stop.set()
    disk.run()
    assert memory.artifacts == {'test': 'stable' if stable else 'legacy'}


@pytest.mark.parametrize('links_only', [False, True])
def test_old_brew_client_installer_recovers_path_after_opt_moved(cache, tmp_path, monkeypatch, links_only):
    from _install_helpers import _seed_worktree, run_install
    import shutil
    rack = tmp_path / 'Cellar/t'
    old, new = (rack / '1.2.3/libexec', rack / '1.2.4/libexec')
    opt = tmp_path / 'opt/t'
    opt.parent.mkdir()
    for source in (old, new):
        _seed_worktree(source)
        (source / '.t-release-version').write_text('v' + source.parent.name)
        (source / '.t-homebrew').write_text(json.dumps(dict(
            formula='agenthangar/tap/t', opt_libexec=str(opt / 'libexec'), brew=cache.sys.executable)))
    opt.symlink_to(new.parent)
    home = tmp_path / 'home'
    (home / 'bin').mkdir(parents=True)
    (home / 'bin/t').symlink_to(opt / 'libexec/bin/t')
    for key, value in dict(HOME=home, XDG_CONFIG_HOME=home / '.config',
                           CODEX_HOME=home / '.codex', CLAUDE_CONFIG_DIR=home / '.claude',
                           XDG_RUNTIME_DIR='/tmp').items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv('T_LOCAL_RC', raising=False)
    monkeypatch.setattr(cache, 'SOURCE', new)
    identity = cache._identity(); identity[0] = str(old)
    root = cache._identity_runtime(identity)
    server, thread = listener(cache, identity, ['old'])
    try:
        options = dict(T_NO_CODEX_HOOKS='1')
        if links_only:
            options['T_LINKS_ONLY'] = '1'
        outcome = run_install(new, home, **options)
        assert outcome.returncode == 0, outcome.stderr
        thread.join(2)
        assert not thread.is_alive()
    finally:
        close_listener(server, thread)
        shutil.rmtree(root)


def test_brew_reinstall_revision_retires_old_daemon(cache, brew_install, monkeypatch):
    server, thread = listener(cache, cache._identity(), cache._revision())
    server.homebrew = cache._homebrew()
    try:
        monkeypatch.setattr(cache, '_revision', lambda: [9, 9, 9])
        cache._existing_status(cache._identity_runtime(cache._identity()), cache._revision(), time.monotonic() + 1)
        thread.join(2)
        assert not thread.is_alive()
    finally:
        close_listener(server, thread)


@pytest.mark.parametrize('pruned', [False, True])
def test_first_brew_discovery_request_migrates_legacy_daemon(cache, tmp_path, monkeypatch, pruned):
    from _install_helpers import _seed_worktree
    import shutil
    import subprocess
    import sys
    rack = tmp_path / 'Cellar/t'
    old, new = rack / '1.2.3/libexec', rack / '1.2.4/libexec'
    opt = tmp_path / 'opt/t'
    opt.parent.mkdir()
    for source in (old, new):
        _seed_worktree(source)
        (source / '.t-release-version').write_text('v' + source.parent.name)
        (source / '.t-homebrew').write_text(json.dumps(dict(
            formula='agenthangar/tap/t', opt_libexec=str(opt / 'libexec'), brew=cache.sys.executable)))
    # Reproduce the former physical identity without relying on the old updater
    # to pass a previous source. Launch an exact helper argv for process recovery.
    script = old / 'libexec/t_session_cache.py'
    script.write_text(script.read_text().replace("brew['opt'] if brew else str(SOURCE)", 'str(SOURCE)'))
    opt.symlink_to(old.parent)
    monkeypatch.setattr(cache, 'SOURCE', old)
    legacy = cache._identity(); legacy[0] = str(old)
    old_root = cache._identity_runtime(legacy)
    old_process = subprocess.Popen([sys.executable, str(script), 'serve'], env=cache._service_env())
    children = []
    original_popen = subprocess.Popen
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if args[0][-1] == 'serve':
            children.append(process)
        return process
    try:
        deadline = time.monotonic() + 4
        while not (old_root / 'service.sock').exists():
            assert old_process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        opt.unlink(); opt.symlink_to(new.parent)
        if pruned:
            shutil.rmtree(old.parent)
        monkeypatch.setattr(cache, 'SOURCE', new)
        monkeypatch.setattr(cache, '__file__', str(new / 'libexec/t_session_cache.py'))
        monkeypatch.setattr(cache, '_runtime', lambda: cache._identity_runtime(cache._identity()))
        monkeypatch.setattr(cache.subprocess, 'Popen', popen)
        result = cache.request('status')
        assert not result.get('unavailable'), result
        assert len(children) == 1
        old_process.wait(timeout=4)
        assert old_process.returncode == 0
    finally:
        for process in [old_process, *children]:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        for root in (old_root, cache._identity_runtime(cache._identity())):
            shutil.rmtree(root, ignore_errors=True)
