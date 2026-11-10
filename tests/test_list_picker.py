"""Session lists always print tables; internal snapshot selection remains guarded."""
from types import SimpleNamespace
import shlex

import pytest
from test_agent_seam import zsh  # noqa: F401
from test_open_picker import picker_stub

SOURCE = ('sid1\t/code/api\tapi-10\tattached\tactive\tLatest task\tcodex\tgpt-test\t2026-10-23T16:41:07Z\n'
          'sid2\t/code/api\tapi-2\tdetached\tidle\tOlder task\tclaude\t-\t2026-10-22T16:41:07Z\n'
          'sid3\t/code/web\tweb-3\tapp\tnone\tOther task\tcodex\t-\t-\n')


def setup_list(t_mod, monkeypatch, remote=False):
    cfg = SimpleNamespace(hosts={}, repos={'api': '/code/api'}, worktree_root='/wt', repo_dir_for_cwd=lambda cwd: '/code/api')
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setenv('TERM', 'xterm')
    calls = []
    def discover(command):
        calls.append(command)
        return ''.join('mini\t' + line for line in SOURCE.splitlines(True)) if remote else SOURCE
    monkeypatch.setattr(t_mod, 'zsh_capture', discover)
    return cfg, SimpleNamespace(repo=None, all=False, remote=remote), calls


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('terminal', [False, True])
def test_list_always_prints_scoped_table(t_mod, monkeypatch, capsys, remote, terminal):
    cfg, args, calls = setup_list(t_mod, monkeypatch, remote)
    args.repo = 'api'
    monkeypatch.setattr(t_mod, '_columns', lambda: 200)
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda *a, **k: pytest.fail('list launched a picker'))
    assert t_mod.cmd_ls(cfg, args) == 0
    output = capsys.readouterr().out
    assert 'Latest task' in output and 'Older task' in output and 'LAST TURN' in output
    assert 'Other task' not in output
    assert calls == ['_dev_rows_all --details' if remote else '_dev_session_rows --details']


def test_empty_list_prints_empty_state(t_mod, monkeypatch, capsys):
    cfg, args, _ = setup_list(t_mod, monkeypatch)
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda command: '')
    assert t_mod.cmd_ls(cfg, args) == 0
    assert 'No dev sessions running' in capsys.readouterr().out


@pytest.mark.parametrize('argv', [['ls'], ['list'], ['session', 'list'], ['session', 'ls']])
def test_list_alias_terminal_mode_bypasses_update(t_mod, monkeypatch, argv):
    assert t_mod._command_readonly(argv, terminal=True)
    assert t_mod._command_readonly(argv, terminal=False)
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: pytest.fail('list checked updates'))
    assert t_mod._t_auto_update(argv) is None


@pytest.mark.parametrize('selection,rc', [('api-10', 0), ('', 130)])
def test_snapshot_helper_opens_exact_existing_identity(zsh, tmp_path, selection, rc):
    source = tmp_path / 'snapshot'
    source.write_text(''.join('mini\t' + line for line in SOURCE.splitlines(True)))
    env = picker_stub(tmp_path, selection)
    result = zsh('''
      _dev_session_rows() { print -u2 UNEXPECTED_SCAN; return 99; }
      _dev_rows_all() { print -u2 VERIFIED_ON_ENTER; cat ''' + shlex.quote(str(source)) + '''; }
      _t_open() { print -r -- "OPEN $*"; }
      _t_list_snapshot ''' + shlex.quote(str(source)) + ''' --remote
      print -r -- "RC=$?"
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'RC={rc}' in result.stdout
    assert 'UNEXPECTED_SCAN' not in result.stderr
    assert (result.stdout + result.stderr).count('VERIFIED_ON_ENTER') == (1 if selection else 0)
    if selection:
        assert 'OPEN api 10 --cli --host mini' in result.stdout
    else:
        assert 'OPEN' not in result.stdout
    rows = (tmp_path / 'picker.log').read_text().splitlines()
    assert len(rows) == 3
    assert 'Last turn:' in rows[0]


@pytest.mark.parametrize('change', ['missing', 'sid', 'state'])
def test_list_revalidates_batch_before_any_open(zsh, tmp_path, change):
    source = tmp_path / 'snapshot'
    source.write_text(''.join('local\t' + line for line in SOURCE.splitlines(True)))
    latest = SOURCE
    if change == 'missing':
        latest = ''.join(SOURCE.splitlines(True)[1:])
    elif change == 'sid':
        latest = SOURCE.replace('sid1', 'different-owner')
    else:
        latest = SOURCE.replace('attached', 'detached', 1)
    current = tmp_path / 'latest'
    current.write_text(latest)
    env = picker_stub(tmp_path, 'api-2,api-10')
    result = zsh('''
      _dev_session_rows() { cat ''' + shlex.quote(str(current)) + '''; }
      _dev_rows_all() { print -u2 UNEXPECTED_REMOTE_SCAN; return 99; }
      _t_open() { print UNEXPECTED_OPEN; }
      _t_list_snapshot ''' + shlex.quote(str(source)) + '''
      print -r -- "RC=$?"
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'RC=1' in result.stdout and 'changed while browsing' in result.stdout + result.stderr
    assert 'UNEXPECTED' not in result.stdout + result.stderr
