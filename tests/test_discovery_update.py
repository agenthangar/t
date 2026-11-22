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
    address = root / 'service.sock'
    bound = cache._socket_identity(address)
    def serve():
        try:
            server.serve_forever(poll_interval=.01)
        finally:
            if cache._socket_identity(address) == bound:
                address.unlink(missing_ok=True)
    thread = threading.Thread(target=serve)
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
                           XDG_RUNTIME_DIR='/tmp', TMUX_TMPDIR=home / 'tmux').items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv('T_LOCAL_RC', raising=False)
    monkeypatch.setattr(cache, 'SOURCE', old)
    # The subprocess installer uses the genuine namespace, not a fixture map.
    identity = cache._identity()
    root = cache._identity_runtime(identity)
    server, thread = listener(cache, identity, cache._revision())
    try:
        outcome = run_install(new, home, T_LINKS_ONLY='1', T_NO_CODEX_HOOKS='1',
                              XDG_STATE_HOME=os.environ['XDG_STATE_HOME'],
                              XDG_CACHE_HOME=os.environ['XDG_CACHE_HOME'])
        assert outcome.returncode == 0, outcome.stderr
        thread.join(2)
        assert not thread.is_alive()
        assert (home / 'bin/t').resolve() == new / 'bin/t'
    finally:
        close_listener(server, thread)
        shutil.rmtree(root)
