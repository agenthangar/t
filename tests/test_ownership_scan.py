"""Private resume scans preserve identities without building display inventory."""
import pytest

from test_agent_seam import zsh, SID


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_ownership_scan_skips_display_and_resolves_once(zsh, agent):
    cwd = str(zsh.home / 'code/api')
    result = zsh(r'''
      _dev_ps_snapshot() { :; }
      _dev_session_claude_pid() { return 1; }
      _dev_agent_of_session() { print -r -- "$AGENT"; }
      _dev_session_sid() { print -u2 LOOKUP; print -r -- "$SID"; }
      _dev_fg_rows() { [[ $1 == --ownership-handles ]] || return 42; }
      _dev_agent_at_welcome() { print -u2 UNEXPECTED_WELCOME; return 42; }
      _dev_session_summary() { print -u2 UNEXPECTED_SUMMARY; return 42; }
      _transcript_meta_batch() { print -u2 UNEXPECTED_TRANSCRIPTS; return 42; }
      _pr_state_flush() { print -u2 UNEXPECTED_PR; return 42; }
      _dev_session_rows --ownership
    ''', FAKE_SESSION_ROWS=f'dev-api-3\t{cwd}\tdetached', AGENT=agent, SID=SID)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split('\t') == [SID, cwd, 'api-3', 'detached', 'unknown', '-', agent]
    assert result.stderr.count('LOOKUP') == 1
    assert 'UNEXPECTED' not in result.stderr


def test_supplied_current_identity_does_not_probe_pane_again(zsh):
    result = zsh(r'''
      _codex_pane_sid() { print -u2 UNEXPECTED; }
      _dev_session_claude_pid() { print -u2 UNEXPECTED; }
      _dev_agent_of_session() { print -u2 UNEXPECTED; }
      _dev_session_sid dev-api-3 /repo 42 codex "$SID"
    ''', SID=SID)
    assert result.stdout.strip() == SID and not result.stderr


def test_failed_batch_uses_conservative_individual_identity(zsh):
    database = zsh.home / 'database'
    database.touch()
    result = zsh(r'''
      _dev_ps_snapshot() { :; }
      _dev_session_claude_pid() { print 42; }
      _dev_agent_of_session() { print codex; }
      _codex_db() { print -r -- "$DB"; }
      python3() { return 1; }
      _dev_session_sid() { print -u2 "LOOKUP $#"; print -r -- "$SID"; }
      _dev_fg_rows() { :; }
      _dev_session_rows --ownership
    ''', DB=str(database), SID=SID, FAKE_SESSION_ROWS='dev-api-3\t/repo\tdetached')
    assert result.returncode == 0 and 'LOOKUP 2' in result.stderr
    assert result.stdout.startswith(SID + '\t')


@pytest.mark.parametrize('modern', [True, False])
def test_remote_capability_uses_same_invocation_with_legacy_fallback(zsh, modern):
    setup = r'''
      REMOTE_HOSTS=(peer example)
      _dev_session_rows() { print -u2 "LOCAL $*"; }
      _dev_remote_scan_with_timeout() {
        print -r -- "REMOTE $2" >> "$TMUX_LOG"
        zsh -fc '
          _dev_session_rows() { print legacy; }
    '''
    setup += '          _dev_ownership_rows() { print modern; }\n' if modern else ''
    setup += r'''
          eval "$1"
        ' -- "${(Q)${2#zsh -lic }}"
      }
      _dev_rows_all --best-effort --ownership
    '''
    result = zsh(setup)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'peer\t' + ('modern' if modern else 'legacy')
    log = zsh.log.read_text()
    assert log.count('REMOTE zsh -lic ') == 1
    assert '_dev_ownership_rows' in log


def test_foreground_ownership_retains_attach_handle_without_title(zsh):
    reg = zsh.home / '.cache/claude-sessions'
    reg.mkdir(parents=True, exist_ok=True)
    (reg / '4242').write_text(SID + '\t' + str(zsh.home / 'code/api') + '\n')
    result = zsh(r'''
      _dev_ps_snapshot() {
        _DEV_PS_COMM=(4242 codex); _DEV_PS_PPID=(4242 1); _DEV_PANE_PIDS=(other 1)
      }
      _dev_agent_nested() { return 1; }
      _dev_agent_is_service() { return 1; }
      _dev_agent_transcript() { print -u2 UNEXPECTED; }
      _dev_fg_rows --ownership-handles
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split('\t')[2] == 'api:' + SID[:8]
    assert 'UNEXPECTED' not in result.stderr
