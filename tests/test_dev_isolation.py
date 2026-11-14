"""Development selection is local to a worktree and one command invocation."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
from types import SimpleNamespace

import pytest

from _install_helpers import _seed_worktree, git, run_install
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def selector():
    return load_script(ROOT / 'libexec/t_dev.py', 'dev_selector')


@pytest.fixture
def trees(tmp_path):
    main = tmp_path / 'main'
    _seed_worktree(main)
    shutil.copytree(ROOT / 'zsh', main / 'zsh', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'ui', main / 'ui', dirs_exist_ok=True)
    binary = main / 'bin/t'
    probe = '''if __name__ == "__main__" and sys.argv[1:] == ["__dev-probe"]:
    print(Path(__file__).resolve().parent.parent)
    sys.exit(0)
if __name__ == "__main__" and sys.argv[1:] == ["__dev-child"]:
    sys.exit(subprocess.run(["zsh", "-lic", 'print -r -- "plugin=$T_HOME"; command t __dev-probe'], cwd=os.environ['OUTSIDE']).returncode)
'''
    binary.write_text(binary.read_text().replace('CONFIG = os.path.join(', probe + 'CONFIG = os.path.join(', 1))
    git('init', '-q', '-b', 'main', cwd=main)
    git('config', 'user.email', 't@example.invalid', cwd=main)
    git('config', 'user.name', 'T', cwd=main)
    git('add', '-A', cwd=main)
    git('commit', '-qm', 'seed', '--no-verify', cwd=main)
    a, b = tmp_path / 'a', tmp_path / 'b'
    git('worktree', 'add', '-qb', 'dev/t-a', str(a), cwd=main)
    git('worktree', 'add', '-qb', 'cursor/b', str(b), cwd=main)
    home = tmp_path / 'home'
    (home / 'bin').mkdir(parents=True)
    (home / 'bin/t').symlink_to(main / 'bin/t')
    (home / '.zshrc').write_text(f'export PATH="$HOME/bin:$PATH"\nsource "{main}/t.plugin.zsh"\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COV_CORE_', 'T_DEV_'))}
    env.update(HOME=str(home), ZDOTDIR=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_CACHE_HOME=str(home / '.cache'), XDG_STATE_HOME=str(home / '.state'),
               PATH=str(home / 'bin') + ':' + env['PATH'], T_NO_UPDATE_CHECK='1', T_NO_AUTORELOAD='',
               T_RECOVERY_DISABLE='1', OUTSIDE=str(home), TMUX='')
    env.pop('T_LOCAL_RC', None)
    return main, a, b, home, env


def test_selector_uses_only_related_worktree_and_explicit_executable(selector, trees):
    main, a, b, home, env = trees
    (a / 'nested').mkdir()
    assert selector.select_root(main, a / 'nested', env) == a
    assert selector.select_root(main, b, env) == b
    assert selector.select_root(main, home, env) == main
    assert selector.select_root(a, b, env, explicit=True) == a
    foreign = home / 'foreign'
    _seed_worktree(foreign)
    git('init', '-q', cwd=foreign)
    assert selector.select_root(main, foreign, env) == main
    assert selector.select_root(home, a, env) == home
    (a / '.t-install-version').write_text('2')
    assert selector.select_root(main, a, env) == main
    (a / '.t-install-version').unlink()
    assert not selector.valid(a, main)
    assert not selector.valid(main, main)
    git('checkout', '-qb', 'topic/unscoped', cwd=b)
    assert selector.select_root(main, b, env) == main
    (b / 'libexec/t_dev.py').unlink()
    assert not selector.valid(b, main)


def test_selector_does_not_probe_unrelated_repositories(selector, trees, monkeypatch):
    main, a, b, home, env = trees
    foreign = home / 'foreign'
    (foreign / '.git').mkdir(parents=True)
    nested = foreign / 'nested'
    nested.mkdir()
    calls = []
    real_git = selector.git

    def record(root, *args):
        calls.append(Path(root))
        return real_git(root, *args)

    monkeypatch.setattr(selector, 'git', record)
    for cwd in (home, nested, main):
        assert selector.select_root(main, cwd, env) == main
    assert calls and set(calls) == {main}


def interrupt_git_once(home, env):
    """Interrupt only the selector process; never signal the test runner."""
    real_git = shutil.which('git')
    stub = home / 'bin/git'
    stub.write_text(f'''#!{sys.executable}
import os
from pathlib import Path
import signal
import sys
import time
marker = Path(os.environ['T_TEST_INTERRUPT_ONCE'])
if not marker.exists():
    marker.touch()
    if os.environ.get('T_TEST_WAIT_INTERRUPT'):
        time.sleep(20)
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(1)
    sys.exit(130)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
''')
    stub.chmod(0o755)
    env['T_TEST_INTERRUPT_ONCE'] = str(home / 'interrupted')


@pytest.mark.parametrize('entry', ['libexec/t_dev.py', 'bin/t'])
def test_source_selection_interrupt_exits_quietly(trees, entry):
    main, a, b, home, env = trees
    interrupt_git_once(home, env)
    args = [str(main)] if entry.endswith('.py') else ['--version']
    result = subprocess.run([sys.executable, str(main / entry), *args],
                            cwd=home, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 130, result.stderr
    assert result.stdout == result.stderr == ''


def test_interrupted_initial_load_keeps_retryable_shell_function(trees):
    main, a, b, home, env = trees
    interrupt_git_once(home, env)
    with (main / 'zsh/shim.zsh').open('a') as file:
        file.write('\n_t_dispatch() { print -r -- "DISPATCH:$*"; }\n')
    config = home / '.config/t'
    config.mkdir(parents=True)
    (config / 'local.zsh').write_text('print loaded >> "$HOME/loads"\n')
    script = f'''source {shlex.quote(str(main / 't.plugin.zsh'))}
print -r -- "source=$?"
whence -w t
t open example --new
print -r -- "retry=$?"
'''
    result = subprocess.run(['zsh', '-f', '-c', script], cwd=home, env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ''
    assert result.stdout.splitlines() == [
        'source=130', 't: function', 'DISPATCH:open example --new', 'retry=0']
    assert (home / 'loads').read_text().splitlines() == ['loaded']


@pytest.mark.parametrize('startup', ['source', 'login'])
def test_ctrl_c_during_startup_recovers_in_same_interactive_shell(trees, startup):
    main, a, b, home, env = trees
    interrupt_git_once(home, env)
    env.update(T_TEST_WAIT_INTERRUPT='1', PS1='READY> ', RPS1='', TERM='dumb',
               SHELL_SESSIONS_DISABLE='1')
    if startup == 'login':
        rc = home / '.zshrc'
        rc.write_text('PS1="READY> "; RPS1=""\n' + rc.read_text())
    with (main / 'zsh/shim.zsh').open('a') as file:
        file.write('\n_t_dispatch() { print -r -- "RECOVERED:$*"; }\n')
    master, slave = pty.openpty()

    def attach_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    argv = ['zsh', '-f', '-i'] if startup == 'source' else ['zsh', '-li']
    process = subprocess.Popen(argv, cwd=home, env=env,
                               stdin=slave, stdout=slave, stderr=slave,
                               preexec_fn=attach_terminal)
    os.close(slave)
    output = bytearray()

    def wait_for(predicate, *, exiting=False):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], .05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:  # Linux returns EIO after the slave closes.
                    pass
            if predicate():
                return
            assert exiting or process.poll() is None, output.decode(errors='replace')
        pytest.fail(output.decode(errors='replace'))

    try:
        if startup == 'source':
            wait_for(lambda: b'READY> ' in output)
            os.write(master, f'source {shlex.quote(str(main / "t.plugin.zsh"))}\n'.encode())
        wait_for(lambda: Path(env['T_TEST_INTERRUPT_ONCE']).exists())
        os.write(master, b'\x03')
        wait_for(lambda: output.count(b'READY> ') >= (2 if startup == 'source' else 1))
        os.write(master, b'whence -w t; t open example --new\n')
        wait_for(lambda: b'RECOVERED:open example --new' in output)
        os.write(master, b'unsetopt checkjobs checkrunningjobs; exit 0\n')
        wait_for(lambda: process.poll() is not None, exiting=True)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)
    assert b't: function' in output
    assert b'Traceback' not in output
    assert b'this verb runs in the shell' not in output


@pytest.mark.parametrize('reload', ['_t_reload', 't open example --new'])
def test_interrupted_reload_does_not_dispatch_stale_source(trees, reload):
    main, a, b, home, env = trees
    interrupt_git_once(home, env)
    script = f'''source {shlex.quote(str(main / 't.plugin.zsh'))} || exit
_t_dispatch() {{ print UNEXPECTED_DISPATCH; }}
rm "$T_TEST_INTERRUPT_ONCE"
{reload}
print -r -- "cancelled=$?"
whence -w t
'''
    # Load successfully first, then arm the one-shot failure for the reload.
    Path(env['T_TEST_INTERRUPT_ONCE']).touch()
    result = subprocess.run(['zsh', '-f', '-c', script], cwd=home, env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ''
    assert result.stdout.splitlines() == ['cancelled=130', 't: function']


def test_cli_scopes_are_independent_and_preserve_explicit_paths(trees):
    main, a, b, home, env = trees
    for cwd, expected in ((a, a), (b, b), (home, main)):
        result = subprocess.run([str(home / 'bin/t'), '__dev-probe'], cwd=cwd, env=env,
                                text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(expected)
    result = subprocess.run([str(a / 'bin/t'), '__dev-probe'], cwd=b, env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.stdout.strip() == str(a), result.stderr
    assert (home / 'bin/t').resolve() == main / 'bin/t'


def test_shell_helpers_switch_before_prompt_without_changing_path(trees):
    main, a, b, home, env = trees
    for root in (main, a, b):
        with (root / 'zsh/shim.zsh').open('a') as file:
            file.write(f'\n_t_dispatch() {{ print -r -- "helper={root}"; }}\n')
    script = f'''source "{main}/t.plugin.zsh"
original_path=$PATH
cd "{a}"; t __probe
_t_tree_is_live "{a}"; print "dev_protected=$?"
_t_tree_is_live "{main}"; print "main_protected=$?"
cd "{b}"; t __probe
cd "{home}"; t __probe
[[ $PATH == $original_path ]]; print "path_unchanged=$?"
print "scope=${{T_DEV_CONTEXT-unset}}"
'''
    result = subprocess.run(['zsh', '-f', '-c', script], cwd=home, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert [line for line in result.stdout.splitlines() if line.startswith('helper=')] == [
        f'helper={a}', f'helper={b}', f'helper={main}']
    for expected in ('dev_protected=1', 'main_protected=0', 'path_unchanged=0', 'scope=unset'):
        assert expected in result.stdout


def test_version_uses_worktree_on_first_call_after_public_cd(trees):
    main, a, b, home, env = trees
    worktrees = home / 'worktrees'
    destination = worktrees / main.name / '1'
    destination.parent.mkdir(parents=True)
    git('worktree', 'move', str(a), str(destination), cwd=main)
    git('commit', '--allow-empty', '-qm', 'distinct development revision',
        '--no-verify', cwd=destination)
    dev_sha = git('rev-parse', '--short', 'HEAD', cwd=destination).stdout.strip()
    main_sha = git('rev-parse', '--short', 'HEAD', cwd=main).stdout.strip()
    assert dev_sha != main_sha
    config = home / '.config/t'
    config.mkdir(parents=True, exist_ok=True)
    (config / 'local.zsh').write_text(
        f'DEV_REPOS[t]={shlex.quote(str(main))}\n'
        f'DEV_WORKTREE_ROOT={shlex.quote(str(worktrees))}\n')
    nested = destination / 'nested'
    nested.mkdir()
    # No precmd runs here: each first invocation must select its own source.
    script = f'''source {shlex.quote(str(main / 't.plugin.zsh'))}
t cd t 1 || exit $?
t --version || exit $?
print -r -- "helper=$T_HOME"
cd {shlex.quote(str(nested))}
t --version || exit $?
cd {shlex.quote(str(home))}
t --version || exit $?
print -r -- "helper=$T_HOME"
'''
    result = subprocess.run(['zsh', '-f', '-c', script], cwd=home, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(f't {dev_sha} (checkout)\n') == 2
    assert result.stdout.count(f'Source: {destination} (branch dev/t-a)') == 2
    assert result.stdout.count(f'Worktree: {destination} (branch dev/t-a)') == 2
    assert f'helper={destination}\n' in result.stdout
    assert f't {main_sha} (checkout)\n' in result.stdout
    assert f'Source: {main} (branch main)' in result.stdout
    assert 'Worktree: none (outside Git)' in result.stdout
    assert result.stdout.endswith(f'helper={main}\n')


def test_version_distinguishes_explicit_source_from_current_worktree(trees):
    main, a, b, home, env = trees
    git('commit', '--allow-empty', '-qm', 'distinct explicit revision',
        '--no-verify', cwd=a)
    sha = git('rev-parse', '--short', 'HEAD', cwd=a).stdout.strip()
    result = subprocess.run([str(a / 'bin/t'), '--version'], cwd=b, env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f't {sha} (checkout)\n')
    assert f'Source: {a} (branch dev/t-a)' in result.stdout
    assert f'Worktree: {b} (branch cursor/b)' in result.stdout


def test_child_login_shell_keeps_invocation_source_after_chdir(trees):
    main, a, b, home, env = trees
    result = subprocess.run([str(home / 'bin/t'), '__dev-child'], cwd=a, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert f'plugin={a}' in result.stdout
    assert result.stdout.splitlines()[-1] == str(a)


def test_two_dev_installations_never_change_shared_files(trees):
    main, a, b, home, env = trees
    before = {str(path.relative_to(home)): (path.readlink() if path.is_symlink() else path.read_bytes())
              for path in home.rglob('*') if path.is_file()}
    for source in (a, b):
        result = run_install(source, home, T_LINK_DEV='1', T_LINKS_ONLY='1')
        assert result.returncode == 0, result.stderr
        assert 'available automatically' in result.stdout
    after = {str(path.relative_to(home)): (path.readlink() if path.is_symlink() else path.read_bytes())
             for path in home.rglob('*') if path.is_file()}
    assert before == after


@pytest.mark.parametrize('output,owner,expected', [
    ('20 10 python\n10 1 zsh\n', '10', True),
    ('20 15 python\n15 10 tmux:server\n10 1 zsh\n', '10', False),
    ('20 20 python\nbad\n', '10', False), ('20 1 python\n', 'no', False)])
def test_invocation_pin_rejects_tmux_environment_and_stale_owners(selector, monkeypatch, output, owner, expected):
    monkeypatch.setattr(selector.os, 'getppid', lambda: 20)
    monkeypatch.setattr(selector.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    assert selector.invocation_owner(owner) is expected


@pytest.mark.parametrize('error', [OSError('missing'), subprocess.TimeoutExpired('ps', 2)])
def test_selector_handles_unavailable_process_and_git_tools(selector, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(selector.subprocess, 'run', fail)
    assert not selector.invocation_owner('42')
    assert selector.git('/missing', 'status') == ''


def test_pin_is_local_validated_and_removed_sources_fall_back(selector, trees, monkeypatch):
    main, a, b, home, env = trees
    original_owner = selector.invocation_owner
    monkeypatch.setattr(selector, 'invocation_owner', lambda owner: owner == 'active')
    env.update(T_DEV_CONTEXT=str(a), T_DEV_CONTEXT_OWNER='active')
    assert selector.select_root(main, home, env) == a
    env['T_DEV_CONTEXT_OWNER'] = 'stale'
    assert selector.select_root(main, home, env) == main
    env.update(T_DEV_CONTEXT=str(main), T_DEV_CONTEXT_OWNER='active')
    assert selector.select_root(main, a, env) == main
    env['T_DEV_CONTEXT'] = str(a)
    shutil.rmtree(a)
    assert selector.select_root(main, home, env) == main
    monkeypatch.setattr(selector, 'invocation_owner', original_owner)
    monkeypatch.setattr(selector.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert not selector.invocation_owner('42')


def test_explicit_path_worktree_keeps_cli_and_plugin_together(trees):
    main, a, b, home, env = trees
    env['PATH'] = str(a / 'bin') + ':' + env['PATH']
    result = subprocess.run(['zsh', '-f', '-c',
        f'source "{main}/t.plugin.zsh"; print "plugin=$T_HOME"; command t __dev-probe'],
        cwd=b, env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f'plugin={a}', str(a)]


def test_nested_release_remains_standalone(selector, trees):
    main, a, b, home, env = trees
    release = main / 'releases/v1'
    _seed_worktree(release)
    shutil.copytree(ROOT / 'zsh', release / 'zsh', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'ui', release / 'ui', dirs_exist_ok=True)
    (release / '.t-release-version').write_text('v1.0.0\n')
    assert selector.canonical(release) is None
    assert selector.select_root(release, b, env) == release
    env['PATH'] = str(release / 'bin') + ':' + env['PATH']
    result = subprocess.run(['zsh', '-f', '-c',
        f'source "{release}/t.plugin.zsh"; print "plugin=$T_HOME canonical=$_T_CANONICAL_HOME"'],
        cwd=b, env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f'plugin={release} canonical={release}'


def test_nosleep_follows_cd_immediately_and_keeps_normal_config(trees):
    main, a, b, home, env = trees
    (home / 'bin/nosleep').symlink_to(main / 'bin/nosleep')
    (a / 'nested').mkdir()
    config = home / '.config/nosleep/config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"grace": 47}\n')
    before = config.read_bytes()
    # Identify the actual function body and helper root, not just T_HOME. The
    # marker also checks that shell-local tuning survives switching runtimes.
    for tree in (main, a, b):
        runtime = tree / 'zsh/nosleep.zsh'
        runtime.write_text(runtime.read_text() + '\n_nosleep_help_for() { '
                           'print -r -- "runtime=$_NOSLEEP_ROOT override=$NOSLEEP_NET_MIN"; }\n')
    script = f'''source {shlex.quote(str(main / 't.plugin.zsh'))} || exit
NOSLEEP_NET_MIN=123
cd {shlex.quote(str(a))} && nosleep --help || exit
nosleep config --show || exit
cd nested && nosleep --help || exit
cd {shlex.quote(str(b))} && nosleep --help || exit
cd {shlex.quote(str(home))} && nosleep --help || exit
print -r -- "config=$XDG_CONFIG_HOME"
'''
    result = subprocess.run(['zsh', '-f', '-c', script], cwd=home, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert [line for line in result.stdout.splitlines() if line.startswith('runtime=')] == [
        f'runtime={tree} override=123' for tree in (a, a, b, main)]
    assert '47s before retrying' in result.stdout
    assert f'config={home / ".config"}' in result.stdout
    assert config.read_bytes() == before
    # The installed binary routes the runtime from nested directories without
    # shell startup or a per-worktree config copy.
    for cwd, expected in ((a / 'nested', a), (b, b), (home, main)):
        result = subprocess.run([str(home / 'bin/nosleep'), '--help'], cwd=cwd, env=env,
                                text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert f'runtime={expected} ' in result.stdout
    assert config.read_bytes() == before


def test_nosleep_shell_loads_uncommitted_edits_without_changing_directory(trees):
    main, a, _, home, env = trees
    runtime = a / 'zsh/nosleep.zsh'
    script = f'''source {shlex.quote(str(main / 't.plugin.zsh'))} || exit
cd {shlex.quote(str(a))} && nosleep --help >/dev/null || exit
print -r -- '_nosleep_help_for() {{ print fresh-runtime; }}' >> {shlex.quote(str(runtime))}
nosleep --help
'''
    result = subprocess.run(['zsh', '-f', '-c', script], cwd=home, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'fresh-runtime'
