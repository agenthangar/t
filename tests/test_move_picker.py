"""Move selects current owners, never a transcript-history resume candidate."""
import json

import pytest

from test_agent_seam import zsh  # noqa: F401 — isolated HOME, process table and tmux


SETUP = r'''
  DEV_WORKTREE_ROOT="$HOME/work trees"
  _dev_self_sid() { :; }
  _claude_sessions_fzf() { print -u2 UNEXPECTED_HISTORY; return 99; }
  _dev_session_rows() {
    printf 'codex-id\t%s\tapi-2\tattached\tactive\tCurrent Codex task\tcodex\tgpt-test\n' "$DEV_WORKTREE_ROOT/api/api-2"
    printf 'claude-id\t%s\tapi-3\tdetached\tactive\tCurrent Claude task\tclaude\topus-test\n' "$HOME/code/api"
    printf 'fg-id\t%s\tweb:fg-id\tattached\tactive\tForeground task\tclaude\topus-test\n' "$HOME/code/web"
    printf 'desktop-id\t%s\tapi-4\tapp\tactive\tDesktop task\tcodex\tgpt-test\n' "$HOME/code/api"
    printf -- '-\t%s\tapi-5\tdetached\tidle\tNo conversation\tclaude\t-\n' "$HOME/code/api"
  }
  rsync() { print -u2 UNEXPECTED_RSYNC; return 99; }
  ssh() { print -r -- "SSH $*"; }
  _dev_worktree_remote_accept() { print -r -- "ACCEPT $*"; }
  _dev_app_slot_reserved() { return 1; }
  _dev_codex_rollout_integrity() { print -r -- "INTEGRITY $*"; }
  _tbeam_kill_owner() { print -u2 -- "STOP $TB_SID"; print dev-api-2; }
  _dev_worktree_beam_push() { print -r -- "PUSH $*"; }
  _tbeam_sync_transcript() { print -r -- "SYNC $*"; }
  _tpush_claude_pid() { :; }
'''


def picker_stub(tmp_path, label='api-2'):
    stub = tmp_path / 'stubbin/fzf'
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
rows = sys.stdin.read().splitlines()
Path(os.environ['PICKER_LOG']).write_text(json.dumps({'rows': rows, 'args': sys.argv[1:]}))
if os.environ.get('CHANGE_ROWS'):
    Path(os.environ['CHANGE_ROWS']).touch()
if os.environ['PICK_LABEL'] == 'cancel':
    sys.exit(130)
if os.environ['PICK_LABEL'] == 'error':
    sys.exit(2)
for row in rows:
    if row.split('\\t')[1] == os.environ['PICK_LABEL']:
        print(row)
        break
''')
    stub.chmod(0o755)
    return {'PICKER_LOG': str(tmp_path / 'picker.json'), 'PICK_LABEL': label}


@pytest.mark.parametrize('args,location,inside,label,expected', [
    ('', 'outside', False, 'api-2', ['api-2', 'api-3', 'web:fg-id']),
    ('', 'api', False, 'api-2', ['api-2', 'api-3']),
    ('api', 'outside', False, 'api-3', ['api-2', 'api-3']),
    ('--all', 'api', True, 'web:fg-id', ['api-2', 'api-3', 'web:fg-id']),
    ('--pick', 'api', True, 'api-3', ['api-2', 'api-3']),
    ('api 2 --pick', 'outside', False, 'api-2', ['api-2']),
])
def test_move_picker_uses_current_sessions(zsh, tmp_path, args, location, inside, label, expected):
    env = picker_stub(tmp_path, label)
    cwd = '$HOME/work trees/api/api-9' if location == 'api' else '$HOME/outside'
    self_owner = '_dev_self_sid() { print self-id; };' if inside else ''
    result = zsh(SETUP + f'''
      mkdir -p "{cwd}"; cd "{cwd}"
      {self_owner}
      t session move {args} --host mini
      print -r -- "rc=$?"
    ''', _tty=True, **env)
    assert 'rc=0' in result.stdout, result.stdout
    log = json.loads((tmp_path / 'picker.json').read_text())
    assert [row.split('\t')[1] for row in log['rows']] == expected
    assert '--no-multi' in log['args']
    assert '--header=Enter move · Esc\nCurrent local sessions' in log['args']
    assert 'UNEXPECTED' not in result.stdout
    sid, agent = ('codex-id', 'codex') if label == 'api-2' else (
        ('claude-id', 'claude') if label == 'api-3' else ('fg-id', 'claude'))
    assert f'STOP {sid}' in result.stdout
    assert f'mini {agent} {sid}' in result.stdout
    assert f'TB_SID={sid} TB_AGENT={agent}' in result.stdout
    assert 'zsh -lic _tbeam_land' in result.stdout
    if agent == 'codex':
        assert 'INTEGRITY codex-id' in result.stdout
        assert 'work trees/api/api-2 mini codex codex-id' in result.stdout


@pytest.mark.parametrize('scenario,rc', [('cancel', 130), ('error', 2), ('empty', 0), ('nonterminal', 1)])
def test_move_picker_cancel_empty_and_nonterminal(zsh, tmp_path, scenario, rc):
    env = picker_stub(tmp_path, scenario)
    extra = '_dev_session_rows() { :; };' if scenario == 'empty' else ''
    result = zsh(SETUP + extra + 't session move api --host mini; print -r -- "rc=$?"',
                 _tty=scenario != 'nonterminal', **env)
    output = result.stdout + result.stderr
    assert f'rc={rc}' in output, output
    assert all(action not in output for action in ('STOP ', 'SYNC ', 'SSH ', 'UNEXPECTED'))
    if scenario == 'empty':
        assert 'no current CLI conversations' in output
    elif scenario == 'nonterminal':
        assert 'selection requires a terminal and fzf' in output
    assert (tmp_path / 'picker.json').exists() == (scenario in ('cancel', 'error'))


@pytest.mark.parametrize('change', ['closed', 'id', 'cwd', 'agent', 'desktop'])
def test_move_refuses_changed_selection_before_stopping_owner(zsh, tmp_path, change):
    env = picker_stub(tmp_path)
    changed = tmp_path / 'changed'
    env['CHANGE_ROWS'] = str(changed)
    row = {
        'closed': '',
        'id': 'another-id\t/code/api\tapi-2\tattached\tactive\tTask\tcodex',
        'cwd': 'codex-id\t/other/api\tapi-2\tattached\tactive\tTask\tcodex',
        'agent': 'codex-id\t/code/api\tapi-2\tattached\tactive\tTask\tclaude',
        'desktop': 'codex-id\t/code/api\tapi-2\tapp\tactive\tTask\tcodex',
    }[change]
    # Keep unrelated identity fields identical so each refusal tests its named change.
    row = row.replace('/code/api', str(zsh.home / 'work trees/api/api-2'))
    import shlex
    result = zsh(SETUP + f'''
      functions[_move_initial_rows]=$functions[_dev_session_rows]
      _dev_session_rows() {{
        if [[ -f $CHANGE_ROWS ]]; then print -r -- {shlex.quote(row)}
        else _move_initial_rows; fi
      }}
      t session move api --host mini; print -r -- "rc=$?"
    ''', _tty=True, **env)
    assert 'rc=1' in result.stdout and 'selected session changed or closed' in result.stdout
    assert 'STOP ' not in result.stdout and 'SYNC ' not in result.stdout


@pytest.mark.parametrize('args', ['api 2', '2', '--session codex-id', ''])
def test_explicit_and_current_targets_bypass_picker(zsh, args):
    result = zsh(SETUP + r'''
      _t_infer_repo() { print api; }
      _t_move_pick() { print -u2 UNEXPECTED_PICKER; return 99; }
      _dev_self_sid() { print codex-id; }
      _dev_self_agent() { print codex; }
      _dev_agent_of_session() { print codex; }
      _dev_session_sid() { print codex-id; }
      _codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
      tmux() {
        [[ $1 == has-session ]] && return 0
        [[ $1 == display-message ]] && print -r -- "$HOME/code/api"
      }
    ''' + f't session move {args} --host mini; print -r -- "rc=$?"')
    assert 'rc=0' in result.stdout, result.stdout + result.stderr
    assert 'mini codex codex-id' in result.stdout
    assert 'UNEXPECTED' not in result.stdout + result.stderr


def test_move_help_explains_current_picker_and_scope(t_mod, capsys):
    assert t_mod.main(['session', 'move', '--help']) == 0
    help_text = ' '.join(capsys.readouterr().out.split())
    for text in ('current local CLI conversation', 'named or current repository',
                 'all repositories outside a checkout', 'not saved history',
                 '--pick', 'scripts must name a target'):
        assert text in help_text
