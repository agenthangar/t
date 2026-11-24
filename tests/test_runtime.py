"""Runtime updates use cooperative owner-bound protocols in isolated state."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import sys
import time

import pytest

from conftest import REPO_ROOT, load_script
from test_session_lifecycle import lifecycle, _shell, _run, _until


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'release'
    root.mkdir()
    for name in ('libexec', 'zsh', 'ui'):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'bin').mkdir()
    shutil.copy2(REPO_ROOT / 'bin/t', root / 'bin/t')
    (root / '.t-install-version').write_text('1\n')
    (root / '.t-release-version').write_text('v0.1.1\n')
    return root


@pytest.fixture
def runtime(source, tmp_path, monkeypatch):
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv('TMUX_TMPDIR', str(tmp_path / 'tmux'))
    (tmp_path / 'tmux').mkdir()
    return load_script(source / 'libexec/t_runtime.py', 'runtime_test')


def test_noop_retries_partial_adapters_and_receipts(runtime, monkeypatch):
    calls = []
    class Adapter:
        name = 'discoverydaemon'
        failure = True
        def inspect(self, context, target): return runtime.outcome('updated')
        def apply(self, context, target):
            calls.append(True)
            if self.failure: raise OSError('temporary failure')
            context['discovery_result'] = dict(status='current')
        def verify(self, *args): return True
    adapter = Adapter()
    monkeypatch.setattr(runtime, 'ADAPTERS', (adapter,))
    monkeypatch.setattr(runtime, 'socket_hints', lambda: [])
    assert runtime.reconcile()['failed']
    assert runtime.read(runtime.private_root() / 'last-reconcile.json')['failed']
    adapter.failure = False
    assert not runtime.reconcile()['failed']
    assert calls == [True, True]


def test_content_fingerprint_and_development_pin(runtime, source, monkeypatch):
    before = runtime.fingerprint('terminalobserver')
    with (source / 'libexec/t_terminal_close.py').open('a') as stream: stream.write('\n# changed without a version bump\n')
    assert runtime.fingerprint('terminalobserver') != before
    (source / '.git').write_text('gitdir: elsewhere')
    monkeypatch.setattr(runtime, 'bootstrap', lambda *a: pytest.fail('changed shared dispatcher'))
    value = runtime.reconcile()
    assert value['results'][0]['status'] == 'deferred'
    assert not value['failed']


def test_hook_parser_rejects_user_wrappers_and_preserves_the_reserved_slot(runtime, monkeypatch):
    pane = dict(identity='$1', session='dev-r-1', pane='%1', pane_pid='123', workspace='/work', dead='0')
    helper = runtime.module('t_terminal_close.py')
    commands = helper['hook_commands']('/socket', pane)
    line = 'client-detached[50100] ' + commands['client-detached[50100]']
    assert runtime.parsed_hook(line, '/socket', pane)
    assert runtime.parsed_hook("client-detached[50100] run-shell -b 'echo t_terminal_close.py'", '/socket', pane) is None
    monkeypatch.setattr(runtime, 'tmux', lambda *a: subprocess.CompletedProcess(a, 0,
        "client-detached[50100] run-shell -b 'echo t_terminal_close.py'\n", ''))
    assert runtime.Hooks().inspect({}, ('/socket', pane))['status'] == 'deferred'


def test_foreign_socket_and_copied_record_are_not_ownership(runtime, monkeypatch, tmp_path):
    file = tmp_path / 'not-a-socket'
    file.touch()
    assert not runtime.owned_socket(file)
    record = dict(protocol=1, component='terminalobserver', pid=123, birth='same', source=str(runtime.ROOT),
                  socket='/other', tty='/dev/pts/1', client_pid='456', descriptor=7, lock_descriptor=8)
    monkeypatch.setattr(runtime, 'native', lambda *a: dict(birth='same', executable='/usr/bin/python3',
        argv=['python3', str(runtime.ROOT / 'libexec/t_terminal_close.py'), 'watch', '/other', '/dev/pts/1', '456', '7', '8']))
    assert not runtime.live_record(record, 'terminalobserver', (tmp_path / 'runtime.json', '/target', None))


def _observer(runtime, source, socket=None, client_pid='99999999'):
    runtime.bootstrap()
    terminal = runtime.module('t_terminal_close.py')
    master, slave = pty.openpty()
    tty, socket = os.ttyname(slave), socket or '/tmp/t-runtime-test-no-server'
    base = terminal['event_path'](socket, tty)
    lock = os.open(str(base) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    process = subprocess.Popen([sys.executable, str(source / 'libexec/t_terminal_close.py'), 'watch', socket,
                               tty, client_pid, str(slave), str(lock)], pass_fds=(slave, lock), env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.close(slave)
    os.close(lock)
    path = Path(str(base) + '.runtime.json')
    _until(lambda: runtime.read(path).get('pid') == process.pid)
    return process, master, path, base


def test_observer_exec_preserves_original_pty_lock_and_elapsed_loss_deadline(runtime, source):
    started = time.monotonic()
    process, master, path, base = _observer(runtime, source)
    try:
        before = runtime.read(path)
        time.sleep(2)
        with (source / 'libexec/t_terminal_close.py').open('a') as stream: stream.write('\n# observer upgrade\n')
        expected = runtime.fingerprint('terminalobserver')
        runtime.request_reload(Path(str(base) + '.reload.json'), before, source, 'terminalobserver')
        _until(lambda: runtime.read(path).get('fingerprint') == expected)
        after = runtime.read(path)
        assert all(after[key] == before[key] for key in ('pid', 'birth', 'token', 'terminal_fd', 'lock_fd'))
        assert after['gone_at'] is not None and after['gone_at'] < time.monotonic() - 1
        fd = os.open(str(base) + '.lock', os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        assert process.wait(timeout=5) == 0, process.stderr.read().decode()
        assert time.monotonic() - started < 6.5  # original five-second deadline, not a new timer after reload
    finally:
        os.close(master)
        if process.poll() is None: process.terminate()
        process.wait(timeout=5)


def test_bad_replacement_keeps_original_observer_and_singleton(runtime, source):
    process, master, path, base = _observer(runtime, source)
    try:
        before = runtime.read(path)
        request = dict(protocol=runtime.PROTOCOL, source=runtime.stable_source(source),
                       fingerprint=runtime.fingerprint('terminalobserver'), token=before['token'],
                       pid=before['pid'], birth=before['birth'])
        (source / 'libexec/t_runtime_dispatch.py').write_text('this is not Python (\n')
        runtime.write(Path(str(base) + '.reload.json'), request)
        time.sleep(.3)
        assert process.poll() is None
        assert runtime.read(path)['pid'] == before['pid']
        assert runtime.read(path)['fingerprint'] == before['fingerprint']
        assert not Path(str(base) + '.reload.json').exists()
        fd = os.open(str(base) + '.lock', os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)
    finally:
        os.close(master)
        process.wait(timeout=6)


def test_real_noop_update_repairs_hooks_without_agent_or_job_signals(lifecycle, source, monkeypatch):
    base, env = lifecycle
    assert _shell(base, env, 't session open repo 1 --codex').returncode == 0
    for key, value in env.items(): monkeypatch.setenv(key, value)
    runtime = load_script(source / 'libexec/t_runtime.py', 'runtime_real_test')
    runtime.bootstrap(previous_sources=[REPO_ROOT])
    terminal = runtime.module('t_terminal_close.py')
    for hook in ('client-detached[50100]', 'client-attached[50100]', 'client-session-changed[50100]'):
        assert _run([str(base / 'bin/tmux'), 'set-hook', '-u', '-t', '=dev-repo-1:', hook], env=env, cwd=base).returncode == 0
    assert terminal['install']('dev-repo-1') == 0
    socket = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:', '#{socket_path}'], env=env, cwd=base).stdout.strip()
    # Reconcile uses verified supplied socket hints, never another user's server.
    monkeypatch.setattr(runtime, 'socket_hints', lambda: [socket])
    marker = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:', '#{pane_pid}'], env=env, cwd=base).stdout
    assert _run([str(base / 'bin/tmux'), 'set-hook', '-u', '-t', '=dev-repo-1:', 'client-detached[50100]'], env=env, cwd=base).returncode == 0
    job = subprocess.Popen(['sleep', '30'], cwd=base, env=env)
    try:
        first = runtime.reconcile()
        assert not first['failed'], first
        assert any(item['component'] == 'terminalhooks' and item['status'] == 'updated' for item in first['results']), json.dumps(first, indent=2)
        second = runtime.reconcile()
        assert not second['failed']
        assert all(item['status'] == 'current' for item in second['results']), second
        assert job.poll() is None
        assert _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:', '#{pane_pid}'], env=env, cwd=base).stdout == marker
    finally:
        job.terminate()
        job.wait(timeout=5)


def test_legacy_detached_observer_remains_deferred_without_takeover(runtime, monkeypatch, tmp_path):
    base = tmp_path / 'legacy'
    path = Path(str(base) + '.runtime.json')
    fd = os.open(str(base) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(runtime, 'native', lambda *a: pytest.fail('legacy owner was replaced'))
    try:
        state = runtime.Worker('terminalobserver').inspect({'roots': []}, (path, '/socket', None))
        assert state['status'] == 'deferred' and 'legacy' in state['reason']
    finally: os.close(fd)


def test_observer_survives_old_homebrew_root_removal(runtime, source, tmp_path):
    opt = tmp_path / 'opt/libexec'
    opt.parent.mkdir()
    opt.symlink_to(source)
    marker = dict(formula='agenthangar/tap/t', opt_libexec=str(opt), brew='/unused/brew')
    (source / '.t-homebrew').write_text(json.dumps(marker))
    process, master, path, base = _observer(runtime, source)
    try:
        before = runtime.read(path)
        new = tmp_path / 'new-keg'
        shutil.copytree(source, new)
        with (new / 'libexec/t_terminal_close.py').open('a') as stream: stream.write('\n# new keg runtime\n')
        opt.unlink()
        opt.symlink_to(new)
        updated = load_script(new / 'libexec/t_runtime.py', 'new_keg_runtime')
        updated.bootstrap(previous_sources=[source])
        shutil.rmtree(source)
        updated.request_reload(Path(str(base) + '.reload.json'), before, new, 'terminalobserver')
        expected = updated.fingerprint('terminalobserver')
        _until(lambda: updated.read(path).get('fingerprint') == expected)
        after = updated.read(path)
        assert after['pid'] == process.pid and after['source'] == str(new)
        assert after['terminal_fd'] == before['terminal_fd'] and after['lock_fd'] == before['lock_fd']
        assert process.wait(timeout=6) == 0, process.stderr.read().decode()
    finally:
        os.close(master)
        if process.poll() is None: process.terminate()
        process.wait(timeout=5)


def test_recovery_selfexec_ack_keeps_owner_token(runtime, source, tmp_path):
    runtime.bootstrap()
    socket = '/tmp/t-runtime-recovery-no-server'
    # Keep the watcher alive without an actual server; this is its ordinary
    # safe loop with only the pane observation replaced by isolated sample data.
    helper = source / 'libexec/t_recovery.py'
    text = helper.read_text().replace('if __name__ == "__main__":',
        'Watcher.poll = lambda self: True\n\nif __name__ == "__main__":')
    helper.write_text(text)
    recovery = runtime.module('t_recovery.py')
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    process = subprocess.Popen([sys.executable, str(helper), 'watch', socket], env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    path = recovery['cache'](socket) / 'watch.runtime.json'
    try:
        _until(lambda: runtime.read(path).get('pid') == process.pid)
        before = runtime.read(path)
        helper.write_text(text + '\n# recovery update\n')
        expected = runtime.fingerprint('recoverywatcher')
        runtime.request_reload(path.with_name('watch.reload.json'), before, source, 'recoverywatcher')
        _until(lambda: runtime.read(path).get('fingerprint') == expected)
        after = runtime.read(path)
        assert all(after[key] == before[key] for key in ('pid', 'birth', 'token'))
        assert process.poll() is None
        _until(lambda: not path.with_name('watch.reload.json').exists())
        time.sleep(.15)
        assert runtime.read(path) == after  # no repeated exec/publish loop
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_new_binary_dispatch_is_explicit_and_failure_propagates(t_mod, monkeypatch, tmp_path, capsys):
    source = tmp_path / 'new'
    (source / 'libexec').mkdir(parents=True)
    (source / 'libexec/t_runtime.py').touch()
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, json.dumps(dict(results=[dict(component='terminalobserver',
            status='failed', reason='retry')], failed=True)), '')
    monkeypatch.setattr(t_mod.subprocess, 'run', run)
    assert t_mod._t_reconcile_runtime(str(source), ['/old']) == 1
    argv, options = calls[0]
    assert argv[:3] == [t_mod.sys.executable, str(source / 'bin/t'), '__runtime-reconcile']
    assert json.loads(argv[3])['previous_sources'] == ['/old']
    assert options['cwd'] == '/' and options['timeout'] == 90
    assert 'T_DEV_CONTEXT' not in options['env']
    assert 'failed terminalobserver' in capsys.readouterr().err


def test_pending_detach_event_survives_observer_handoff_with_original_descriptor(runtime, source, tmp_path):
    helper = source / 'libexec/t_terminal_close.py'
    # Exercise descriptor handoff independently of the native exit observer.
    original = helper.read_text().replace("DARWIN = sys.platform == 'darwin'", 'DARWIN = False')
    # A test-only startup pause makes the request+event ordering deterministic.
    loop = '        while True:\n            if terminal:'
    assert loop in original
    helper.write_text(original.replace(loop, '        time.sleep(.5)\n' + loop, 1))
    process, master, path, base = _observer(runtime, source)
    evidence = tmp_path / 'pending-event-evidence.json'
    try:
        before = runtime.read(path)
        callback = '''
def detached(socket, identity, pane, pane_pid, tty, terminal_fd):
    Path(%r).write_text(json.dumps(dict(identity=identity, pane=pane, pane_pid=pane_pid,
                                      terminal_fd=runtime()['fd_identity'](terminal_fd))))
    return 0

''' % str(evidence)
        helper.write_text(original.replace("if __name__ == '__main__':", callback + "if __name__ == '__main__':"))
        runtime.request_reload(Path(str(base) + '.reload.json'), before, source, 'terminalobserver')
        runtime.write(Path(str(base) + '.event'), ['$9', '%8', '777'])
        assert process.wait(timeout=6) == 0, process.stderr.read().decode()
        saved = json.loads(evidence.read_text())
        assert saved == dict(identity='$9', pane='%8', pane_pid='777', terminal_fd=before['terminal_fd'])
        assert not Path(str(base) + '.event').exists()
    finally:
        os.close(master)
        if process.poll() is None: process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize('component,file', [('terminalobserver', 't_terminal_close.py'),
                                           ('recoverywatcher', 't_recovery.py')])
def test_replacement_requires_helper_capability_protocol(runtime, source, component, file):
    path = source / 'libexec' / file
    path.write_text(path.read_text().replace('RUNTIME_PROTOCOL = 1', 'RUNTIME_PROTOCOL = 2'))
    with pytest.raises(ValueError, match='helper reload protocol'):
        runtime.validate_source(source, component)


def test_runtime_owner_race_prevents_hook_writes(runtime, monkeypatch):
    pane = dict(identity='$1', session='dev-r-1', pane='%1', pane_pid='123', workspace='/work', dead='0')
    monkeypatch.setattr(runtime, 'same_owner', lambda *args: False)
    monkeypatch.setattr(runtime, 'module', lambda *args: pytest.fail('attempted a hook write after owner change'))
    with pytest.raises(ValueError, match='owner changed'):
        runtime.Hooks().apply({}, ('/socket', pane))


def test_legacy_observer_discovery_is_exact_and_read_only(runtime, monkeypatch):
    socket = '/private/test.sock'
    args = ['python3', str(runtime.ROOT / 'libexec/t_terminal_close.py'), 'watch', socket,
            '/dev/pts/1', '456', '7', '8']
    monkeypatch.setattr(runtime, 'command', lambda *a, **k: subprocess.CompletedProcess(a, 0,
        f'123 {os.getuid()} ' + ' '.join(args) + '\n', ''))
    monkeypatch.setattr(runtime, 'native', lambda *a: dict(pid=123, birth='old', argv=args))
    found = runtime.legacy_workers(socket, 'terminalobserver')
    assert found[0]['tty'] == '/dev/pts/1' and found[0]['source'] == str(runtime.ROOT)
    args.append('unexpected-wrapper-work')
    assert runtime.legacy_workers(socket, 'terminalobserver') == []


def test_required_missing_recovery_watcher_is_repaired_only_after_enable(lifecycle, source, monkeypatch):
    base, env = lifecycle
    assert _shell(base, env, 't session open repo 1 --codex').returncode == 0
    for key, value in env.items(): monkeypatch.setenv(key, value)
    runtime = load_script(source / 'libexec/t_runtime.py', 'runtime_recovery_repair')
    runtime.bootstrap(previous_sources=[REPO_ROOT])
    terminal = runtime.module('t_terminal_close.py')
    for hook in ('client-detached[50100]', 'client-attached[50100]', 'client-session-changed[50100]'):
        assert _run([str(base / 'bin/tmux'), 'set-hook', '-u', '-t', '=dev-repo-1:', hook], env=env, cwd=base).returncode == 0
    assert terminal['install']('dev-repo-1') == 0
    socket = _run([str(base / 'bin/tmux'), 'display-message', '-p', '-t', '=dev-repo-1:', '#{socket_path}'], env=env, cwd=base).stdout.strip()
    monkeypatch.setattr(runtime, 'socket_hints', lambda: [socket])
    disabled = runtime.reconcile()
    assert not any(item['component'] == 'recoverywatcher' for item in disabled['results'])
    monkeypatch.delenv('T_RECOVERY_DISABLE')
    assert _run([str(base / 'bin/tmux'), 'set-environment', '-u', '-t', '=dev-repo-1:', 'T_RECOVERY_DISABLE'], env=env, cwd=base).returncode == 0
    path = runtime.module('t_recovery.py')['cache'](socket) / 'watch.runtime.json'
    try:
        repaired = runtime.reconcile()
        assert not repaired['failed'], repaired
        assert any(item['component'] == 'recoverywatcher' and item['status'] == 'updated' for item in repaired['results'])
        record = runtime.read(path)
        assert runtime.live_record(record, 'recoverywatcher', (path, socket, None))
        again = runtime.reconcile()
        assert any(item['component'] == 'recoverywatcher' and item['status'] == 'current' for item in again['results'])
    finally:
        record = runtime.read(path)
        if record and runtime.live_record(record, 'recoverywatcher', (path, socket, None)):
            os.kill(record['pid'], 15)  # Only this test-owned watcher, never the agent or tmux server.


@pytest.fixture
def alias_server(runtime, monkeypatch):
    import socket
    import tempfile
    with tempfile.TemporaryDirectory(prefix='t-hotfix-', dir='/tmp') as directory:
        base = Path(directory)
        real = base / 'real'; real.mkdir()
        (base / 'alias').symlink_to(real, target_is_directory=True)
        (real / 'private').mkdir()
        canonical, alias = str(real / 'private/server'), str(base / 'alias/private/server')
        server = socket.socket(socket.AF_UNIX)
        server.bind(canonical)
        def tmux(socket, *args):
            return subprocess.CompletedProcess(args, 0, canonical + '\n' if 'display-message' in args else '', '')
        monkeypatch.setattr(runtime, 'tmux', tmux)
        try:
            yield canonical, alias
        finally:
            server.close()


def test_server_inventory_deduplicates_verified_aliases_and_keeps_distinct_sockets(runtime, alias_server):
    import socket
    canonical, alias = alias_server
    other = str(Path(canonical).parent.parent / 'server')
    with socket.socket(socket.AF_UNIX) as second:
        second.bind(other)
        inventory = runtime.server_inventory([alias, canonical, other])
        assert len(inventory) == 2
        assert inventory[0]['socket'] == canonical
        assert {alias, canonical} <= inventory[0]['aliases']
        assert inventory[0]['identity'] != inventory[1]['identity']


def test_exact_owned_historical_hooks_parse_without_user_wrapper_adoption(runtime, alias_server):
    canonical, alias = alias_server
    pane = dict(identity='$1', pane='%1', pane_pid='123')
    command = runtime.module('t_terminal_close.py')['hook_commands'](alias, pane)['client-detached[50100]']
    assert runtime.parsed_hook('client-detached[50100] ' + command, canonical, pane)
    assert runtime.parsed_hook('client-detached[50100] run-shell "echo '+alias+'"', canonical, pane) is None


def test_alias_worker_target_preserves_original_lock_and_event(runtime, alias_server):
    canonical, alias = alias_server
    runtime.server_inventory([canonical, alias])
    terminal = runtime.module('t_terminal_close.py')
    base = terminal['event_path'](alias, '/dev/pts/1')
    event = Path(str(base) + '.event'); event.write_text('pending event')
    with open(str(base) + '.lock', 'w') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = runtime.worker_path('terminalobserver', canonical, '/dev/pts/1')
        assert path == Path(str(base) + '.runtime.json')
        before = runtime.Worker('terminalobserver').inspect(dict(roots={str(runtime.ROOT)}),
            (path, canonical, dict(tty='/dev/pts/1', pid='123')))
        assert before['status'] == 'deferred'
        assert before['reason_code'] == 'waiting-for-attachment'
        assert event.read_text() == 'pending event'
        pending = runtime.read(runtime.pending_path('terminalobserver', canonical, '/dev/pts/1'))
        assert pending['server_identity'] == list(runtime.socket_identity(canonical))
        assert alias in pending['aliases']


def test_recycled_socket_cannot_acknowledge_a_previous_runtime_record(runtime, alias_server, monkeypatch):
    canonical, alias = alias_server
    record = dict(protocol=runtime.PROTOCOL, component='terminalobserver', socket=canonical,
                  server_identity=[-1, -1], fingerprint=runtime.fingerprint('terminalobserver'),
                  source=str(runtime.ROOT))
    monkeypatch.setattr(runtime, 'native', lambda *a: pytest.fail('stale socket inspected a process'))
    assert not runtime.live_record(record, 'terminalobserver', (Path('/record'), canonical, None))


def test_normal_attachment_rejects_user_hook_before_recovery_or_observer_migration(runtime, alias_server, monkeypatch):
    canonical, alias = alias_server
    pane = dict(identity='$1', session='dev-r-1', pane='%1', pane_pid='123', workspace='/work', dead='0')
    monkeypatch.setattr(runtime, 'panes', lambda *a: [pane])
    monkeypatch.setattr(runtime, 'same_owner', lambda *a: True)
    monkeypatch.setattr(runtime, 'hook_source', lambda *a: str(runtime.ROOT))
    def tmux(socket, *args):
        text = '/dev/pts/1|123|$1\n' if args[0] == 'list-clients' else 'client-attached[50100] run-shell "echo custom"\n'
        return subprocess.CompletedProcess(args, 0, text, '')
    monkeypatch.setattr(runtime, 'tmux', tmux)
    assert not runtime.adopt_attachment(canonical, '/dev/pts/1', '123')


def test_concurrent_alias_attachments_arm_one_original_pty_owner(runtime, alias_server, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    canonical, alias = alias_server
    runtime.server_inventory([canonical, alias])
    terminal = runtime.module('t_terminal_close.py')
    terminal['attached'].__globals__['runtime'] = lambda: runtime.__dict__
    monkeypatch.setitem(terminal['attached'].__globals__, 'stamp_attachment', lambda *a: None)
    monkeypatch.setattr(runtime, 'adopt_attachment', lambda *a: True)
    held = []
    def spawn(argv, **options):
        held.append(os.dup(options['pass_fds'][1]))
    monkeypatch.setattr(runtime.subprocess, 'Popen', spawn)
    master, slave = pty.openpty()
    tty = os.ttyname(slave)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda socket: terminal['attached'](socket, tty, '123'), (canonical, alias)))
        assert results == [0, 0]
        assert len(held) == 1
        assert runtime.lock_held(str(terminal['event_path'](canonical, tty)) + '.lock')
    finally:
        for fd in held: os.close(fd)
        os.close(master); os.close(slave)


def test_pending_clear_requires_verified_successor_fingerprint(runtime, alias_server, monkeypatch):
    canonical, alias = alias_server
    path = runtime.defer_migration('terminalobserver', canonical, '/dev/pts/1', 'legacy observer')
    record = dict(source=str(runtime.ROOT), fingerprint='old')
    monkeypatch.setattr(runtime, 'live_record', lambda *a, **kw: True)
    assert not runtime.complete_migration('terminalobserver', canonical, '/dev/pts/1', record, None)
    assert path.exists()
    record['fingerprint'] = runtime.fingerprint('terminalobserver')
    assert runtime.complete_migration('terminalobserver', canonical, '/dev/pts/1', record, None)
    assert not path.exists()


def test_old_format_record_is_reload_only_until_successor_incarnation(runtime, alias_server, monkeypatch):
    canonical, alias = alias_server
    record = dict(protocol=1, component='recoverywatcher', pid=123, birth='same',
                  source=str(runtime.ROOT), socket=canonical, token='token')
    monkeypatch.setattr(runtime, 'native', lambda *a: dict(birth='same', executable='/usr/bin/python3',
        argv=['python3', str(runtime.ROOT / 'libexec/t_recovery.py'), 'watch', canonical]))
    target = (Path('/record'), canonical, None)
    assert not runtime.live_record(record, 'recoverywatcher', target)
    assert runtime.live_record(record, 'recoverywatcher', target, allow_legacy=True)
    record['server_identity'] = list(runtime.socket_identity(canonical))
    assert runtime.live_record(record, 'recoverywatcher', target)


def test_attachment_cooperatively_adopts_old_format_held_observer(runtime, source, alias_server, monkeypatch):
    canonical, alias = alias_server
    process, master, path, base = _observer(runtime, source, alias, str(os.getpid()))
    before = runtime.read(path)
    legacy = dict(before); legacy.pop('server_identity')
    runtime.write(path, legacy)
    with (source / 'libexec/t_terminal_close.py').open('a') as stream:
        stream.write('\n# new attachment runtime\n')
    pane = dict(identity='$1', session='dev-r-1', pane='%1', pane_pid='123', workspace='/work', dead='0')
    terminal = runtime.module('t_terminal_close.py')
    terminal['attached'].__globals__['runtime'] = lambda: runtime.__dict__
    commands = terminal['hook_commands'](canonical, pane)
    def tmux(socket, *args):
        text = canonical + '\n'
        if args[0] == 'list-clients': text = before['tty'] + '|' + str(os.getpid()) + '|$1\n'
        elif args[0] == 'show-hooks': text = '\n'.join(key + ' ' + value for key, value in commands.items())
        elif args[0] == 'show-environment': text = 'T_RECOVERY_DISABLE=1\n'
        return subprocess.CompletedProcess(args, 0, text, '')
    monkeypatch.setattr(runtime, 'tmux', tmux)
    monkeypatch.setattr(runtime, 'panes', lambda *a: [pane])
    monkeypatch.setattr(runtime, 'same_owner', lambda *a: True)
    monkeypatch.setattr(runtime, 'hook_source', lambda *a: str(source))
    runtime.server_inventory([canonical, alias])
    try:
        assert terminal['attached'](canonical, before['tty'], str(os.getpid())) == 0
        after = runtime.read(path)
        assert after['fingerprint'] == runtime.fingerprint('terminalobserver')
        assert after['server_identity'] == list(runtime.socket_identity(canonical))
        assert all(after[key] == before[key] for key in ('pid', 'birth', 'token', 'terminal_fd', 'lock_fd'))
        # A fresh module still routes canonical detach to the retained alias FD.
        fresh = load_script(source / 'libexec/t_runtime.py', 'fresh_alias_runtime')
        terminal['notify_detached'].__globals__['runtime'] = lambda: fresh.__dict__
        routed = []
        terminal['notify_detached'].__globals__['atomic_write'] = lambda path, value: routed.append(path)
        assert terminal['notify_detached'](canonical, '$1', '%1', '123', before['tty']) == 0
        assert routed == [Path(str(base) + '.event')]
    finally:
        os.close(master)
        if process.poll() is None: process.terminate()
        process.wait(timeout=5)


def test_disappearing_hook_target_retains_scoped_receipt_and_reconciles_other_components(runtime, monkeypatch):
    pane = dict(identity='$1', session='dev-r-1', pane='%1', pane_pid='123', workspace='/work', dead='0')
    monkeypatch.setattr(runtime, 'server_inventory', lambda *a: [dict(socket='/private/server')])
    monkeypatch.setattr(runtime, 'socket_hints', lambda: [])
    monkeypatch.setattr(runtime, 'panes', lambda *a: [pane])
    monkeypatch.setattr(runtime, 'hook_source', lambda *a: str(runtime.ROOT))
    monkeypatch.setattr(runtime, 'same_owner', lambda *a: True)
    monkeypatch.setattr(runtime.Hooks, 'inspect', lambda *a: (_ for _ in ()).throw(ValueError('session disappeared')))
    class Discovery:
        name = 'discoverydaemon'
        def inspect(self, *a): return runtime.outcome('current')
    monkeypatch.setattr(runtime, 'ADAPTERS', (Discovery(),))
    result = runtime.reconcile()
    assert not result['failed']
    assert any(row['component'] == 'discoverydaemon' for row in result['results'])
    deferred = next(row for row in result['results'] if row['component'] == 'terminalhooks')
    assert deferred['server'] == '/private/server' and deferred['session'] == 'dev-r-1'
    assert deferred['reason_code'] == 'session-unavailable'


@pytest.mark.parametrize('birth', [[1795309000, 123456], '123456789'])
def test_legacy_alias_scan_deduplicates_native_darwin_and_linux_births(runtime, monkeypatch, birth):
    socket, alias = '/private/tmp/server', '/tmp/server'
    args = ['python3', str(runtime.ROOT / 'libexec/t_terminal_close.py'), 'watch', socket,
            '/dev/pts/1', '456', '7', '8']
    monkeypatch.setattr(runtime, 'socket_aliases', lambda *a: [alias, socket, socket])
    monkeypatch.setattr(runtime, 'command', lambda *a, **k: subprocess.CompletedProcess(a, 0,
        f'123 {os.getuid()} ' + ' '.join(args) + '\n', ''))
    monkeypatch.setattr(runtime, 'native', lambda *a: dict(pid=123, birth=deepcopy(birth), argv=list(args)))
    found = runtime.legacy_workers(socket, 'terminalobserver')
    assert len(found) == 1
    assert found[0]['birth'] == birth
    assert found[0]['pid'] == 123
    monkeypatch.setattr(runtime, 'socket_aliases', lambda *a: [socket, alias, socket])
    assert runtime.legacy_workers(socket, 'terminalobserver') == found
