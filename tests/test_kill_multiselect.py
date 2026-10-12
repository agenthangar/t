"""The kill picker keeps list details and routes every selected target safely."""
import subprocess
from pathlib import Path

import pytest

from test_agent_seam import zsh  # noqa: F401 - shared isolated shell fixture


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _safe_repo(zsh):
    repo = zsh.home / 'code' / 'api'
    repo.mkdir(parents=True)
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'task.txt').write_text('done\n')
    _git(repo, 'add', 'task.txt')
    _git(repo, 'commit', '-m', 'Initial task')
    _git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    _git(repo, 'switch', '-c', 'dev/api-1')
    return repo


def test_kill_safety_requires_clean_work_and_merged_head(zsh):
    repo = _safe_repo(zsh)
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 0
    (repo / 'task.txt').write_text('pending edit\n')
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 1
    _git(repo, 'add', 'task.txt')
    _git(repo, 'commit', '-m', 'Pending work')
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 1
    _git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 0


def test_kill_safety_does_not_trust_local_main_without_remote_proof(zsh):
    repo = _safe_repo(zsh)
    _git(repo, 'update-ref', '-d', 'refs/remotes/origin/main')
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 1


def test_kill_safety_accepts_exact_cached_squash_merge_head(zsh):
    repo = _safe_repo(zsh)
    _git(repo, 'remote', 'add', 'origin', 'git@github.com:example/api.git')
    (repo / 'task.txt').write_text('finished work\n')
    _git(repo, 'add', 'task.txt')
    _git(repo, 'commit', '-m', 'Finished work')
    head = _git(repo, 'rev-parse', 'HEAD')
    cache = zsh.home / '.cache' / 'claude-sessions' / 'pr'
    cache.mkdir(parents=True)
    (cache / 'example#api#42').write_text('MERGED')
    (cache / 'example#api#42.head').write_text(head)
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 0
    (cache / 'example#api#42.head').write_text('deadbeef')
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 1


def test_kill_safety_recognizes_squashed_tree_in_main_history(zsh):
    repo = _safe_repo(zsh)
    (repo / 'task.txt').write_text('finished work\n')
    _git(repo, 'add', 'task.txt')
    _git(repo, 'commit', '-m', 'Feature commit')
    _git(repo, 'switch', 'main')
    (repo / 'task.txt').write_text('finished work\n')
    _git(repo, 'add', 'task.txt')
    _git(repo, 'commit', '-m', 'Squash merge')
    _git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    _git(repo, 'switch', 'dev/api-1')
    assert zsh(f'_dev_kill_no_pending_work {repo}').returncode == 0


def test_safe_labels_include_detached_and_inactive_desktop_only(zsh):
    repo = _safe_repo(zsh)
    rows = '\n'.join([
        f'-\t{repo}\tapi-1\tdetached\tactive\tCompleted CLI\tcodex\tgpt-6',
        f'-\t{repo}\tapi-2\tattached\tactive\tAttached CLI\tcodex\tgpt-6',
        f'-\t{repo}\tapi-3\tapp\tnone\tIdle desktop\tcodex\tgpt-6',
        f'-\t{repo}\tapi-4\tapp\tactive\tLoaded desktop\tcodex\tgpt-6',
        f'-\t{repo}\tapi:fg\tdetached\tactive\tForeground\tcodex\tgpt-6',
        f'-\t{repo}\tapi-5\tdetached\tactive\tFollow-up · #42 merged, still in progress\tcodex\tgpt-6',
        f'-\t{repo}\tapi-6\tdetached\tactive\tReview pending · #43 open\tcodex\tgpt-6',
        f'-\t{repo}\tapi-7\tdetached\tactive\tReview unknown · #44 ?\tcodex\tgpt-6',
    ])
    result = zsh('print -r -- "$FAKE_DETAILS" | _dev_kill_safe_labels', FAKE_DETAILS=rows)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['api-1', 'api-3']


def test_kill_picker_starts_with_only_proven_safe_rows(zsh):
    log = _picker_stub(zsh)
    repo = _safe_repo(zsh)
    rows = '\n'.join([
        f'-\t{repo}\tapi-1\tdetached\tactive\tCompleted CLI\tcodex\tgpt-6',
        f'-\t{repo}\tapi-2\tattached\tactive\tAttached CLI\tcodex\tgpt-6',
        f'-\t{repo}\tapi-3\tapp\tnone\tIdle desktop\tcodex\tgpt-6',
        f'-\t{repo}\tapi-4\tapp\tactive\tLoaded desktop\tcodex\tgpt-6',
    ])
    code = PICKER_MOCK + '_dev_session_rows() { print -r -- "$FAKE_DETAILS"; }; _dev_kill_pick api ""'
    result = zsh(code, _tty=True, FAKE_DETAILS=rows, FZF_LOG=str(log), FZF_CANCEL='1')
    assert result.returncode == 130
    visible = Path(f'{log}.visible').read_text()
    all_rows = Path(f'{log}.all').read_text()
    assert 'api-1' in visible and 'api-3' in visible
    assert 'api-2' not in visible and 'api-4' not in visible
    assert all(f'api-{n}' in all_rows for n in range(1, 5))


def test_kill_picker_empty_default_can_still_reveal_pending_rows(zsh):
    log = _picker_stub(zsh)
    repo = _safe_repo(zsh)
    (repo / 'task.txt').write_text('pending\n')
    row = f'-\t{repo}\tapi-1\tdetached\tactive\tUncommitted work\tcodex\tgpt-6'
    code = PICKER_MOCK + '_dev_session_rows() { print -r -- "$FAKE_DETAILS"; }; _dev_kill_pick api ""'
    result = zsh(code, _tty=True, FAKE_DETAILS=row, FZF_LOG=str(log), FZF_CANCEL='1')
    assert result.returncode == 130
    assert not Path(f'{log}.visible').read_text().strip()
    assert 'api-1' in Path(f'{log}.all').read_text()


def test_kill_picker_remote_default_uses_verified_host_labels(zsh):
    log = _picker_stub(zsh)
    repo = zsh.home / 'code' / 'api'
    rows = '\n'.join([
        f'mini\t-\t{repo}\tapi-3\tdetached\tactive\tCompleted remotely\tcodex\tgpt-6',
        f'studio\t-\t{repo}\tapi-4\tdetached\tactive\tPending remotely\tcodex\tgpt-6',
    ])
    ssh = zsh.home.parent / 'stubbin' / 'ssh'
    ssh.write_text('''#!/bin/sh
printf '%s\n' "$*" >> "$SSH_LOG"
case "$*" in *mini.example*'_dev_kill_safe_labels'*) printf 'api-3\n' ;; esac
''')
    ssh.chmod(0o755)
    ssh_log = zsh.home.parent / 'ssh.log'
    code = PICKER_MOCK + '''
      typeset -gA REMOTE_HOSTS=(mini mini.example studio studio.example)
      _dev_rows_all() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill_pick api '' 1
    '''
    result = zsh(code, _tty=True, FAKE_DETAILS=rows, FZF_LOG=str(log),
                 FZF_CANCEL='1', SSH_LOG=str(ssh_log))
    assert result.returncode == 130
    visible = Path(f'{log}.visible').read_text()
    all_rows = Path(f'{log}.all').read_text()
    assert 'api-3' in visible and 'api-4' not in visible
    assert 'api-3' in all_rows and 'api-4' in all_rows
    assert ssh_log.read_text().count('_dev_kill_safe_labels') == 2


PICKER_MOCK = """
  _t_fzf_filtered() {
    print -r -- "$1" > "$FZF_LOG.visible"
    print -r -- "$2" > "$FZF_LOG.all"
    [[ -n $FZF_CANCEL ]] && return 1
    if [[ -n $FZF_SELECT_ALL ]]; then print -r -- "$2"
    else print -r -- "$1"; fi
  }
"""


def _picker_stub(zsh):
    path = zsh.home.parent / "stubbin" / "fzf"
    path.write_text('#!/bin/sh\nexit 1\n')
    path.chmod(0o755)
    return zsh.home.parent / "fzf.log"


def _rows(zsh):
    root = zsh.home / "code" / "api"
    return "\n".join([
        f"sid-1\t{root}\tapi-1\tattached\tactive\tLong task description with every word visible\tcodex\tgpt-6-astra-extra-long-model",
        f"sid-2\t{root}\tapi-2\tapp\tnone\tNo conversation recorded\tcodex\t-",
    ])


def test_kill_picker_shows_full_details_without_a_terminal(zsh):
    repo = _safe_repo(zsh)
    rows = '\n'.join([
        f'-\t{repo}\tapi-1\tdetached\tactive\tLong task description with every word visible\tcodex\tgpt-6-astra-extra-long-model',
        f'-\t{repo}\tapi-2\tattached\tactive\tStill attached\tcodex\tgpt-6',
    ])
    code = "_dev_session_rows() { print -r -- \"$FAKE_DETAILS\"; }; _dev_kill_pick api 1"
    result = zsh(code, FAKE_DETAILS=rows)
    assert result.returncode == 1
    assert "gpt-6-astra-extra-long-model" in result.stdout
    assert "Long task description with every word visible" in result.stdout
    assert "Still attached" not in result.stdout
    assert 'Ctrl-A' in result.stderr


def test_kill_picker_without_terminal_hides_unverified_rows(zsh):
    code = '_dev_session_rows() { print -r -- "$FAKE_DETAILS"; }; _dev_kill_pick api ""'
    result = zsh(code, FAKE_DETAILS=_rows(zsh))
    assert result.returncode == 1
    assert 'No unattached sessions with no pending work.' in result.stdout
    assert 'No conversation recorded' not in result.stdout


@pytest.mark.parametrize('force', ['', '1'])
def test_kill_picker_dispatches_multiple_cli_and_desktop_rows(zsh, force):
    log = _picker_stub(zsh)
    rows = _rows(zsh)
    code = PICKER_MOCK + """
      _dev_session_rows() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill() { print -r -- "CLOSE $1 $2 [${3:-none}]"; }
      _dev_app_slot_reserved() { return 0; }
      _dev_kill_pick api "$KILL_FORCE"
    """
    result = zsh(code, _tty=True, FAKE_DETAILS=rows, FAKE_HAS_SESSION="1",
                 FZF_LOG=str(log), FZF_SELECT_ALL="1", KILL_FORCE=force)
    assert result.returncode == 0, result.stdout
    shown_force = force or 'none'
    assert f"CLOSE api 1 [{shown_force}]" in result.stdout
    assert f"CLOSE api 2 [{shown_force}]" in result.stdout
    shown = Path(f'{log}.all').read_text()
    assert 'gpt-6-astra-extra-long-model' in shown
    assert 'Long task description with every word visible' in shown


def test_kill_picker_cancel_never_closes_a_session(zsh):
    log = _picker_stub(zsh)
    code = PICKER_MOCK + """
      _dev_session_rows() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill() { print -r -- "UNEXPECTED CLOSE $1 $2"; }
      _dev_kill_pick api 1
    """
    result = zsh(code, _tty=True, FAKE_DETAILS=_rows(zsh), FAKE_HAS_SESSION="1",
                 FZF_LOG=str(log), FZF_CANCEL="1")
    assert result.returncode == 130
    assert "UNEXPECTED CLOSE" not in result.stdout


def test_kill_picker_keeps_going_after_one_close_fails(zsh):
    log = _picker_stub(zsh)
    code = PICKER_MOCK + """
      _dev_session_rows() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill() {
        print -r -- "CLOSE $1 $2"
        [[ $2 == 1 ]] && return 1
        return 0
      }
      _dev_app_slot_reserved() { return 0; }
      _dev_kill_pick api ''
    """
    result = zsh(code, _tty=True, FAKE_DETAILS=_rows(zsh), FAKE_HAS_SESSION="1",
                 FZF_LOG=str(log), FZF_SELECT_ALL="1")
    assert result.returncode == 1
    assert "CLOSE api 1" in result.stdout
    assert "CLOSE api 2" in result.stdout


@pytest.mark.parametrize('force', ['', '1'])
def test_kill_picker_remote_rows_keep_host_and_route_each_selection(zsh, force):
    log = _picker_stub(zsh)
    root = zsh.home / "code" / "api"
    rows = "\n".join([
        f"mini\tsid-3\t{root}\tapi-3\tdetached\tactive\tRemote repair\tcodex\tgpt-6-astra",
        f"studio\tsid-4\t{root}\tapi-4\tapp\tactive\tDesktop thread\tcodex\tgpt-6-astra",
    ])
    ssh = zsh.home.parent / "stubbin" / "ssh"
    ssh.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n")
    ssh.chmod(0o755)
    ssh_log = zsh.home.parent / "ssh.log"
    code = PICKER_MOCK + """
      typeset -gA REMOTE_HOSTS=(mini mini.example studio studio.example)
      _dev_rows_all() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill_pick api "$KILL_FORCE" 1
    """
    result = zsh(code, _tty=True, FAKE_DETAILS=rows, FZF_LOG=str(log),
                 SSH_LOG=str(ssh_log), FZF_SELECT_ALL="1", KILL_FORCE=force)
    assert result.returncode == 0, result.stdout
    calls = ssh_log.read_text()
    assert "mini.example" in calls and "studio.example" in calls
    assert "zsh -lic" in calls
    assert "api" in calls and "3" in calls and "4" in calls
    assert ("-y" in calls) == bool(force)
    assert 'mini' in Path(f'{log}.all').read_text()


def test_kill_picker_empty_scan_reports_no_sessions(zsh):
    code = '_dev_session_rows() { return 0; }; _dev_kill_pick "" ""'
    result = zsh(code)
    assert result.returncode == 1
    assert "no sessions found" in result.stderr
    assert "STATUS" not in result.stdout


def test_bare_kill_opens_picker_but_explicit_all_keeps_existing_path(zsh):
    code = """
      _dev_kill_pick() { print -r -- "PICK ${1:-all} ${2:-no}"; }
      _dev_kill_one() { print -r -- "CLOSE $1 $2"; }
      _dev_kill '' '' 1
      _dev_kill api all 1
    """
    result = zsh(code, FAKE_SESSIONS="dev-api-1 dev-api-2")
    assert result.returncode == 0, result.stderr
    assert "PICK all 1" in result.stdout
    assert "CLOSE dev-api-1 1" in result.stdout
    assert "CLOSE dev-api-2 1" in result.stdout
    assert result.stdout.count("PICK") == 1
