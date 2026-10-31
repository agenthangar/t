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
    key = fields[0] if is_host else '/'.join(fields[:2])
    for wanted in choice.split(','):
        if key == wanted or (not is_host and wanted == '#' + fields[-1]):
            print(row)
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
    assert f'usage: t remote {action}' in help_text
    assert ('--host' if action == 'move' else '[host]') in help_text


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
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + '''
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
    assert '--multi' in logs[0]['args'] and '--no-multi' in logs[1]['args']
    assert any('Move this session to which host?' in arg and
               'Destination host · current host: alpha' in arg for arg in logs[1]['args'])


# Exercise the real fan-out and host-side selector worker. The source registers
# the checkout under a different alias and path than the controller.
REMOTE_ONLY = 'DEV_HANDOFF[codex]=t; ' + HOSTS + r'''
_dev_session_rows_impl() {
  printf 'unrelated\t%s\tother-1\tdetached\tactive\tOther\tcodex\n' "$HOME/code/other"
  if [[ -n ${DEV_REPOS[remote-auth]:-} ]]; then
    printf 'auth-id\t%s\tremote-auth-1\tattached\tactive\tAuth\tcodex\n' "$HOME/foreign/auth"
  fi
}
_dev_remote_scan_with_timeout() {
  local -A DEV_REPOS=(remote-auth "$HOME/foreign/auth")
  [[ $1 == user@alpha ]] || DEV_REPOS=()
  [[ ${AMBIGUOUS:-} == 1 && $1 == user@beta ]] && DEV_REPOS=(a "$HOME/a/auth" b "$HOME/b/auth")
  local -a worker
  worker=("${(@z)2}")
  eval "${(Q)worker[3]}"
}
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; print -r -- "EXPECTED $T_MOVE_EXPECT_ROW"; }
'''


@pytest.mark.parametrize('command', ['t remote move', 't session move', 't move'])
@pytest.mark.parametrize('host', ['local', 'beta', 'cancel'])
def test_remote_only_repo_reaches_destination_picker(zsh, tmp_path, host, command):
    env = fake_picker(tmp_path, host=host)
    result = zsh(REMOTE_ONLY + command + ' auth 1; print rc=$?', _tty=True, **env)
    text = output(result)
    assert f'rc={130 if host == "cancel" else 0}' in text, text
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert len(logs) == 1  # unique source goes straight to the destination picker
    assert all(not row.startswith('alpha\t') for row in logs[0]['rows'])
    assert 'local\tthis machine' in logs[0]['rows']
    assert any('current host: alpha' in arg for arg in logs[0]['args'])
    if host == 'cancel':
        assert 'MOVE ' not in text
    else:
        assert f'MOVE --session|auth-id|--from|alpha|--host|{host}' in text
        assert 'EXPECTED auth-id\t' in text and 'remote-auth-1' in text
    assert 'unrelated' not in text


@pytest.mark.parametrize('args,ambiguous,expected', [
    ('auth 1', '1', 2),
    ('missing 1', '0', 0),
    ('auth --from local', '0', 1),
])
def test_remote_only_repo_scope_failures_never_move(zsh, args, ambiguous, expected):
    result = zsh(REMOTE_ONLY + f't remote move {args}; print rc=$?', AMBIGUOUS=ambiguous)
    text = output(result)
    assert f'rc={expected}' in text, text
    assert 'MOVE ' not in text and 'host selection requires' not in text


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
    expected = shlex.split(args)
    if action == 'resume':
        expected[0] = '$t_resume_repo'
        assert 'command t __resume-alias' in command
    assert words[-len(expected):] == expected


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
    result = zsh(HOSTS + f'ssh() {{ print WRONG_SSH; }}; t remote {action}; print rc=$?', _tty=True, **env)
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
    assert 't session move api 2 --from alpha --host beta' in text


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
DEV_HANDOFF[codex]=t
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
DEV_HANDOFF[codex]=t
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
    assert 'rc=1' in text and 't session move api' in text, text
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
        result = zsh(setup + 'ssh() { print WRONG_SSH; }; t remote open; print rc=$?')
        text = output(result)
        assert 'rc=1' in text and expected in text and 'WRONG_SSH' not in text


def test_resolved_destination_never_reinterpreted_as_source_alias(zsh):
    result = zsh(SETUP + r'''
DEV_HANDOFF[codex]=t
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


@pytest.mark.parametrize('action,args,heading,detail', [
    ('open', '', 'Open a session on which host?', ''),
    ('open', '--new', 'Start a new session on which host?', ''),
    ('resume', '', 'Resume saved work on which host?', 'Browse this host’s saved conversations'),
])
def test_remote_host_picker_explains_action(zsh, tmp_path, action, args, heading, detail):
    env = fake_picker(tmp_path)
    result = zsh(HOSTS + f'_t_remote_open_inventory() {{ :; }}; ssh() {{ :; }}; t remote {action} {args}; print rc=$?', _tty=True, **env)
    assert 'rc=0' in output(result), output(result)
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert len(logs) == 1
    assert any(heading in arg and detail in arg and 'SSH hosts · alias and target' in arg
               for arg in logs[0]['args'])
    assert 'alpha\tuser@alpha' in logs[0]['rows']


@pytest.mark.parametrize('action', ['open', 'resume', 'move'])
def test_explicit_remote_host_bypasses_picker(zsh, tmp_path, action):
    env = fake_picker(tmp_path)
    args = '--from alpha --session exact --detach' if action == 'move' else 'api 3'
    result = zsh(HOSTS + f'ssh() {{ :; }}; t remote {action} {args} --host beta; print rc=$?', **env)
    assert 'rc=0' in output(result), output(result)
    assert not (tmp_path / 'picker.jsonl').exists()


@pytest.mark.parametrize('action', ['open', 'resume'])
@pytest.mark.parametrize('argv,expected', [
    ('beta api 3', ('beta', 'api', '3')),
    ('beta --host beta 3', ('beta', 'beta', '3')),
    ('beta', ('beta', None, None)),
    ('beta api', ('beta', 'api', None)),
    ('api 3 --host beta', ('beta', 'api', '3')),
    ('--host=beta api 3', ('beta', 'api', '3')),
])
def test_remote_host_first_parser(t_mod, action, argv, expected):
    args = t_mod.build_parser().parse_args(['remote', action, *shlex.split(argv)])
    assert (args.host, args.repo, args.slot) == expected


@pytest.mark.parametrize('action', ['open', 'resume'])
@pytest.mark.parametrize('host', ['beta', 'user@raw-host'])
def test_remote_host_first_direct_delegation(zsh, action, host):
    result = zsh(HOSTS + 'ssh() { print -r -- "SSH ${(j:|:)@}"; return 19; }; '
                 + f't remote {action} {host} api 3; print rc=$?')
    text = output(result)
    assert 'rc=19' in text
    assert f'SSH {"user@beta" if host == "beta" else host}|zsh -lic ' in text
    assert ('_t_remote_open_local' if action == 'open' else '_t_resume') in text


@pytest.mark.parametrize('args', ['api', 'api 2', 'api --new', 'beta api x',
                                   'beta api 2 extra', 'local api 2',
                                   'beta api 2 --host alpha'])
def test_remote_host_first_rejects_ambiguous_or_invalid_targets(zsh, args):
    result = zsh(HOSTS + 'REMOTE_HOSTS[api]=user@api; ssh() { print WRONG_SSH; }; '
                 + f't remote open {args}; print rc=$?')
    assert 'rc=2' in output(result) and 'WRONG_SSH' not in output(result)


@pytest.mark.parametrize('args,expected', [('beta', 'api'), ('beta api', 'api'),
                                           ('beta --new', 'api'), ('beta 3', 'api')])
def test_remote_host_first_infers_and_scopes(zsh, args, expected):
    result = zsh(HOSTS + '''
_t_infer_repo() { print api; }
_t_open_picker() { print -r -- "PICK $1 HOST $6"; }
ssh() { print -r -- "SSH ${(j:|:)@}"; }
''' + f't remote open {args}; print rc=$?', _tty=True)
    text = output(result)
    assert 'rc=0' in text, text
    assert expected in text
    assert 'HOST beta' in text or 'SSH -t|user@beta|' in text


@pytest.mark.parametrize('tab_rc', [0, 1])
def test_remote_open_multiple_sessions_fans_out_on_controller_before_attach(zsh, tmp_path, tab_rc):
    env = fake_picker(tmp_path)
    result = zsh(HOSTS + r'''
_t_remote_open_inventory() {
  print -u2 -- "SCAN $1 $2"
  printf 'beta\tid-one\t/remote/api\tapi-1\tdetached\tactive\tFirst task\tcodex\n'
  printf 'beta\tid-two\t/remote/api\tapi-2\tdetached\tactive\tSecond task\tclaude\n'
}
_t_fzf_filtered() { print -r -- "$1"; }
_t_open() { print -r -- "ATTACH ${(j:|:)@}"; }
''' + f'_dev_open_tab() {{ print -r -- "TAB $*"; return {tab_rc}; }}; '
                 + 't remote open beta api; print rc=$?', _tty=True, **env)
    text = output(result)
    assert f'rc={tab_rc}' in text, text
    assert 'SCAN beta api' in text
    assert 'TAB t open api 2 --cli --host beta' in text
    assert 'ATTACH api|1|--cli|--host|beta' in text
    assert text.index('TAB ') < text.index('ATTACH ')
    assert ('Attach with:' in text) == bool(tab_rc)


@pytest.mark.parametrize('mode,rc', [('empty', 0), ('cancel', 130), ('failed', 1), ('scan_cancel', 130)])
def test_remote_open_local_selection_no_side_effects(zsh, tmp_path, mode, rc):
    env = fake_picker(tmp_path)
    scan = 'return 130' if mode == 'scan_cancel' else 'return 1' if mode == 'failed' else (':' if mode == 'empty' else
        "printf 'beta\\tid-one\\t/remote/api\\tapi-1\\tdetached\\tactive\\tTask\\tcodex\\n'")
    result = zsh(HOSTS + f'''
_t_remote_open_inventory() {{ {scan}; }}
_t_fzf_filtered() {{ return 130; }}
_t_open() {{ print WRONG_OPEN; }}
_dev_open_tab() {{ print WRONG_TAB; }}
t remote open beta api; print rc=$?
''', _tty=True, **env)
    assert f'rc={rc}' in output(result) and 'WRONG_' not in output(result)


def test_remote_open_inventory_worker_is_read_only_and_filters_on_target(zsh):
    result = zsh(r'''
DEV_REPOS=(foreign "$HOME/remote/api")
_dev_session_rows() {
  printf 'one\t%s\tforeign-1\tdetached\tactive\tTask\tcodex\n' "$HOME/remote/api"
  printf 'two\t%s\tforeign:abcd\tattached\tactive\tForeground\tclaude\n' "$HOME/remote/api"
  printf 'three\t%s\tother-1\tdetached\tactive\tOther\tcodex\n' "$HOME/other"
  printf 'four\t%s\tforeign-2\tapp\tactive\tDesktop\tcodex\n' "$HOME/remote/api"
}
_t_open() { print WRONG_OPEN; }
_dev_open_tab() { print WRONG_TAB; }
_t_open_picker() { print WRONG_PICK; }
_t_remote_open_rows api; print rc=$?
_t_remote_open_rows unknown; print unknown=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'foreign-1' in text and 'foreign:abcd' in text
    assert 'other-1' not in text and 'foreign-2' not in text and 'WRONG_' not in text
    assert 'unknown=1' in text and 'unknown repository: unknown' in text


@pytest.mark.parametrize('scan_rc', [0, 1, 2, 130])
def test_remote_open_inventory_quotes_worker_and_propagates_status(zsh, scan_rc):
    result = zsh(HOSTS + f'''
_dev_remote_scan_with_timeout() {{
  print -u2 -r -- "SCAN $1 $2 timeout=$3 stderr=$4"
  printf 'id\\t/remote/api\\tapi-1\\tdetached\\tactive\\tTask\\tcodex\\n'
  return {scan_rc}
}}
_t_remote_open_inventory beta 'remote repo'; print rc=$?
''')
    text = output(result)
    assert f'rc={scan_rc}' in text and 'SCAN user@beta zsh -lic ' in text
    assert 'timeout=15 stderr=1' in text
    assert '_t_remote_open_rows' in text
    assert ('beta\tid\t/remote/api' in result.stdout) == (scan_rc == 0)


@pytest.mark.parametrize('action,argv', [('open', 'beta --new api 3'),
                                       ('resume', 'beta --days 7 api 3')])
def test_remote_parser_allows_interleaved_flags(t_mod, action, argv):
    args = t_mod.build_parser().parse_args(['remote', action, *shlex.split(argv)])
    assert (args.host, args.repo, args.slot) == ('beta', 'api', '3')


@pytest.mark.parametrize('action,flag', [('open', '--host'), ('resume', '--days'),
                                       ('move', '--from'), ('move', '--session')])
def test_remote_parser_rejects_missing_flag_values_after_targets(t_mod, action, flag):
    with pytest.raises(SystemExit) as exc:
        t_mod.build_parser().parse_args(['remote', action, 'beta', flag])
    assert exc.value.code == 2


def test_remote_inventory_worker_runs_without_remote_helper_install(zsh):
    result = zsh(HOSTS + r'''
DEV_REPOS=(foreign "$HOME/remote path/api")
_dev_session_rows() {
  printf 'one\t%s\tforeign-1\tdetached\tactive\tTask\tcodex\n' "$HOME/remote path/api"
  printf 'two\t/elsewhere\tother-1\tdetached\tactive\tTask\tcodex\n'
}
_dev_remote_scan_with_timeout() {
  unfunction _t_remote_open_rows
  zsh() { [[ $1 == -lic ]] || return 91; eval "$2"; }
  eval "$2"
}
_t_remote_open_inventory beta api; print rc=$?
_t_remote_open_inventory beta unknown; print unknown=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'beta\tone\t' in text and 'foreign-1' in text
    assert 'other-1' not in text
    assert 'unknown=1' in text and 'unknown repository: unknown' in text


@pytest.mark.parametrize('route', ['send', 'pull', 'saved'])
def test_native_default_routes_leave_source_and_destination_untouched(zsh, route):
    setup = SETUP + r'''
_dev_self_sid() { :; }
_codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
_dev_codex_rollout_integrity() { print -u2 WRONG_INTEGRITY; return 1; }
_tbeam_kill_owner() { print -u2 WRONG_STOP; return 1; }
_dev_worktree_beam_push() { print -u2 WRONG_CHECKPOINT; return 1; }
_tbeam_sync_transcript() { print -u2 WRONG_SYNC; return 1; }
_tbeam_pull_transcript() { print -u2 WRONG_SYNC; return 1; }
ssh() { printf 'codex-id\t%s\tapi-2\tattached\tactive\tTask\tcodex\n' "$HOME/code/api"; }
'''
    invocation = {'send': 't session move --session codex-id --host mini --detach',
                  'pull': '_dev_pull mini mini api 2',
                  'saved': '_t_resume_on_host mini codex-id "$HOME/code/api" codex detach'}[route]
    result = zsh(setup + invocation + '; print rc=$?')
    text = output(result)
    assert 'rc=1' in text and 'native desktop app' in text and 'Hand off' in text
    assert 'no transfer was started' in text and 'WRONG_' not in text


def test_remote_relay_preserves_initiator_policy_and_checks_peer(zsh):
    result = zsh(HOSTS + r'''
DEV_HANDOFF[codex]=t
ssh() { print -r -- "$*"; }
t session move api 2 --from alpha --host beta --detach
''')
    text = output(result)
    assert '_T_HANDOFF_CODEX=t' in text
    assert '_t_handoff_check' in text and 'update' in text


@pytest.mark.parametrize('agent,value,success', [('claude', 't', True), ('codex', 't', True), ('codex', 'native', False), ('claude', 'native', False), ('codex', 'typo', False)])
def test_handoff_shell_policy_validation(zsh, agent, value, success):
    result = zsh(f'DEV_HANDOFF[{agent}]={value}; _t_handoff_check {agent} alpha beta')
    assert (result.returncode == 0) is success


@pytest.mark.parametrize('command', ['t session move', 't move'])
def test_canonical_move_selects_inside_agent_before_destination(zsh, tmp_path, command):
    env = fake_picker(tmp_path, session='local/api-2', host='alpha')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + SETUP + '''
_dev_self_sid() { print codex-id; }
_dev_rows_all() { print -u2 WRONG_REMOTE_SCAN; return 99; }
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
''' + command + '; print rc=$?', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text and 'MOVE --session|codex-id|--host|alpha' in text, text
    assert 'WRONG_REMOTE_SCAN' not in text
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert len(logs) == 2 and '--multi' in logs[0]['args'] and '--no-multi' in logs[1]['args']


def test_move_batch_excludes_all_source_aliases_and_detaches(zsh, tmp_path):
    env = fake_picker(tmp_path, session='alpha/api-2,beta/api-2', host='gamma')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + '''
REMOTE_HOSTS[alpha-alias]=user@alpha
REMOTE_HOSTS[gamma]=user@gamma
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; print -r -- "EXPECTED $T_MOVE_EXPECT_ROW"; }
t session move --remote; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text and '2 completed; 0 failed; 0 unattempted' in text, text
    assert text.count('MOVE ') == 2
    assert 'MOVE --session|remote-id|--detach|--from|alpha|--host|gamma' in text
    assert 'MOVE --session|remote-id|--detach|--from|beta|--host|gamma' in text
    assert 'EXPECTED remote-id\t/home/source/code/api' in text
    assert 'EXPECTED remote-id\t/home/dest/code/api' in text
    logs = [json.loads(line) for line in (tmp_path / 'picker.jsonl').read_text().splitlines()]
    assert [row.split('\t')[0] for row in logs[1]['rows']] == ['local', 'gamma']


@pytest.mark.parametrize('failure', [17, 130])
def test_move_batch_stops_and_identifies_remaining_sessions(zsh, tmp_path, failure):
    env = fake_picker(tmp_path, session='local/api-1,alpha/api-2,beta/api-2', host='gamma')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + f'''
REMOTE_HOSTS[gamma]=user@gamma
_t_beam() {{ print -r -- "MOVE $T_MOVE_EXPECT_ROW"; [[ $* == *--from\\ alpha* ]] && return {failure}; return 0; }}
t move -r; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert f'rc={failure}' in text, text
    assert text.count('MOVE ') == 2 and '1 completed;' in text and '1 unattempted' in text
    assert 'completed: local/api-1 (local-id)' in text
    assert f'{"cancelled" if failure == 130 else "failed"}: alpha/api-2 (remote-id)' in text
    assert 'unattempted: beta/api-2 (remote-id)' in text


@pytest.mark.parametrize('change', ['stale', 'failed-scan', 'native'])
def test_move_batch_preflights_every_selection_before_mutation(zsh, tmp_path, change):
    env = fake_picker(tmp_path, session='local/api-1,alpha/api-2', host='beta')
    env['CHANGE_ROWS'] = str(tmp_path / 'changed')
    setup = '''
_t_remote_move_rows() {
  if [[ -f $CHANGE_ROWS && $1 == alpha ]]; then
    _dev_rows_all | head -n 1
  else _dev_rows_all; fi
}
''' if change == 'stale' else ('''
_t_remote_move_rows() { [[ -f $CHANGE_ROWS && $1 == alpha ]] && return 1; _dev_rows_all; }
''' if change == 'failed-scan' else 'DEV_HANDOFF[codex]=native;')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + setup + '''
_t_beam() { print WRONG_MOVE; }
t session move -r; print rc=$?
''', _tty=True, **env)
    assert 'rc=1' in output(result) and 'WRONG_MOVE' not in output(result), output(result)


def test_move_batch_keeps_snapshot_identity_for_identical_display_rows(zsh, tmp_path):
    env = fake_picker(tmp_path, session='#2', host='beta')
    result = zsh(HOSTS + '''
_dev_rows_all() {
 printf 'alpha\tone\t/path/one\tapi-2\tattached\tactive\tSame task\tclaude\n'
 printf 'alpha\ttwo\t/path/two\tapi-2\tattached\tactive\tSame task\tclaude\n'
}
_t_remote_move_rows() { _dev_rows_all; }
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; print -r -- "EXPECTED $T_MOVE_EXPECT_ROW"; }
t move -r; print rc=$?
''', _tty=True, **env)
    text = output(result)
    assert 'rc=0' in text and 'MOVE --session|two|' in text and 'EXPECTED two\t/path/two' in text, text


def test_move_batch_deduplicates_repeated_selection_and_aliases(zsh, tmp_path):
    env = fake_picker(tmp_path, session='alpha/api-2,alpha/api-2,alpha-alias/api-2', host='beta')
    result = zsh(HOSTS + '''
REMOTE_HOSTS[alpha-alias]=user@alpha
_dev_rows_all() {
 printf 'alpha\tone\t/path/one\tapi-2\tattached\tactive\tTask\tclaude\n'
 printf 'alpha-alias\tone\t/path/one\tapi-2\tattached\tactive\tTask\tclaude\n'
}
_t_remote_move_rows() { _dev_rows_all; }
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t move -r; print rc=$?
''', _tty=True, **env)
    assert 'rc=0' in output(result) and output(result).count('MOVE ') == 1, output(result)


def test_move_batch_runs_current_session_last(zsh, tmp_path):
    env = fake_picker(tmp_path, session='local/api-1,alpha/api-2', host='beta')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + '''
_dev_self_sid() { print local-id; }
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t move -r; print rc=$?
''', _tty=True, **env)
    text = output(result)
    moves = [line for line in text.splitlines() if line.startswith('MOVE ')]
    assert 'rc=0' in text and len(moves) == 2, text
    assert 'remote-id' in moves[0] and 'local-id' in moves[1]


def test_move_root_alias_operational_parser_route(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, 'Config', lambda: object())
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: None)
    seen = []
    monkeypatch.setattr(t_mod, 'cmd_pending', lambda cfg, args: seen.append(args) or 23)
    assert t_mod.main(['move', '--remote', '--host', 'beta']) == 23
    assert seen[0].verb == 'beam' and seen[0].remote and seen[0].host == 'beta'


def test_move_remote_discovery_flag_does_not_change_receive_engine(zsh):
    result = zsh(HOSTS + '''
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t move --remote api 2 --from alpha --host beta --detach; print rc=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'MOVE api|2|--from|alpha|--host|beta|--detach' in text, text
    assert '--remote' not in text


def test_move_from_local_keeps_source_local(zsh):
    result = zsh(HOSTS + '''
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t session move api 2 --from local --host beta --detach; print rc=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'MOVE api|2|--detach|--host|beta' in text, text
    assert '--from' not in text


def test_move_remote_unique_exact_target_remains_scriptable(zsh):
    result = zsh(HOSTS + ROWS + '''
_dev_rows_all() { printf 'alpha\tid\t%s\tapi-2\tattached\tactive\tTask\tclaude\n' "$HOME/code/api"; }
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t move api 2 -r --host beta --detach; print rc=$?
''')
    text = output(result)
    assert 'rc=0' in text and 'MOVE --session|id|--detach|--from|alpha|--host|beta' in text, text


def test_move_batch_rejects_foreground_before_mutation(zsh, tmp_path):
    env = fake_picker(tmp_path, session='local/api-1,alpha/api-2', host='beta')
    result = zsh('DEV_HANDOFF[codex]=t; ' + HOSTS + ROWS + '''
_t_beam() { print WRONG_MOVE; }
t move -r --fg; print rc=$?
''', _tty=True, **env)
    assert 'rc=2' in output(result) and '--fg requires a single session' in output(result)
    assert 'WRONG_MOVE' not in output(result)


def test_legacy_beam_remote_flag_uses_canonical_discovery(zsh):
    result = zsh('''
_t_session_move() { print -r -- "CANONICAL ${(j:|:)@}"; }
_t_beam() { print WRONG_ENGINE; }
t beam -r --host beta; print rc=$?
''')
    assert 'rc=0' in output(result) and 'CANONICAL -r|--host|beta' in output(result)
    assert 'WRONG_ENGINE' not in output(result)
