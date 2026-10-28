"""Unavailable hosts warn while resume keeps local locks and known-owner checks."""
import io
import shlex
import subprocess
import sys
from contextlib import nullcontext

import pytest

from conftest import REPO_ROOT
from test_agent_seam import zsh  # noqa: F401
from test_resume_app import saved_rollout, SID, OTHER


SCANS = r'''
  REMOTE_HOSTS[offline]=offline
  REMOTE_HOSTS[online]=online
  _dev_remote_scan_with_timeout() {
    [[ $1 == offline ]] && return ${OFFLINE_RC:-255}
    printf 'online-id\t%s\tapi-4\tdetached\tactive\tReachable\tcodex\n' "$HOME/code/.worktrees/api/4"
  }
'''


@pytest.mark.parametrize('stderr_tty,stdout_tty,term,no_color,color', [
    (True, True, 'xterm-256color', None, True),
    (True, False, 'xterm-256color', None, True),
    (False, True, 'xterm-256color', None, False),
    (True, True, 'dumb', None, False),
    (True, True, 'xterm-256color', '', False),
    (True, True, 'xterm-256color', '1', False),
])
@pytest.mark.parametrize('returncode', [0, 1, 255])
def test_capture_replays_only_warning_lines(t_mod, monkeypatch, stderr_tty, stdout_tty,
                                          term, no_color, color, returncode):
    class Stream(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self.tty = tty

        def isatty(self):
            return self.tty

    out, err = Stream(stdout_tty), Stream(stderr_tty)
    monkeypatch.setattr(t_mod.sys, 'stdout', out)
    monkeypatch.setattr(t_mod.sys, 'stderr', err)
    monkeypatch.setenv('TERM', term)
    monkeypatch.delenv('NO_COLOR', raising=False)
    if no_color is not None:
        monkeypatch.setenv('NO_COLOR', no_color)
    diagnostics = 'ssh: diagnostic\nwarning: first\n\n warning: indented\nwarning:second\nwarning: final'
    rows = 'local\tlocal-id\t/work/api\tapi-3\tdetached\tnone\tLocal\tcodex\n'
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, rows, diagnostics)

    monkeypatch.setattr(t_mod.subprocess, 'run', run)
    # Exercise the real replay boundary, with no progress output mixed in.
    monkeypatch.setattr(t_mod, '_spinner', lambda *a, **kw: nullcontext())
    assert t_mod.zsh_capture('_dev_rows_all --details') == rows
    expected = diagnostics
    if color:
        for warning in ('warning: first', 'warning: final'):
            expected = expected.replace(warning, '\x1b[33m' + warning + '\x1b[0m')
    assert err.getvalue() == expected
    assert out.getvalue() == ''
    assert calls[0][0] == ['zsh', '-lic', t_mod.ZSH_PRELUDE + '_dev_rows_all --details']
    assert calls[0][1] == dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, start_new_session=True, text=True)


@pytest.mark.parametrize('mode,color', [('tty', True), ('stdout_redirected', True),
                                        ('stderr_redirected', False), ('plain', False),
                                        ('dumb', False), ('no_color', False)])
def test_python_remote_list_replays_captured_warnings(zsh, tmp_path, mode, color):
    # The real CLI spawns a login shell: give that shell only fixture integration.
    (zsh.home / '.zshrc').write_text(
        f'source {shlex.quote(str(REPO_ROOT / "t.plugin.zsh"))}\n' + SCANS + r'''
_dev_session_rows() {
  printf 'local-id\t%s\tapi-3\tdetached\tnone\tLocal\tcodex\n' "$HOME/code/.worktrees/api/3"
}
''')
    output_file, error_file = tmp_path / 'rows.txt', tmp_path / 'warnings.txt'
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(REPO_ROOT / "bin/t"))} session list -r -a'
    if mode == 'stdout_redirected':
        command += f' > {shlex.quote(str(output_file))}'
    if mode == 'stderr_redirected':
        command += f' 2> {shlex.quote(str(error_file))}'
    env = {'TERM': 'dumb' if mode == 'dumb' else 'xterm-256color'}
    if mode == 'no_color':
        env['NO_COLOR'] = ''
    result = zsh(command + '; print rc=$?', _tty=mode != 'plain', **env)
    text = result.stdout + result.stderr
    if error_file.exists():
        text += error_file.read_text()
    assert 'rc=0' in text
    assert 'warning: dev: offline unreachable — skipped' in text
    assert ('\x1b[33mwarning: dev: offline' in text) == color
    rows = output_file.read_text() if output_file.exists() else result.stdout
    assert 'api-3' in rows and 'api-4' in rows
    assert 'Local' in rows and 'Reachable' in rows
    if output_file.exists():
        assert '\x1b' not in rows and 'warning:' not in rows


@pytest.mark.parametrize('mode,expected', [('', 1), ('--best-effort', 0)])
@pytest.mark.parametrize('remote_rc', [255, 124, 127, 7])
def test_partial_scan_keeps_rows_and_strict_default(zsh, mode, expected, remote_rc):
    result = zsh(SCANS + r'''
      _dev_session_rows() {
        printf 'local-id\t%s\tapi-3\tdetached\tnone\tLocal\tcodex\n' "$HOME/code/.worktrees/api/3"
      }
      setopt errexit
      rc=0
      _dev_rows_all ''' + mode + r''' --details || rc=$?
      print -r -- "rc=$rc"
    ''', OFFLINE_RC=str(remote_rc))
    assert result.returncode == 0, result.stderr
    assert f'rc={expected}' in result.stdout
    assert 'local\tlocal-id\t' in result.stdout
    assert 'online\tonline-id\t' in result.stdout
    assert 'offline\t' not in result.stdout
    assert 'warning: dev: offline' in result.stderr
    assert 'skipped' in result.stderr
    assert '\x1b' not in result.stdout + result.stderr


def test_best_effort_local_failure_stays_fatal(zsh):
    result = zsh(SCANS + '_dev_session_rows() { return 9; }; _dev_rows_all --best-effort')
    assert result.returncode == 1
    assert 'local session scan failed (rc=9)' in result.stderr


@pytest.mark.parametrize('env,color', [({}, True), ({'NO_COLOR': ''}, False), ({'TERM': 'dumb'}, False)])
def test_remote_warnings_highlight_only_on_capable_terminal(zsh, env, color):
    result = zsh('_t_warn "dev: sleeping unreachable — skipped"', _tty=True,
                 **{'TERM': 'xterm-256color', **env})
    assert 'warning: dev: sleeping unreachable — skipped' in result.stdout
    assert ('\x1b[33m' in result.stdout) == color


def test_partial_resume_checks_selected_saved_writer_and_starts(zsh):
    cwd, _ = saved_rollout(zsh)
    result = zsh(SCANS + r'''
      _dev_session_rows() { :; }
      _dev_ensure_session_cwd() { print -r -- "$1"; }
      _t_resume api 3 --days all
    ''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'offline unreachable' in result.stderr
    assert 'ownership on unavailable hosts could not be verified' in result.stderr
    assert 'no saved conversation was resumed. Retry when configured hosts' not in result.stderr
    calls = zsh.log.read_text()
    assert 'new-session' in calls and 'respawn-pane' in calls
    assert cwd.exists()


@pytest.mark.parametrize('owner', ['same_sid', 'same_worktree', 'local_failure'])
def test_resume_preflight_still_rejects_known_owners_and_local_failure(zsh, owner):
    cwd, _ = saved_rollout(zsh)
    result = zsh(SCANS + r'''
      _dev_session_rows() { [[ $OWNER != local_failure ]]; }
      _dev_remote_scan_with_timeout() {
        [[ $1 == offline ]] && return 255
        if [[ $OWNER == same_sid ]]; then
          printf '%s\t/other/worktree\tapi-9\tdetached\tactive\tLive\tcodex\n' "$SID"
        elif [[ $OWNER == same_worktree ]]; then
          printf 'different-id\t%s\tapi-3\tdetached\tactive\tLive\tcodex\n' "$CWD"
        fi
      }
      _T_RESUME_ALLOW_UNAVAILABLE=1 _dev_resume_session dev-api-3 "$CWD" "$SID" codex
    ''', CWD=str(cwd), SID=SID, OWNER=owner)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'already live' in result.stderr or 'local session scan failed' in result.stderr
    assert 'new-session' not in zsh.log.read_text()


def test_saved_app_resume_preflight_continues_with_unavailable_host(zsh):
    cwd, _ = saved_rollout(zsh)
    result = zsh(SCANS + r'''
      _dev_session_rows() { :; }
      _t_resume_app_check api 3 "$SID" "$CWD"
    ''', CWD=str(cwd), SID=SID)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'saved\tapi-3'
    assert 'ownership on unavailable hosts could not be verified' in result.stderr


@pytest.mark.parametrize('phase', ['discovery', 'preflight', 'app', 'remote_detach'])
def test_resume_cancellation_stays_130(zsh, phase):
    cwd, _ = saved_rollout(zsh)
    result = zsh(r'''
      _dev_rows_all() { return 130; }
      _dev_worktree_beam_push() { :; }
      _tbeam_sync_transcript() { :; }
      ssh() { return 130; }
      case $PHASE in
        discovery) REMOTE_HOSTS[offline]=offline; _t_resume api 3 ;;
        preflight) _T_RESUME_ALLOW_UNAVAILABLE=1 _dev_resume_session dev-api-3 "$CWD" "$SID" codex ;;
        app) _t_resume_app_check api 3 "$SID" "$CWD" ;;
        remote_detach) _t_resume_on_host offline "$SID" "$CWD" codex detach ;;
      esac
    ''', PHASE=phase, CWD=str(cwd), SID=SID)
    assert result.returncode == 130, result.stdout + result.stderr


@pytest.mark.parametrize('verb', ['open', 'close'])
def test_remote_session_pickers_keep_reachable_rows_with_offline_peer(zsh, tmp_path, verb):
    picker = tmp_path / 'stubbin/fzf'
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    result = zsh(SCANS + r'''
      _dev_session_rows() { :; }
      _t_fzf_filtered() { print -u2 PICKER_OPENED; print -r -- "$2"; }
      _t_open_selected() { print -r -- "OPEN $*"; }
      ssh() { print -r -- "SSH $*"; }
      if [[ $VERB == open ]]; then _t_open_picker api 1 '' 1
      else _dev_kill_pick api 1 1; fi
    ''', _tty=True, VERB=verb)
    assert result.returncode == 0, result.stdout
    assert 'PICKER_OPENED' in result.stdout
    assert 'offline unreachable' in result.stdout
    assert ('OPEN online' if verb == 'open' else '→ Killing online:api-4') in result.stdout


def test_remote_close_cancellation_stops_remaining_marks(zsh, tmp_path):
    picker = tmp_path / 'stubbin/fzf'
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    result = zsh(SCANS + r'''
      REMOTE_HOSTS[second]=second
      _dev_session_rows() { :; }
      _t_fzf_filtered() { print -r -- "$2"; }
      ssh() {
        if [[ $1 == -t ]]; then print -r -- "$*" >> "$HOME/closed"; return 130; fi
        return 0
      }
      _dev_kill_pick api 1 1
    ''', _tty=True)
    assert result.returncode == 130, result.stdout
    assert len((zsh.home / 'closed').read_text().splitlines()) == 1


def test_remote_resume_worker_opens_picker_despite_an_unavailable_peer(zsh, tmp_path):
    cwd, _ = saved_rollout(zsh)
    picker = tmp_path / 'stubbin/fzf'
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    result = zsh(SCANS + r'''
      _dev_session_rows() {
        printf '%s\t%s\tapi-3\tdetached\tactive\tLocal live\tcodex\n' "$SID" "$CWD"
      }
      _t_fzf_filtered() { print -u2 PICKER_OPENED; print -r -- "$2" | awk -F'\t' '$7 == "here"'; }
      _t_dev() { print -r -- "ATTACH $*"; }
      # Execute the quoted worker in this sandbox, preserving its real flags.
      ssh() { eval "${@[-1]}"; }
      zsh() { [[ $1 == -lic ]] && eval "$2"; }
      _t_remote resume api --host online
    ''', _tty=True, SID=SID, CWD=str(cwd), FAKE_SESSIONS='dev-api-3', FAKE_SESSION_PATH=str(cwd))
    assert result.returncode == 0, result.stdout
    assert 'PICKER_OPENED' in result.stdout
    assert 'ATTACH api 3' in result.stdout
    assert 'offline unreachable — skipped' in result.stdout
    assert 'no saved conversation was resumed' not in result.stdout
    assert 'new-session' not in zsh.log.read_text()


def test_partial_resume_batch_skips_saved_failure_and_attaches_live_mark(zsh, tmp_path):
    cwd, _ = saved_rollout(zsh)
    live_cwd, _ = saved_rollout(zsh, sid=OTHER, slot='5')
    picker = tmp_path / 'stubbin/fzf'
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    result = zsh(SCANS + r'''
      _dev_session_rows() {
        printf '%s\t%s\tapi-5\tdetached\tactive\tLive\tcodex\n' "$LIVE_SID" "$LIVE_CWD"
      }
      _dev_ensure_session_cwd() { print -r -- "$1"; }
      _dev_resume_session() { print -u2 SELECTED_LAUNCH_FAILURE; return 1; }
      _t_fzf_filtered() { print -r -- "$2"; }
      _dev_open_picks() { print -r -- "ATTACH $*"; }
      _t_resume api --days all --live
    ''', _tty=True, LIVE_SID=OTHER, LIVE_CWD=str(live_cwd),
        FAKE_SESSIONS='dev-api-5', FAKE_SESSION_PATH=str(live_cwd))
    assert result.returncode == 0, result.stdout
    assert 'api 3: launch failed' in result.stdout
    assert 'ATTACH local\tapi\t5' in result.stdout
    calls = zsh.log.read_text()
    assert 'new-session' not in calls and 'respawn-pane' not in calls
    assert cwd.exists()


@pytest.mark.parametrize('failure', ['claude', 'codex', 'hostname', 'cancel'])
def test_remote_transcript_pull_continues_and_caches_only_success(zsh, failure):
    saved_rollout(zsh)
    result = zsh(r'''
      REMOTE_HOSTS[a]=a; REMOTE_HOSTS[b]=b
      _dev_rows_all() { :; }
      _dev_ensure_session_cwd() { print -r -- "$1"; }
      _dev_resume_session() { :; }
      _t_resume_rsync() {
        print -r -- "$*" >> "$HOME/pulls"
        if [[ $* == *a:* ]]; then
          [[ $FAILURE == claude && $* == *.claude/* ]] && return 255
          [[ $FAILURE == codex && $* == *.codex/* ]] && return 12
          [[ $FAILURE == cancel ]] && return 130
        fi
        return 0
      }
      ssh() { [[ $FAILURE == hostname && $* == *' a '* ]] && return 255; print verified; }
      setopt errexit
      _t_resume api 3 --remote --days all
    ''', FAILURE=failure)
    pulls = (zsh.home / 'pulls').read_text()
    if failure == 'cancel':
        assert result.returncode == 130
        assert 'b:' not in pulls
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'b:' in pulls
        assert not (zsh.home / '.cache/t/hostnames/a').exists()
        assert (zsh.home / '.cache/t/hostnames/b').read_text() == 'verified\n'
        if failure != 'hostname':
            assert 'a transcript fetch failed' in result.stderr
            assert "pulled a's latest transcripts" not in result.stderr


@pytest.mark.parametrize('local_failure', [False, True])
def test_remote_move_source_picker_keeps_reachable_candidates(zsh, tmp_path, local_failure):
    picker = tmp_path / 'stubbin/fzf'
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    result = zsh(SCANS + r'''
      _dev_session_rows() { [[ $LOCAL_FAILURE == 0 ]]; }
      _t_fzf_single() { print -u2 PICKER_OPENED; cat >&2; return 130; }
      ssh() { print -u2 UNEXPECTED_SSH; return 99; }
      _t_remote move api --host offline
    ''', _tty=True, LOCAL_FAILURE=str(int(local_failure)))
    assert result.returncode == (1 if local_failure else 130), result.stdout
    assert 'offline unreachable' in result.stdout
    assert ('PICKER_OPENED' in result.stdout) == (not local_failure)
    if not local_failure:
        assert 'online' in result.stdout and 'api-4' in result.stdout
    assert 'UNEXPECTED_SSH' not in result.stdout


def test_remote_move_explicit_unavailable_source_still_fails(zsh):
    result = zsh(SCANS + '_dev_session_rows() { :; }; _t_remote_move_rows offline')
    assert result.returncode == 1
    assert not result.stdout
