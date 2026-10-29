"""The shared resume picker also carries host-independent saved identities."""
import shlex
import time

import pytest

from test_agent_seam import zsh  # noqa: F401


def history_row(sid='saved', slot='3', workspace='/former/home/trees/old/old-3', updated=None, host='local'):
    return '\t'.join(map(str, [updated or int(time.time()), 'api', slot, sid, workspace,
                              'Oct 28 12:00', 'Saved work', host, 'codex']))


def shell_candidates(rows):
    return '_t_resume_history_candidates() { print -r -- ' + shlex.quote('\n'.join(rows)) + '; }; '


def test_resume_history_selects_inactive_conversation_from_busy_slot(zsh):
    wt = zsh.home / 'code/.worktrees/api/3'
    wt.mkdir(parents=True)
    setup = shell_candidates([history_row()]) + (
        f"_dev_session_rows() {{ print -r -- 'owner\t{wt}\tapi-3\tdetached\tactive\tCurrent work\tcodex'; }}; "
        '_t_fzf_filtered() { print -r -- "$1"; }; '
        '_t_resume_history_launch() { print -u2 -- "HISTORY $*"; print dev-api-99; }; '
    )
    result = zsh(setup + '_t_resume api', _tty=True,
                 FAKE_SESSIONS='dev-api-3', FAKE_SESSION_PATH=str(wt))
    assert result.returncode == 0, result.stdout
    assert 'HISTORY api saved codex' in result.stdout
    assert 'attach-session -t =dev-api-99:' in zsh.log.read_text()
    assert 'send-keys' not in zsh.log.read_text()


def test_resume_history_multiple_conversations_allocate_independent_slots(zsh):
    setup = shell_candidates([history_row('one'), history_row('two')]) + (
        '_t_fzf_filtered() { print -r -- "$1"; }; '
        '_t_resume_history_launch() { [[ $2 == one ]] && print dev-api-98 || print dev-api-99; }; '
        '_dev_open_tab() { print -r -- "TAB $*"; }; '
    )
    result = zsh(setup + '_t_resume api', _tty=True)
    assert result.returncode == 0, result.stdout
    assert 'already revived' not in result.stdout
    assert ('TAB t open api 98' in result.stdout or 'TAB t open api 99' in result.stdout)


def test_resume_history_host_prepares_then_uses_selected_destination(zsh):
    setup = shell_candidates([history_row()]) + (
        '_t_resume_history_launch() { print -u2 -- "HISTORY $*"; print "dev-api-99\t/prepared/api-99"; }; '
        '_t_resume_on_host() { print -u2 -- "LAND $*"; print dev-api-99; }; '
    )
    result = zsh('DEV_HANDOFF[codex]=t; ' + setup + '_t_resume api --host destination')
    assert result.returncode == 0, result.stderr
    assert 'HISTORY api saved codex prepare' in result.stderr
    assert 'LAND destination saved /prepared/api-99 codex detach' in result.stderr
    assert 'on destination' in result.stdout


def test_resume_history_cancel_and_days_do_not_launch(zsh):
    setup = shell_candidates([history_row()]) + (
        '_T_RESUME_REQUIRE_PICKER=1; _t_fzf_filtered() { return 130; }; '
        '_t_resume_history_launch() { print UNEXPECTED; }; '
    )
    result = zsh(setup + '_t_resume api', _tty=True)
    assert result.returncode == 130 and 'UNEXPECTED' not in result.stdout
    result = zsh(shell_candidates([history_row(updated=1)]) + '_t_resume api')
    assert result.returncode == 1
    assert 'older conversation' in result.stderr


def test_resume_history_live_id_attaches_exact_owner(zsh):
    setup = shell_candidates([history_row()]) + (
        "_dev_session_rows() { print -r -- 'saved\t/current/tree\tapi-88\tdetached\tactive\tLive\tcodex'; }; "
        '_t_dev() { print -r -- "ATTACH $*"; }; '
        '_t_resume_history_launch() { print UNEXPECTED; }; '
    )
    result = zsh(setup + '_t_resume api --live', _tty=True)
    assert result.returncode == 0, result.stdout
    assert 'ATTACH api 88' in result.stdout and 'UNEXPECTED' not in result.stdout


def test_resume_history_app_rejects_claude_before_preparing(zsh):
    row = history_row().removesuffix('codex') + 'claude'
    result = zsh(shell_candidates([row]) +
                 'OSTYPE=darwin; _t_resume_history_launch() { print -u2 UNEXPECTED; }; _t_resume api --app')
    assert result.returncode == 1
    assert 'only saved Codex' in result.stderr
    assert 'UNEXPECTED' not in result.stderr


@pytest.mark.parametrize('owner', ['local', 'remote', 'collision', 'desktop', 'remote-desktop'])
@pytest.mark.parametrize('command', ['t resume api 3', 't session resume api 3'])
def test_explicit_slot_browses_live_and_saved_conversations_before_acting(zsh, owner, command):
    wt = zsh.home / 'code/.worktrees/api/3'
    wt.mkdir(parents=True)
    setup = shell_candidates([history_row('saved-one'), history_row('saved-two'),
                              history_row('unrelated', slot='4')])
    row = f'owner\t{wt}\tapi-3\tdetached\tactive\tCurrent work\tcodex'
    env = {}
    if owner in ('remote', 'remote-desktop'):
        if owner == 'remote-desktop':
            row = row.replace('\tdetached\t', '\tapp\t')
        setup += f"REMOTE_HOSTS[mini]=mini.invalid; _dev_rows_all() {{ print -r -- 'mini\t{row}'; }}; "
    else:
        setup += f"_dev_session_rows() {{ print -r -- '{row}'; }}; "
        if owner == 'local':
            env = dict(FAKE_SESSIONS='dev-api-3', FAKE_SESSION_PATH=str(wt))
        elif owner == 'collision':
            env = dict(FAKE_HAS_SESSION='1', FAKE_SESSION_PATH='/another/worktree')
        else:
            setup += '_dev_app_slot_reserved() { return 0; }; '
    setup += '''
      _t_fzf_filtered() { print -u2 -- "PICKER show_all=$6 all=$2"; return 130; }
      _t_dev() { print UNEXPECTED_ATTACH; }
      _dev_remote_attach() { print UNEXPECTED_REMOTE_ATTACH; }
      _t_resume_history_launch() { print UNEXPECTED_LAUNCH; }
    '''
    result = zsh(setup + command, _tty=True, **env)
    assert result.returncode == 130, result.stdout
    assert 'PICKER show_all=1' in result.stdout
    assert 'saved-one' in result.stdout and 'saved-two' in result.stdout
    assert ('desktop' if 'desktop' in owner else 'Current work') in result.stdout
    assert 'unrelated' not in result.stdout and 'UNEXPECTED_' not in result.stdout


@pytest.mark.parametrize('live', [False, True])
def test_explicit_slot_requires_selection_even_with_one_conversation(zsh, live):
    setup = shell_candidates([history_row()])
    if live:
        setup += "_dev_session_rows() { print -r -- 'saved\t/current/tree\tapi-88\tdetached\tactive\tLive\tcodex'; }; "
    setup += '''
      _t_fzf_filtered() { print -u2 PICKER; return 130; }
      _t_dev() { print UNEXPECTED_ATTACH; }
      _t_resume_history_launch() { print UNEXPECTED_LAUNCH; }
    '''
    result = zsh(setup + 't resume api 3', _tty=True)
    assert result.returncode == 130 and 'PICKER' in result.stdout
    assert 'UNEXPECTED_' not in result.stdout
    if live:
        result = zsh(setup + 't resume api 3')
        assert result.returncode == 1
        assert 'UNEXPECTED_' not in result.stdout and 'PICKER' not in result.stdout
        result = zsh(setup + 't resume api 3 </dev/null', _tty=True)
        assert result.returncode == 1
        assert 'UNEXPECTED_' not in result.stdout and 'PICKER' not in result.stdout


def test_explicit_slot_saved_selection_preserves_busy_owner(zsh):
    wt = zsh.home / 'code/.worktrees/api/3'
    wt.mkdir(parents=True)
    setup = shell_candidates([history_row()]) + (
        f"_dev_session_rows() {{ print -r -- 'owner\t{wt}\tapi-3\tdetached\tactive\tCurrent work\tcodex'; }}; "
        '_t_fzf_filtered() { print -r -- "$1"; }; '
        '_t_resume_history_launch() { print -u2 -- "HISTORY $*"; print dev-api-99; }; '
        '_t_dev() { print UNEXPECTED_ATTACH; }; '
    )
    result = zsh(setup + 't resume api 3', _tty=True,
                 FAKE_SESSIONS='dev-api-3', FAKE_SESSION_PATH=str(wt))
    assert result.returncode == 0, result.stdout
    assert 'HISTORY api saved codex' in result.stdout
    assert 'UNEXPECTED_ATTACH' not in result.stdout
    log = zsh.log.read_text()
    assert 'attach-session -t =dev-api-99:' in log
    assert 'respawn-pane' not in log and 'kill-session' not in log


def test_explicit_slot_live_selection_attaches_exact_owner(zsh):
    setup = shell_candidates([history_row()]) + '''
      _dev_session_rows() { printf 'saved\t/current/tree\tapi-88\tdetached\tactive\tLive\tcodex\n'; }
      _t_fzf_filtered() { print -r -- "$2"; }
      _t_dev() { print -r -- "ATTACH $*"; }
      _t_resume_history_launch() { print UNEXPECTED_LAUNCH; }
    '''
    result = zsh(setup + 't session resume api 3', _tty=True)
    assert result.returncode == 0 and 'ATTACH api 88' in result.stdout
    assert 'UNEXPECTED_LAUNCH' not in result.stdout


def test_explicit_slot_shared_filters_restore_inactive_and_all_rows(zsh, tmp_path):
    from test_resume_multiselect import FZF_ALL
    picker = tmp_path / 'stubbin/fzf'
    picker.write_text(FZF_ALL)
    picker.chmod(0o755)
    wt = zsh.home / 'code/.worktrees/api/3'
    wt.mkdir(parents=True)
    setup = shell_candidates([history_row()]) + (
        f"_dev_session_rows() {{ print -r -- 'owner\t{wt}\tapi-3\tdetached\tactive\tCurrent work\tcodex'; }}; "
        '_t_dev() { print UNEXPECTED_ATTACH; }; '
        '_t_resume_history_launch() { print UNEXPECTED_LAUNCH; }; '
    )
    log = tmp_path / 'picker.log'
    result = zsh(setup + 't resume api 3', _tty=True, FZF_LOG=str(log),
                 PICK_ACTIONS='on,off', PICK_CANCEL='1',
                 FAKE_SESSIONS='dev-api-3', FAKE_SESSION_PATH=str(wt))
    assert result.returncode == 130 and 'UNEXPECTED_' not in result.stdout
    initial = log.with_suffix('.log.initial').read_text()
    inactive = log.with_suffix('.log.step0').read_text()
    all_rows = log.with_suffix('.log.step1').read_text()
    assert 'Current work' in initial and 'Saved work' in initial
    assert 'Current work' not in inactive and 'Saved work' in inactive
    assert 'Current work' in all_rows and 'Saved work' in all_rows


def test_explicit_slot_missing_picker_never_resumes_implicitly(zsh):
    setup = shell_candidates([history_row()]) + '''
      command() {
        [[ $1 == -v && $2 == fzf ]] && return 1
        builtin command "$@"
      }
      _t_resume_history_launch() { print UNEXPECTED_LAUNCH; }
    '''
    result = zsh(setup + '_t_resume api 3', _tty=True)
    assert result.returncode == 1 and 'install fzf and retry' in result.stdout
    assert 'UNEXPECTED_LAUNCH' not in result.stdout


def test_explicit_slot_failed_owner_scan_never_attaches(zsh):
    wt = zsh.home / 'code/.worktrees/api/3'
    wt.mkdir(parents=True)
    result = zsh('''
      _dev_session_rows() { return 7; }
      _t_dev() { print UNEXPECTED_ATTACH; }
      t resume api 3
    ''', _tty=True, FAKE_SESSIONS='dev-api-3', FAKE_SESSION_PATH=str(wt))
    assert result.returncode == 1 and 'ownership could not be verified' in result.stdout
    assert 'UNEXPECTED_ATTACH' not in result.stdout
