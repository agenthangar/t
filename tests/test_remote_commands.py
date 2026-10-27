"""Remote orchestration uses isolated scanners and SSH, never real hosts."""
import json
import shlex

import pytest

from test_agent_seam import zsh  # noqa: F401
from test_move_picker import SETUP

HOSTS = '''
REMOTE_HOSTS=(alpha user@alpha beta user@beta beta-alias user@beta)
_dev_self_sid() { :; }
_t_infer_repo() { return 1; }
'''
ROWS = r'''
_dev_rows_all() {
  printf 'local\tlocal-id\t%s\tapi-1\tdetached\tactive\tLocal task\tcodex\tgpt-test\n' "$HOME/code/api"
  printf 'alpha\tremote-id\t/home/source/code/api\tapi-2\tattached\tactive\tRemote task\tclaude\topus-test\n'
  printf 'beta\tremote-id\t/home/dest/code/api\tapi-2\tdetached\tactive\tOther task\tcodex\tgpt-test\n'
  printf 'alpha\tdesktop-id\t/home/source/code/api\tapi-3\tapp\tactive\tDesktop\tcodex\tgpt-test\n'
  printf 'alpha\t-\t/home/source/code/api\tapi-4\tdetached\tidle\tNo id\tclaude\t-\n'
}
_t_remote_move_rows() { _dev_rows_all; }
'''


def fake_picker(tmp_path, session='alpha/api-2', host='beta'):
    stub = tmp_path / 'stubbin/fzf'
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
rows = sys.stdin.read().splitlines()
with Path(os.environ['PICK_LOG']).open('a') as log:
    log.write(json.dumps(dict(rows=rows,args=sys.argv[1:]))+'\\n')
is_host = any('host> ' in arg for arg in sys.argv)
choice = os.environ['PICK_HOST'] if is_host else os.environ['PICK_SESSION']
if choice == 'cancel': sys.exit(130)
if os.environ.get('CHANGE_ROWS'): Path(os.environ['CHANGE_ROWS']).touch()
for row in rows:
    fields = row.split('\\t')
    if (fields[0] if is_host else '/'.join(fields[:2])) == choice:
        print(row)
        break
''')
    stub.chmod(0o755)
    return dict(PICK_LOG=str(tmp_path / 'picker.jsonl'), PICK_HOST=host, PICK_SESSION=session)


def output(result):
    return result.stdout + result.stderr


@pytest.mark.parametrize('action,flags', [('open', '--new'), ('resume', '--days=all'), ('move', '--from=alpha --session=exact --detach')])
def test_remote_parser_and_help(t_mod, capsys, action, flags):
    words = ['remote', action, 'api', '2', '--host=beta', *shlex.split(flags)]
    args = t_mod.build_parser().parse_args(words)
    assert (args.verb, args.action, args.repo, args.slot, args.host) == ('remote', action, 'api', '2', 'beta')
    assert t_mod.main(['remote', action, '--help']) == 0
    help_text = capsys.readouterr().out
    assert f'usage: t remote {action}' in help_text and '--host' in help_text


def test_remote_group_requires_verb(t_mod):
    with pytest.raises(SystemExit):
        t_mod.build_parser().parse_args(['remote', 'api'])


@pytest.mark.parametrize('flags', ['api 2 --from alpha --host beta', '--host=beta --from=alpha api 2', '--session exact --from alpha --host beta'])
def test_session_move_relays_directly_with_resolved_aliases(zsh, flags):
    result = zsh(HOSTS + '''
ssh() { print -r -- "SSH ${(j:|:)@}"; return 17; }
_tbeam_kill_owner() { print -u2 WRONG_STOP; }
_tbeam_sync_transcript() { print -u2 WRONG_SYNC; }
''' + f't session move {flags} --detach; print rc=$?')
    text = output(result)
    assert 'rc=17' in text and 'SSH user@alpha|zsh -lic ' in text
    # Decode exactly the two shell quotation layers sent through SSH.
    command = shlex.split(text.split('SSH user@alpha|', 1)[1].splitlines()[0])[2]
    args = shlex.split(command)
    assert '_t_beam' in args and args[args.index('--host') + 1] == 'user@beta'
    assert '--detach' in args and 'WRONG_' not in text


@pytest.mark.parametrize('flags', ['--from alpha --host alpha', '--from beta --host beta-alias', '--here --host alpha', '--host', '--host=', '--from', '--from=', '--session', '--fg --detach --host beta'])
def test_move_invalid_inputs_fail_before_ssh(zsh, flags):
    result = zsh(HOSTS + 'ssh() { print WRONG_SSH; }; t session move api 2 ' + flags + '; print rc=$?')
    assert 'rc=2' in output(result) and 'WRONG_SSH' not in output(result)


def test_remote_move_selects_current_cross_host_identity_and_destination(zsh, tmp_path):
    env = fake_picker(tmp_path)
    result = zsh(HOSTS + ROWS + '''
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; print -r -- "EXPECTED $T_MOVE_EXPECT_ROW"; }
t remote move; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text, text
    assert 'MOVE --session|remote-id|--from|alpha|--host|beta' in text
    assert 'EXPECTED remote-id\t/home/source/code/api\tapi-2\tattached' in text
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert len(logs) == 2
    assert [row.split('\t')[:2] for row in logs[0]['rows']] == [
        ['local', 'api-1'], ['alpha', 'api-2'], ['beta', 'api-2']]
    assert all('desktop' not in row for row in logs[0]['rows'])
    assert all(not row.startswith('alpha\t') for row in logs[1]['rows'])
    assert '--no-multi' in logs[0]['args'] and '--no-multi' in logs[1]['args']


@pytest.mark.parametrize('which', ['session', 'host'])
def test_remote_move_cancellation_never_mutates(zsh, tmp_path, which):
    env = fake_picker(tmp_path, session='cancel' if which == 'session' else 'alpha/api-2', host='cancel')
    result = zsh(HOSTS + ROWS + '_t_beam() { print WRONG_MOVE; }; t remote move; print rc=$?', _tty=True, **env)
    assert 'rc=130' in output(result) and 'WRONG_MOVE' not in output(result)


@pytest.mark.parametrize('change', ['id', 'cwd', 'agent', 'desktop', 'closed'])
def test_remote_move_rechecks_after_destination_selection(zsh, tmp_path, change):
    env = fake_picker(tmp_path)
    env['CHANGE_ROWS'] = str(tmp_path / 'changed')
    changed = {
        'id': 'alpha\tchanged\t/home/source/code/api\tapi-2\tattached\tactive\tTask\tclaude',
        'cwd': 'alpha\tremote-id\t/elsewhere\tapi-2\tattached\tactive\tTask\tclaude',
        'agent': 'alpha\tremote-id\t/home/source/code/api\tapi-2\tattached\tactive\tTask\tcodex',
        'desktop': 'alpha\tremote-id\t/home/source/code/api\tapi-2\tapp\tactive\tTask\tclaude',
        'closed': '',
    }[change]
    result = zsh(HOSTS + ROWS + f'''
_t_remote_move_rows() {{ if [[ -f $CHANGE_ROWS ]]; then print -r -- {shlex.quote(changed)}; else _dev_rows_all; fi; }}
_t_beam() {{ print WRONG_MOVE; }}
t remote move --host beta; print rc=$?
''', _tty=True, **env)
    assert 'rc=1' in output(result) and 'selected session changed or closed' in output(result)
    assert 'WRONG_MOVE' not in output(result)


@pytest.mark.parametrize('mode', ['empty', 'failed', 'nonterminal'])
def test_remote_move_empty_failed_or_nonterminal(zsh, mode):
    setup = '_dev_rows_all() { :; };' if mode == 'empty' else (
        '_dev_rows_all() { return 1; };' if mode == 'failed' else ROWS)
    result = zsh(HOSTS + setup + '_t_beam() { print WRONG_MOVE; }; t remote move --host beta; print rc=$?')
    assert f'rc={0 if mode == "empty" else 1}' in output(result)
    assert 'WRONG_MOVE' not in output(result)


@pytest.mark.parametrize('action,args', [('open', 'api 3'), ('open', 'api --new'), ('resume', 'api 3 --days all')])
def test_remote_open_resume_delegate_quoted_worker_and_status(zsh, action, args):
    result = zsh(HOSTS + 'ssh() { print -r -- "SSH ${(j:|:)@}"; return 19; }; ' + f't remote {action} {args} --host beta; print rc=$?')
    text = output(result)
    assert 'rc=19' in text and 'SSH user@beta|zsh -lic ' in text
    command = shlex.split(text.split('SSH user@beta|', 1)[1].splitlines()[0])[2]
    words = shlex.split(command)
    assert 'T_NO_UPDATE_CHECK=1' in words
    assert ('_t_remote_open_local' if action == 'open' else '_t_resume') in words
    assert words[-len(shlex.split(args)):] == shlex.split(args)


def test_remote_open_empty_inventory_never_creates(zsh, tmp_path):
    env = fake_picker(tmp_path)
    result = zsh('''
_t_open_picker() { return 2; }
_t_open() { print WRONG_NEW; }
_t_remote_open_local api; print rc=$?
''', _tty=True, **env)
    assert 'rc=0' in output(result) and 'no current sessions' in output(result)
    assert 'WRONG_NEW' not in output(result)


@pytest.mark.parametrize('action', ['open', 'resume'])
def test_remote_host_picker_requires_explicit_enter_and_cancel(zsh, tmp_path, action):
    env = fake_picker(tmp_path, host='cancel')
    result = zsh(HOSTS + f'ssh() {{ print WRONG_SSH; }}; t remote {action} api; print rc=$?', _tty=True, **env)
    assert 'rc=130' in output(result) and 'WRONG_SSH' not in output(result)


def test_resume_host_conflict_is_preflight(zsh):
    result = zsh(HOSTS + '''
_t_resume_host_check fg:alpha beta api 2; print conflict=$?
_t_resume_host_check alpha alpha api 2; print same=$?
_t_resume_host_check beta beta-alias api 2; print alias=$?
_t_resume_host_check here beta api 2; print local=$?
''')
    text = output(result)
    assert 'conflict=1' in text and 'same=0' in text and 'alias=0' in text and 'local=1' in text
    assert 't remote move api 2 --from alpha --host beta' in text


@pytest.mark.parametrize('failure', ['stop', 'push', 'sync', 'receipt', 'ssh'])
def test_sender_failure_never_claims_destination_started(zsh, failure):
    overrides = {
        'stop': '_tbeam_kill_owner() { return 2; };',
        'push': '_dev_worktree_beam_push() { return 1; };',
        'sync': '_tbeam_sync_transcript() { return 1; };',
        'receipt': 'ssh() { [[ $* == *_tbeam_land ]] && print invalid; return 0; };',
        'ssh': 'ssh() { [[ $* == *_tbeam_land ]] && { print dev-api-2; return 23; }; return 0; };',
    }[failure]
    result = zsh(SETUP + '''
_dev_self_sid() { :; }
_codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
''' + overrides + 't session move --session codex-id --host mini --detach; print rc=$?')
    text = output(result)
    assert 'rc=1' in text and '✓ Running' not in text
    if failure in ('stop', 'push', 'sync'):
        assert 'zsh -lic _tbeam_land' not in text


@pytest.mark.parametrize('change', ['none', 'cwd', 'agent', 'id'])
def test_source_rechecks_original_picker_identity(zsh, change):
    cwd = str(zsh.home / 'code/api')
    expected = {'none': f'codex-id\t{cwd}\tapi-2\tattached\tactive\tTask\tcodex',
                'cwd': 'codex-id\t/old/worktree\tapi-2\tattached\tactive\tTask\tcodex',
                'agent': f'codex-id\t{cwd}\tapi-2\tattached\tactive\tTask\tclaude',
                'id': f'previous-id\t{cwd}\tapi-2\tattached\tactive\tTask\tcodex'}[change]
    result = zsh(SETUP + r'''
_dev_self_sid() { :; }
_codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
_dev_session_rows() { printf 'codex-id\t%s\tapi-2\tattached\tactive\tTask\tcodex\n' "$HOME/code/api"; }
''' + f'T_MOVE_EXPECT_ROW={shlex.quote(expected)} t session move --session codex-id --host mini --detach; print rc=$?')
    text = output(result)
    assert f'rc={0 if change == "none" else 1}' in text, text
    if change != 'none':
        assert 'STOP ' not in text and 'SYNC ' not in text and 'selected session changed' in text


def test_surviving_foreground_owner_blocks_landing(zsh):
    result = zsh(SETUP + r'''
_dev_self_sid() { :; }
_codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
_tbeam_kill_owner() { return 1; }
_dev_pid_for_sid() { print 123; }
_t_wait_for_exit() { :; }
kill() { return 0; }
t session move --session codex-id --host mini --detach; print rc=$?
''')
    text = output(result)
    assert 'rc=1' in text and 'did not exit' in text
    assert 'SYNC ' not in text and '_tbeam_land' not in text


def test_resume_batch_host_conflict_prevents_every_action(zsh, tmp_path):
    import re
    from test_resume_multiselect import FZF_ALL
    stub = tmp_path / 'stubbin/fzf'
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    for n in (3, 5):
        wt = zsh.home / 'code/.worktrees/api' / str(n)
        wt.mkdir(parents=True)
        project = zsh.home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(wt))
        project.mkdir(parents=True)
        (project / f'c{n}.jsonl').write_text(f'{{"type":"user","message":{{"content":"Task {n}"}}}}\n')
    wt3 = zsh.home / 'code/.worktrees/api/3'
    row = f'alpha\tc3\t{wt3}\tapi:fg\tattached\tactive\tLive work\tclaude'
    result = zsh(HOSTS + f'''
_dev_rows_all() {{ print -r -- {shlex.quote(row)}; }}
_t_resume_on_host() {{ print WRONG_RESUME; }}
_dev_remote_open() {{ print WRONG_ATTACH; }}
_t_resume api --live --host beta; print rc=$?
''', _tty=True, FZF_LOG=str(tmp_path / 'fzf.log'))
    text = output(result)
    assert 'rc=1' in text and 't remote move api' in text, text
    assert 'WRONG_' not in text and 'send-keys' not in zsh.log.read_text()


def test_remote_resume_worker_refuses_other_host_owner(zsh):
    result = zsh(HOSTS + '''
_T_RESUME_OWNER_HOST=here _T_RESUME_DESTINATION_HOST=user@beta _t_resume_host_check alpha '' api 2; print remote=$?
_T_RESUME_OWNER_HOST=here _t_resume_host_check here '' api 2; print local=$?
''')
    text = output(result)
    assert 'remote=1' in text and 'local=0' in text
    assert '--from alpha --host user@beta' in text


@pytest.mark.parametrize('source', ['local', 'alpha'])
def test_remote_explicit_source_target_skips_both_pickers(zsh, source):
    result = zsh(HOSTS + f'''
_t_remote_host_pick() {{ print -u2 WRONG_PICKER; return 99; }}
_t_remote_move_rows() {{ print -u2 WRONG_SCAN; return 99; }}
_t_beam() {{ print -r -- "MOVE $*"; }}
t remote move api 2 --from {source} --host beta --detach; print rc=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'MOVE api 2 --detach' in text and 'WRONG_' not in text


def test_selected_remote_source_receives_original_expected_identity(zsh, tmp_path):
    env = fake_picker(tmp_path)
    result = zsh(HOSTS + ROWS + '''
ssh() { print -r -- "SSH ${(j:|:)@}"; }
t remote move --host beta --detach; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text, text
    # The SSH command carries tabs using zsh $'...' quoting; inspect in the
    # sandboxed zsh parser rather than translating that syntax with shlex.
    assert 'T_MOVE_EXPECT_ROW=' in text and 'remote-id' in text and '/home/source/code/api' in text
    assert '_t_beam' in text and 'user@beta' in text


def test_host_picker_empty_and_nonterminal_fail_before_ssh(zsh):
    for setup, expected in [('REMOTE_HOSTS=();', 't hosts add'), (HOSTS, 'requires a terminal')]:
        result = zsh(setup + 'ssh() { print WRONG_SSH; }; t remote open api; print rc=$?')
        text = output(result)
        assert 'rc=1' in text and expected in text and 'WRONG_SSH' not in text


def test_resolved_destination_never_reinterpreted_as_source_alias(zsh):
    result = zsh(SETUP + r'''
REMOTE_HOSTS[user@beta]=wrong-host
_dev_self_sid() { :; }
_codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
_T_BEAM_HOST_IS_TARGET=1 t session move --session codex-id --host user@beta --detach; print rc=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'SSH user@beta ' in text and 'wrong-host' not in text


@pytest.mark.parametrize('args', ['typo', 'typo 2', 'typo --new'])
def test_remote_open_worker_rejects_unknown_repo_before_selection(zsh, args):
    result = zsh(HOSTS + """
_t_open_picker() { print WRONG_PICK; }
_t_open() { print WRONG_OPEN; }
""" + f'_t_remote_open_local {args}; print rc=$?')
    text = output(result)
    assert 'rc=1' in text and 'unknown repository: typo' in text
    assert 'WRONG_' not in text


@pytest.mark.parametrize('args', ['typo', 'typo 2'])
def test_explicit_destination_rejects_unknown_source_repo(zsh, args):
    result = zsh(SETUP + """
_t_move_pick() { print WRONG_PICK; }
""" + f'_t_beam {args} --host beta --detach; print rc=$?')
    text = output(result)
    assert 'rc=1' in text and 'unknown repository: typo' in text
    assert 'WRONG_' not in text and 'STOP ' not in text and 'SYNC ' not in text
