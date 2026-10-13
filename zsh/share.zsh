# Fresh Cursor receiving conversations use their own t-cursor-* tmux family.
# Dev slots only support Claude/Codex; this path keeps Cursor's verified
# sessionStart hook and recovery owner checks intact.

_t_share_install_cursor_hook() {
  python3 - "$T_HOME/libexec/t_recovery.py" <<'PY'
import runpy
import sys
runpy.run_path(sys.argv[1])['install_cursor_hook']()
PY
}

_t_share_cursor_new() {
  _dev_with_launch_locks "$2" '' _t_share_cursor_new_claimed "$@"
}

_t_share_cursor_new_claimed() {
  emulate -L zsh
  local session=$1 dir=${2:A} brief=$3 logfile
  [[ $session =~ '^t-cursor-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' ]] || {
    print -u2 -- 't share: invalid Cursor session name'; return 1
  }
  [[ -d $dir && -f $brief && ! -L $brief ]] || {
    print -u2 -- 't share: Cursor workspace or initial prompt is missing'; return 1
  }
  command -v cursor-agent >/dev/null 2>&1 || {
    print -u2 -- 't share: cursor-agent is not installed'; return 1
  }
  _dev_launch_preflight "$session" "$dir" '' || return 1
  _t_share_install_cursor_hook || return 1
  _dev_auto_trust "$dir"
  logfile="$HOME/.tmux-logs/${session}.log"
  mkdir -p "$HOME/.tmux-logs" || return 1
  tmux new-session -d -s "$session" -c "$dir" || return 1
  tmux set-option -t "=$session:" window-size latest 2>/dev/null || {
    tmux kill-session -t "=$session:"; return 1
  }
  tmux pipe-pane -t "=$session:" -o "cat >> ${(q)logfile}" || {
    tmux kill-session -t "=$session:"; return 1
  }
  # The body is read by the pane at startup, never interpolated into this
  # send-keys call or its interactive shell history.
  tmux send-keys -t "=$session:" "unset CLAUDECODE CLAUDE_CODE_SESSION_ID CLAUDE_RESUME_ID CODEX_THREAD_ID CODEX_SESSION_ID CURSOR_SESSION_ID CURSOR_AGENT DEV_AGENT T_ISSUE_TOKEN; exec cursor-agent \"\$(cat -- ${(q)brief})\"" Enter || {
    tmux kill-session -t "=$session:"; return 1
  }
  _dev_launch_startup_check "$session" "$logfile" || return 1
  _dev_recovery_watch "$session"
}
