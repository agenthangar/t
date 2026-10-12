"""The kill picker keeps list details and routes every selected target safely."""
import pytest

from test_agent_seam import zsh  # noqa: F401 - shared isolated shell fixture


PICK_ALL = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
rows = sys.stdin.read().splitlines()
with open(os.environ['FZF_LOG'], 'w') as log:
    log.write('\\n'.join(args + rows))
if os.environ.get('FZF_CANCEL'):
    sys.exit(1)
for row in rows[1:]:
    print(row)
"""


def _picker_stub(zsh):
    path = zsh.home.parent / "stubbin" / "fzf"
    path.write_text(PICK_ALL)
    path.chmod(0o755)
    return zsh.home.parent / "fzf.log"


def _rows(zsh):
    root = zsh.home / "code" / "api"
    return "\n".join([
        f"sid-1\t{root}\tapi-1\tattached\tactive\tLong task description with every word visible\tcodex\tgpt-6-astra-extra-long-model",
        f"sid-2\t{root}\tapi-2\tapp\tnone\tNo conversation recorded\tcodex\t-",
    ])


def test_kill_picker_shows_full_details_without_a_terminal(zsh):
    rows = _rows(zsh)
    code = "_dev_session_rows() { print -r -- \"$FAKE_DETAILS\"; }; _dev_kill_pick api 1"
    result = zsh(code, FAKE_DETAILS=rows)
    assert result.returncode == 1
    assert "STATUS" in result.stdout
    assert "UI" in result.stdout
    assert "MODEL" in result.stdout
    assert "gpt-6-astra-extra-long-model" in result.stdout
    assert "Long task description with every word visible" in result.stdout
    assert "▣ desktop" in result.stdout
    assert "No conversation recorded" in result.stdout


@pytest.mark.parametrize('force', ['', '1'])
def test_kill_picker_dispatches_multiple_cli_and_desktop_rows(zsh, force):
    log = _picker_stub(zsh)
    rows = _rows(zsh)
    code = """
      _dev_session_rows() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill() { print -r -- "CLOSE $1 $2 [${3:-none}]"; }
      _dev_app_slot_reserved() { return 0; }
      _dev_kill_pick api "$KILL_FORCE"
    """
    result = zsh(code, _tty=True, FAKE_DETAILS=rows, FAKE_HAS_SESSION="1",
                 FZF_LOG=str(log), KILL_FORCE=force)
    assert result.returncode == 0, result.stdout
    shown_force = force or 'none'
    assert f"CLOSE api 1 [{shown_force}]" in result.stdout
    assert f"CLOSE api 2 [{shown_force}]" in result.stdout
    args = log.read_text()
    assert "--multi" in args
    assert "--header-lines=1" in args
    assert "--with-nth=-1" in args
    assert "--preview" in args
    assert "Ctrl-/ full details" in args


def test_kill_picker_cancel_never_closes_a_session(zsh):
    log = _picker_stub(zsh)
    code = """
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
    code = """
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
                 FZF_LOG=str(log))
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
    code = """
      typeset -gA REMOTE_HOSTS=(mini mini.example studio studio.example)
      _dev_rows_all() { print -r -- "$FAKE_DETAILS"; }
      _dev_kill_pick api "$KILL_FORCE" 1
    """
    result = zsh(code, _tty=True, FAKE_DETAILS=rows, FZF_LOG=str(log),
                 SSH_LOG=str(ssh_log), KILL_FORCE=force)
    assert result.returncode == 0, result.stdout
    calls = ssh_log.read_text()
    assert "mini.example" in calls and "studio.example" in calls
    assert "zsh -lic" in calls
    assert "api" in calls and "3" in calls and "4" in calls
    assert ("-y" in calls) == bool(force)
    assert "HOST" in log.read_text()


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
