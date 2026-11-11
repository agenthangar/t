# Session-open recovery keeps effects in the installed shell's guarded adapters.
_t_open_recover() {
  local mode=$1 repo=$2 slot=$3 yes=${4:-0} rc
  local -x T_OPEN_RECOVERY_AGENT=${5:-}
  python3 "$T_HOME/libexec/t_open_recovery.py" "$mode" "$repo" "$slot" "$yes"; rc=$?
  (( rc == 0 )) || return $rc
  _t_session_attach "dev-$repo-$slot" "$(_dev_agent_of_session "dev-$repo-$slot")"
}

_t_open_recovery_owner() {
  local session=$1 dir=${2:A} identity actual pane dead pid sid agent dead_identity
  tmux has-session -t "=$session:" 2>/dev/null || return 0
  identity=$(tmux display-message -p -t "=$session:" '#{session_id}') || return 1
  actual=$(tmux display-message -p -t "=$session:" '#{session_path}') || return 1
  [[ $identity == \$<-> && ${actual:A} == $dir ]] || {
    print -u2 't open: selected session belongs to a different worktree'; return 1
  }
  pane=$(tmux list-panes -s -t "=$session:" -F '#{pane_id}') || return 1
  [[ $pane == \%<-> ]] || { print -u2 't open: recovery requires one agent pane'; return 1; }
  dead=$(tmux display-message -p -t "$pane" '#{pane_dead}') || return 1
  local _DEV_PS_AT=0
  pid=$(_dev_session_claude_pid "$session")
  sid=$(_dev_session_sid "$session" "$dir")
  agent=$(_dev_agent_of_session "$session")
  dead_identity=$(tmux display-message -p -t "$pane" '#{pane_id} #{pane_pid} #{pane_dead_time} #{pane_dead}') || return 1
  [[ $(tmux display-message -p -t "=$session:" '#{session_id}') == $identity ]] || return 1
  printf '%s\0' "$identity" "$pane" "$dead" "$pid" "$sid" "$agent" "$dead_identity"
}

_t_open_recovery_check() {
  local session=$1 dir=$2 identity=$3 pane=$4 dead=$5 pid=$6 sid=$7 agent=$8 expected=$9 actual up
  actual=$(_t_open_recovery_owner "$session" "$dir") || return 1
  [[ $actual == "$(printf '%s\0' "$identity" "$pane" "$dead" "$pid" "$sid" "$agent" "$expected")" ]] || {
    print -u2 't open: selected owner changed; nothing stopped'; return 1
  }
  [[ $dead == 1 || $pid == <-> ]] || {
    print -u2 't open: could not identify the live client; nothing stopped'; return 1
  }
  if [[ -n $pid ]]; then
    [[ -z $sid || $sid != "$(_dev_self_sid)" ]] || {
      print -u2 't open: run recovery outside the selected agent'; return 1
    }
    _t_restart_client_only "$pid" || return 1
    up=$$
    while [[ $up == <-> && $up -gt 1 ]]; do
      [[ $up != $pid ]] || { print -u2 't open: run recovery outside the selected agent'; return 1; }
      up=$(ps -o ppid= -p "$up" 2>/dev/null); up=${up//[[:space:]]/}
    done
  fi
}

_t_open_recovery_capture() {
  _t_open_recovery_check "$@" || return 1
  tmux capture-pane -p -J -S - -t "$4"
}

_t_open_recovery_stop() {
  _t_open_recovery_check "$@" || return 1
  if [[ $5 == 1 && -z $6 ]]; then
    tmux kill-session -t "$3:"
  else
    _t_lifecycle_run close "$1" "$2" "$7" "$8"
  fi
}

_t_open_recovery_preflight() {
  local session=$1 dir=${2:A} sid=$3 row
  local self_sid=$(_dev_self_sid)
  [[ -z $sid || $sid != $self_sid ]] || {
    print -u2 't open: run recovery outside the selected conversation'; return 1
  }
  local -a f
  _dev_issue_preparing "$dir" && {
    # History reservation is reset below; an actual issue preparation remains guarded.
    local gitdir=$(git -C "$dir" rev-parse --absolute-git-dir 2>/dev/null)
    if [[ -n $gitdir && -f "$gitdir/t-issue-preparing" ]]; then
      print -u2 't open: issue preparation is active; use t issue start'; return 1
    fi
  }
  local rows; rows=$(_dev_rows_all) || {
    print -u2 't open: ownership could not be verified on every host'; return 1
  }
  for row in "${(@f)rows}"; do
    f=("${(@ps:\t:)row}")
    if [[ ( -n $sid && $f[2] == $sid ) || ( $f[1] == local && ${f[3]:A} == $dir ) ]]; then
      print -u2 "t open: $f[4] on $f[1] still owns this conversation or worktree"; return 1
    fi
  done
  if (( ${#REMOTE_HOSTS} )); then
    local remote_owner resolve_rc
    remote_owner=$(_dev_remote_resolve "$4" "$5" '' quiet); resolve_rc=$?
    if [[ -n $remote_owner ]] || (( resolve_rc > 1 )); then
      print -u2 't open: remote owner is present or could not be verified; use --host to recover there'
      return 1
    fi
    remote_owner=$(_dev_remote_legacy_owner "$4" "$5"); resolve_rc=$?
    if [[ -n $remote_owner ]] || (( resolve_rc > 1 )); then
      print -u2 't open: remote reservation must be released on its host'; return 1
    fi
  fi
  return 0
}

