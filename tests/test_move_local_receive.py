"""Local move destinations exercise the real receive engine with isolated hosts."""
import json

import pytest

from test_agent_seam import zsh  # noqa: F401
from test_remote_commands import HOSTS, ROWS, fake_picker, output


RECEIVE = HOSTS + r'''
DEV_HANDOFF[codex]=t
mkdir -p "$HOME/code/api"
_dev_session_rows() {
  printf 'unrelated\t%s\tapi-9\tdetached\tactive\tOther\tclaude\n' "$HOME/code/api"
  if [[ $FAIL != stale || ! -f "$HOME/stop" ]]; then
    printf 'remote-id\t%s\tapi-2\tattached\tactive\tTask\tclaude\n' "$HOME/code/api"
  fi
}
_dev_rows_all() { _dev_session_rows | awk '{print "alpha\t" $0}'; }
_dev_remote_scan_with_timeout() { _dev_session_rows; }
_dev_repo_rows_command() { print '_dev_session_rows --details | tail -n 1'; }
ssh() {
  case $* in
    *_dev_worktree_beam_push*) print -u2 PUSH; [[ $FAIL != push ]]; return ;;
  esac
  [[ $* != *_tbeam_kill_owner* ]] || touch "$HOME/stop"
  local -a words=("${(@z)2}")
  ( eval "${(Q)words[3]}" )
}
_tbeam_kill_owner() { print -u2 "STOP $TB_SID"; [[ $FAIL != stop ]] || return 17; print dev-api-2; }
_tbeam_pull_transcript() { print -u2 "TRANSCRIPT $4"; [[ $FAIL != transcript ]]; }
_dev_beam_land_cwd() { [[ $FAIL != land ]] || return 1; print -r -- "$1"; }
_dev_worktree_beam_sync() { print -u2 SYNC; [[ $FAIL != sync ]]; }
_dev_slot_for_cwd() { print 'api 2'; }
_dev_resume_session() { print -u2 "START $3"; }
tmux() { [[ $1 != attach-session ]] || print ATTACH; }
'''


@pytest.mark.parametrize('flags', ['', '--detach'])
def test_selected_remote_conversation_lands_locally(zsh, tmp_path, flags):
    env = fake_picker(tmp_path, host='local')
    result = zsh(RECEIVE + f't session move api 2 {flags}; print rc=$?', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text, text
    assert 'STOP remote-id' in text and 'TRANSCRIPT remote-id' in text and 'START remote-id' in text
    assert 'unrelated' not in text
    assert ('ATTACH' in text) == (not flags)
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert len(logs) == 1 and 'local\tthis machine' in logs[0]['rows']
    assert any('current host: alpha' in arg for arg in logs[0]['args'])


@pytest.mark.parametrize('failure', ['stale', 'stop', 'push', 'transcript', 'land', 'sync'])
def test_receive_failures_never_start_local_owner(zsh, failure):
    result = zsh(RECEIVE + 't session move api 2 --host local --detach; print rc=$?', FAIL=failure)
    text = output(result)
    assert 'rc=1' in text and 'START ' not in text and 'ATTACH' not in text, text
    if failure == 'stale':
        assert 'STOP ' not in text and 'selected session changed or closed' in text
    if failure in ('stale', 'stop', 'push'):
        assert 'TRANSCRIPT ' not in text


@pytest.mark.parametrize('target', ['api 2', '--session remote-id'])
def test_explicit_source_and_local_destination_use_receive(zsh, target):
    result = zsh(RECEIVE + f't session move {target} --from alpha --host local --detach; print rc=$?')
    text = output(result)
    assert 'rc=0' in text and 'START remote-id' in text, text


@pytest.mark.parametrize('changed', ['id', 'cwd', 'label', 'agent'])
def test_receive_rechecks_picker_identity_before_stop(zsh, changed):
    replacement = {'id': 'different', 'cwd': '/different', 'label': 'api-9',
                   'agent': 'codex'}
    index = {'id': 1, 'cwd': 2, 'label': 3, 'agent': 7}[changed]
    result = zsh(RECEIVE + f'''
T_MOVE_EXPECT_ROW=$(printf 'remote-id\\t%s\\tapi-2\\tattached\\tactive\\tTask\\tclaude' "$HOME/code/api")
local -a expected=("${{(@ps:\\t:)T_MOVE_EXPECT_ROW}}")
expected[{index}]={replacement[changed]}
T_MOVE_EXPECT_ROW=${{(pj:\\t:)expected}}
_dev_pull alpha user@alpha '' '' '' 1 remote-id; print rc=$?
''')
    text = output(result)
    assert 'rc=1' in text and 'STOP ' not in text, text


def test_remote_batch_can_receive_locally_without_attaching(zsh, tmp_path):
    env = fake_picker(tmp_path, session='alpha/api-2,beta/api-2', host='local')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + '''
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t move --remote; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text and text.count('MOVE ') == 2, text
    assert '--detach|--from|alpha|--host|local' in text and '--detach|--from|beta|--host|local' in text


def test_local_source_excludes_and_rejects_local_destination(zsh, tmp_path):
    env = fake_picker(tmp_path, session='local/api-1', host='beta')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + '''
_t_beam() { print MOVE; }
t move --remote; print rc=$?
''', _tty=True, **env)
    assert 'rc=0' in output(result), output(result)
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert not any(row.startswith('local\t') for row in logs[1]['rows'])
    result = zsh(HOSTS + ROWS + '_t_beam() { print WRONG_MOVE; }; t move api 1 --host local --detach; print rc=$?')
    assert 'rc=2' in output(result) and 'WRONG_MOVE' not in output(result), output(result)


def test_exact_target_disambiguates_source_before_local_destination(zsh, tmp_path):
    env = fake_picker(tmp_path, session='alpha/api-2', host='local')
    setup = HOSTS + ROWS + 'DEV_REPOS[api]=/home/controller/code/api; '
    result = zsh(setup + '''
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t session move api 2; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text and '--from|alpha|--host|local' in text, text
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert len(logs) == 2
    assert [row.split('\t')[:2] for row in logs[0]['rows']] == [['alpha', 'api-2'], ['beta', 'api-2']]
    result = zsh(setup + '_t_beam() { print WRONG_MOVE; }; t move api 2 --host local --detach; print rc=$?')
    assert 'rc=1' in output(result) and 'WRONG_MOVE' not in output(result)


def test_local_host_picker_with_unregistered_explicit_source(zsh, tmp_path):
    env = fake_picker(tmp_path, host='local')
    result = zsh('REMOTE_HOSTS=(); _t_remote_host_pick raw-host move raw-host; print rc=$?', _tty=True, **env)
    assert 'local' in output(result) and 'rc=0' in output(result), output(result)


def test_nonterminal_local_receive_requires_detach(zsh):
    result = zsh(RECEIVE + 't move api 2 --host local; print rc=$?')
    assert 'rc=1' in output(result) and 'require --detach' in output(result), output(result)
    assert 'STOP ' not in output(result)
