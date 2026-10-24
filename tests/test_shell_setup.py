"""Fresh-home install/repair and read-only diagnostic regressions for #160."""
import argparse
import os
import pty
import select
import time
from pathlib import Path
import runpy
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = runpy.run_path(str(ROOT / 'libexec/t_shell_setup.py'))


def env_for(home):
    # Deliberately do not inherit user configuration, credentials or session data.
    return dict(HOME=str(home), PATH='/usr/bin:/bin:/usr/local/bin',
                XDG_CONFIG_HOME=str(home / '.config'), XDG_CACHE_HOME=str(home / '.cache'),
                XDG_STATE_HOME=str(home / '.state'), XDG_DATA_HOME=str(home / '.data'),
                ZDOTDIR=str(home), T_NO_UPDATE_CHECK='1', T_RECOVERY_DISABLE='1',
                SHELL_SESSIONS_DISABLE='1', TERM='dumb', TMUX_TMPDIR=str(home))


@pytest.mark.parametrize('original', ['', '# existing without newline', '# shell\nexport EDITOR=vi\n'])
def test_missing_and_existing_rc_preserved_and_idempotent(tmp_path, original):
    home = tmp_path / 'home with spaces'
    home.mkdir()
    rc = home / '.zshrc'
    if original:
        rc.write_text(original)
        rc.chmod(0o640)
    env = env_for(home)
    path, changed = SHELL['repair'](home, env)
    assert changed and path == rc
    assert rc.read_text().startswith(original)
    before, mtime = rc.read_bytes(), rc.stat().st_mtime_ns
    assert SHELL['repair'](home, env) == (rc, False)
    assert rc.read_bytes() == before and rc.stat().st_mtime_ns == mtime
    if original:
        assert rc.stat().st_mode & 0o777 == 0o640
    assert SHELL['state'](home, env)[1] == 'configured'


def test_custom_zdotdir_and_symlink_preserved(tmp_path):
    target = tmp_path / 'managed-elsewhere'
    target.write_text('# keep\n')
    rc_dir = tmp_path / 'zsh'
    rc_dir.mkdir()
    (rc_dir / '.zshrc').symlink_to(target)
    SHELL['repair'](tmp_path, {'ZDOTDIR': str(rc_dir)})
    assert (rc_dir / '.zshrc').is_symlink()
    assert target.read_text().startswith('# keep\n')
    assert not (tmp_path / '.zshrc').exists()


@pytest.mark.parametrize('content', [SHELL['BEGIN']+'\n# custom\n', SHELL['END']+'\n'])
def test_modified_block_refused_without_changes(tmp_path, content):
    rc = tmp_path / '.zshrc'
    rc.write_text(content)
    assert SHELL['state'](tmp_path, {})[1] == 'modified managed block'
    with pytest.raises(ValueError, match='modified managed block'):
        SHELL['repair'](tmp_path, {})
    assert rc.read_text() == content


def test_dangling_rc_refused(tmp_path):
    rc = tmp_path / '.zshrc'
    rc.symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError, match='dangling symlink'):
        SHELL['repair'](tmp_path, {})
    assert rc.is_symlink() and not rc.exists()


@pytest.mark.parametrize('argv', [['doctor', '--repair'], ['system', 'diagnose', '--repair']])
def test_repair_dispatch(t_mod, monkeypatch, argv):
    seen = []
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    monkeypatch.setitem(t_mod.IMPLEMENTED, 'doctor', lambda cfg, args: seen.append(args.repair) or 0)
    monkeypatch.setitem(t_mod.IMPLEMENTED, 'config', lambda *a: pytest.fail('opened config'))
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: pytest.fail('update prompt'))
    assert not t_mod._command_readonly(argv)
    assert t_mod.main(argv) == 0 and seen == [True]


@pytest.mark.parametrize('argv', [['doctor'], ['system', 'diagnose']])
@pytest.mark.parametrize('terminal', [False, True])
def test_diagnostics_are_read_only(t_mod, monkeypatch, tmp_path, capsys, argv, terminal):
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config'))
    monkeypatch.setenv('ZDOTDIR', str(tmp_path))
    monkeypatch.setattr(t_mod, '_perm_root', lambda: None)
    monkeypatch.setattr(t_mod, 'Config', lambda: argparse.Namespace(repos={}, worktree_root=str(tmp_path)))
    monkeypatch.setattr(t_mod, '_repair_shell_setup', lambda: pytest.fail('implicit repair'))
    monkeypatch.setitem(t_mod.IMPLEMENTED, 'config', lambda *a: pytest.fail('opened config'))
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: pytest.fail('update prompt'))
    def run(cmd, **kwargs):
        assert 'fetch' not in cmd and '-lic' not in cmd
        if cmd[0] != 'git':
            assert kwargs.get('stdin') == subprocess.DEVNULL
        return subprocess.CompletedProcess(cmd, 1, '', '')
    monkeypatch.setattr(t_mod.subprocess, 'run', run)
    (tmp_path / '.zshrc').write_text('t config\n')  # must never execute this
    before = list(tmp_path.iterdir())
    assert t_mod.main(argv) == 0
    assert list(tmp_path.iterdir()) == before
    assert (tmp_path / '.zshrc').read_text() == 't config\n'
    assert 't doctor --repair' in capsys.readouterr().out


def test_repair_fresh_interactive_shell_and_binary_refusal(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    env = env_for(home)
    binary = ROOT / 'bin/t'
    refused = subprocess.run([str(binary), 'open', 't', '--new'], env=env, capture_output=True, text=True)
    assert refused.returncode == 2 and 't doctor --repair' in refused.stderr
    # Exercise the real repair without running the expensive unrelated diagnostic probes.
    script = "import runpy; m=runpy.run_path("+repr(str(binary))+"); raise SystemExit(m['_repair_shell_setup']())"
    repaired = subprocess.run(['python3', '-c', script], env=env, capture_output=True, text=True)
    assert repaired.returncode == 0, repaired.stderr
    rc = home / '.zshrc'
    before = rc.read_bytes()
    assert subprocess.run(['python3', '-c', script], env=env, capture_output=True).returncode == 0
    assert rc.read_bytes() == before
    assert not (home / '.codex').exists() and not (home / '.claude').exists()
    # Real zsh startup and function dispatch; replace only session launch after startup.
    shell = subprocess.run(['zsh', '-lic',
        'whence -w t; _t_open() { print -r -- "OPEN:$*"; }; t open t --new'],
        env=env, cwd=home, capture_output=True, text=True)
    assert shell.returncode == 0, shell.stderr
    assert 't: function' in shell.stdout and 'OPEN:t --new' in shell.stdout


def test_successful_noop_install_repairs_but_plan_and_failure_do_not(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setenv('ZDOTDIR', str(tmp_path))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config'))
    monkeypatch.setattr(t_mod, 'ZSHRC_LOCAL', str(tmp_path / 'local.zsh'))
    monkeypatch.setattr(t_mod, '_install_probe', lambda: {
        a: {'version': '1.2.3', 'logged_in': True, 'bin': a, 'path': a}
        for a in t_mod.INSTALL_AGENTS})
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: False)
    cfg = argparse.Namespace(hosts={}, repos={})
    for argv in (['install'], ['install', '--dry-run'], ['install', '--status']):
        assert t_mod.cmd_install(cfg, t_mod.build_parser().parse_args(argv)) == (1 if argv == ['install'] else 0)
        assert not (tmp_path / '.zshrc').exists()
    assert t_mod._install_then_setup(cfg, argparse.Namespace(), 1, chain=False) == 1
    assert not (tmp_path / '.zshrc').exists()
    args = t_mod.build_parser().parse_args(['install', '-y', '--no-hosts'])
    assert t_mod.cmd_install(cfg, args) == 0
    assert (tmp_path / '.zshrc').is_file()


def test_repair_preserves_conflicting_binary(t_mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    binary = tmp_path / 'bin/t'
    binary.parent.mkdir()
    binary.write_text('foreign command')
    assert t_mod._repair_shell_setup() == 1
    assert binary.read_text() == 'foreign command'
    assert not (tmp_path / '.zshrc').exists()
    assert 'different t' in capsys.readouterr().err


def test_unreadable_encoding_is_reported_and_preserved(tmp_path):
    rc = tmp_path / '.zshrc'
    rc.write_bytes(b'# legacy byte: \xff\n')
    assert SHELL['state'](tmp_path, {}) == (rc, 'unreadable')
    with pytest.raises(UnicodeError):
        SHELL['repair'](tmp_path, {})
    assert rc.read_bytes() == b'# legacy byte: \xff\n'


def test_helper_cli_error_and_success(tmp_path):
    env = env_for(tmp_path)
    helper = ROOT / 'libexec/t_shell_setup.py'
    first = subprocess.run(['python3', str(helper)], env=env, capture_output=True, text=True)
    assert first.returncode == 0 and 'source ' in first.stdout
    (tmp_path / '.zshrc').write_text(SHELL['BEGIN']+'\n')
    rejected = subprocess.run(['python3', str(helper)], env=env, capture_output=True, text=True)
    assert rejected.returncode == 1 and 'modified managed block' in rejected.stderr


def test_broken_command_link_repaired(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setenv('ZDOTDIR', str(tmp_path))
    binary = tmp_path / 'bin/t'
    binary.parent.mkdir()
    binary.symlink_to(tmp_path / 'removed-release/bin/t')
    assert t_mod._repair_shell_setup() == 0
    assert binary.resolve() == ROOT / 'bin/t'


def test_homebrew_repair_uses_stable_path(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setenv('ZDOTDIR', str(tmp_path))
    opt = tmp_path / 'opt/t/libexec'
    opt.parent.mkdir(parents=True)
    opt.symlink_to(ROOT, target_is_directory=True)
    monkeypatch.setattr(t_mod, '_t_homebrew', lambda root: {'opt_libexec': str(opt)})
    assert t_mod._repair_shell_setup() == 0
    assert os.readlink(tmp_path / 'bin/t') == str(opt / 'bin/t')


def test_missing_shell_setup_is_a_finding(t_mod):
    findings = t_mod._doctor_findings({'shell_setup': 'missing'})
    assert len(findings) == 1 and 't doctor --repair' in findings[0]
    assert 'nothing suspicious' not in findings[0]
    assert t_mod._doctor_findings({'shell_setup': 'configured'}) == ['✓ nothing suspicious found']


@pytest.mark.parametrize('argv', [['doctor'], ['system', 'diagnose']])
def test_plain_doctor_in_terminal_does_not_open_config(tmp_path, argv):
    # Real tty descriptors exercise the reported interactive case; no mocked
    # dispatcher or non-TTY config early exit can hide an accidental menu.
    home = tmp_path / 'home'
    home.mkdir()
    socket_root = tmp_path / 'tmux-runtime'
    socket_root.mkdir()
    env = env_for(home)
    # Exercise a gh version probe with the same startup side effects as recent
    # GitHub CLI releases, regardless of which tools this machine has installed.
    tools = tmp_path / 'tools'
    tools.mkdir()
    gh = tools / 'gh'
    gh.write_text("""#!/bin/sh
if [ "$GH_TELEMETRY" != disabled ] || [ "$GH_NO_UPDATE_NOTIFIER" != 1 ]; then
    mkdir -p "$XDG_STATE_HOME/gh"
    printf 'probe state' > "$XDG_STATE_HOME/gh/state"
fi
printf 'gh version 2.103.2\\n'
""")
    gh.chmod(0o755)
    env['PATH'] = str(tools) + ':/usr/bin:/bin'
    env['GH_TELEMETRY'] = 'enabled'  # doctor must override this for its probe
    env['GH_NO_UPDATE_NOTIFIER'] = '0'
    # Apple's system Python caches stdlib bytecode under HOME before t starts.
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # A real tmux client may create tmux-$UID even for read-only list-sessions.
    # Keep its isolated runtime separate from the HOME/config snapshot.
    env['TMUX_TMPDIR'] = str(socket_root)
    rc = home / '.zshrc'
    rc.write_text('print CONFIG_STARTUP_EXECUTED; t config\n')
    master, slave = pty.openpty()
    process = subprocess.Popen([str(ROOT / 'bin/t'), *argv], env=env, cwd=tmp_path,
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    chunks = []
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # Linux reports EIO after the last slave closes.
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            elif process.poll() is not None:
                break
        assert process.wait(timeout=1) == 0, b''.join(chunks).decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    output = b''.join(chunks).decode(errors='replace')
    assert 'agent session diagnostics' in output and 'Findings' in output
    assert 'Review local settings' not in output and 'CONFIG_STARTUP_EXECUTED' not in output
    assert 'gh 2.103.2' in output
    assert list(home.iterdir()) == [rc]
    assert rc.read_text() == 'print CONFIG_STARTUP_EXECUTED; t config\n'


@pytest.mark.parametrize('legacy', SHELL['LEGACY'])
def test_legacy_setup_preserved_and_loaded_once(tmp_path, legacy):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'bin').mkdir()
    (home / 'bin/t').symlink_to(ROOT / 'bin/t')
    rc = home / '.zshrc'
    rc.write_text(legacy + '\n')
    config = home / '.config/t/local.zsh'
    config.parent.mkdir(parents=True)
    config.write_text('print loaded >> "$HOME/loads"\n')
    env = env_for(home)
    assert SHELL['state'](home, env)[1] == 'configured'
    assert SHELL['repair'](home, env) == (rc, False)
    assert rc.read_text() == legacy + '\n'
    shell = subprocess.run(['zsh', '-lic', 'whence -w t'], env=env, cwd=home,
                           capture_output=True, text=True, timeout=10)
    assert shell.returncode == 0 and 't: function' in shell.stdout, shell.stderr
    assert (home / 'loads').read_text().splitlines() == ['loaded']


@pytest.mark.parametrize('confirm', [True, False])
def test_interactive_install_reviews_shell_repair_when_agents_present(t_mod, tmp_path, monkeypatch, confirm):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setenv('ZDOTDIR', str(tmp_path))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config'))
    monkeypatch.setattr(t_mod, 'ZSHRC_LOCAL', str(tmp_path / 'local.zsh'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, '_install_probe', lambda: {
        a: {'version': '1.2.3', 'logged_in': True, 'bin': a, 'path': a}
        for a in t_mod.INSTALL_AGENTS})
    def review(items, plan):
        shell = next(item for item in items if item.get('kind') == 'shell')
        assert shell['checked']
        assert [step['step'] for step in plan()] == ['shell']
        assert 'preserve existing settings' in plan()[0]['label']
        return confirm
    monkeypatch.setattr(t_mod, '_install_wizard', review)
    monkeypatch.setattr(t_mod, '_install_relink', lambda: pytest.fail('unrequested agent integrations'))
    cfg = argparse.Namespace(hosts={}, repos={})
    args = t_mod.build_parser().parse_args(['install', '--no-hosts'])
    assert t_mod.cmd_install(cfg, args) == (0 if confirm else 130)
    assert (tmp_path / '.zshrc').exists() == confirm
