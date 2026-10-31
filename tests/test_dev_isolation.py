"""Development selection is local to a worktree and one command invocation."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
