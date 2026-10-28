"""The shared resume picker also carries host-independent saved identities."""
import shlex
import time

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
    result = zsh(setup + '_t_resume api --host destination')
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
