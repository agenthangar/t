# Private adapter: Python owns operation order; effects retain the calling shell.
_t_lifecycle_run() {
  # Nested peers carry machine frames, so only the public controller draws progress.
  if [[ $1 == peer ]]; then _t_lifecycle_run_impl "$@"
  else _t_with_progress "Verifying conversation lifecycle" _t_lifecycle_run_impl "$@"; fi
}
_t_lifecycle_run_impl() {
  local scratch frame code=1 worker_rc=0 action count argument rx tx worker_pid index
  local -a arguments
  scratch=$(mktemp -d) || return 1
  coproc python3 "$T_HOME/libexec/t_lifecycle.py" bridge-shell "$scratch" "$@"
  worker_pid=$!
  exec {rx}<&p {tx}>&p
  {
    while IFS= read -r -d '' frame <&$rx; do
      if [[ $frame == done ]]; then
        IFS= read -r -d '' code <&$rx || code=1
        [[ $code == <-> ]] || code=1
        break
      fi
      if [[ $frame == output ]]; then
        IFS= read -r -d '' argument <&$rx || break
        print -r -- "$argument"
        continue
      fi
      [[ $frame == call ]] || break
      IFS= read -r -d '' action <&$rx || break
      IFS= read -r -d '' count <&$rx || break
      [[ $count == <-> && $count -le 128 ]] || break
      arguments=()
      for (( index=0; index<count; index++ )); do
        IFS= read -r -d '' argument <&$rx || break 2
        arguments+=("$argument")
      done
      worker_rc=0
      : >"$scratch/error"
      if [[ $action == attach ]]; then
        # Interactive terminal ownership must not be redirected into diagnostics.
        _t_lifecycle_effect "$action" "${arguments[@]}" >"$scratch/output" || worker_rc=$?
      else
        _t_lifecycle_effect "$action" "${arguments[@]}" >"$scratch/output" 2>"$scratch/error" || worker_rc=$?
      fi
      # Keep the original error visible and retain a bounded copy in the receipt,
      # including failures before a pane (and therefore its log) can be created.
      cat "$scratch/error" >&2
      print -r -- "$worker_rc" >&$tx || break
    done
    exec {tx}>&-
    tx=
    wait "$worker_pid" 2>/dev/null || true
  } always {
    exec {rx}<&-
    [[ -z $tx ]] || exec {tx}>&-
    rm -rf -- "$scratch"
  }
  return $code
}

_t_lifecycle_effect() {
  local action=$1; shift
  case $action in
    preflight) _dev_launch_preflight "$@" ;;
    new)
      local _T_LIFECYCLE_VERIFY_READY=1
      _dev_new_session_effect "$@" ;;
    resume) _dev_resume_session_effect "$@" ;;
    replace-preflight)
      [[ $(tmux display-message -p -t "=$1:" '#{session_path}') == "$2" &&
         $(tmux display-message -p -t "=$1:" '#{pane_id}') == "$5" &&
         $(tmux display-message -p -t "$5" '#{pane_dead}') == 1 ]] || {
        print -u2 't restart: reserved pane changed; no replacement started'; return 1
      } ;;
    replace)
      tmux respawn-pane -t "$5" -c "$2" "zsh -lic ${(q)6}" || return 1
      tmux set-option -p -t "$5" remain-on-exit "$7" ;;
    ready)
      local session=$1 dir=$2 agent=$3 sid=$4 pid identity current recovery
      if [[ -z $sid ]]; then
        # Fresh CLIs can wait for their first prompt before minting a thread ID.
        # Verify the requested agent or its owned recovery UI, never just a shell.
        identity=$(tmux display-message -p -t "=$session:" '#{session_id}|#{session_path}|#{pane_id}|#{pane_pid}') || return 1
        local -a owner_parts=("${(@s:|:)identity}")
        # Read pane fields from the right; workspace names may contain pipes.
        (( ${#owner_parts} >= 4 )) || return 1
        local owner_dir="${(j:|:)owner_parts[2,-3]}" pane=${owner_parts[-2]} pane_pid=${owner_parts[-1]}
        local -F SECONDS=0
        while (( SECONDS < 15 )); do
          _DEV_PS_AT=0
          _dev_ps_snapshot
          pid=$(_dev_session_claude_pid "$session")
          current=$(tmux display-message -p -t "=$session:" '#{session_id}|#{session_path}|#{pane_id}|#{pane_pid}') || {
            print -u2 "t: $session exited during startup; inspect $HOME/.tmux-logs/$session.log"; return 1
          }
          [[ $current == "$identity" && ${owner_dir:A} == ${dir:A} ]] || return 1
          if [[ -n $pid && $(_dev_agent_of_session "$session") == "$agent" ]]; then
            print -r -- "${owner_parts[1]}|$owner_dir"; return 0
          fi
          if [[ $agent == codex ]]; then
            recovery=$(tmux show-options -pqv -t "$pane" @t_recovery_ui 2>/dev/null)
            if [[ -n $recovery ]] && python3 "$T_HOME/libexec/t_offline.py" --recovery-ready "$recovery" "$pane_pid" "$dir"; then
              # Recheck after inspecting processes; never follow a replaced pane.
              [[ $(tmux display-message -p -t "=$session:" '#{session_id}|#{session_path}|#{pane_id}|#{pane_pid}') == "$identity" &&
                 $(tmux show-options -pqv -t "$pane" @t_recovery_ui 2>/dev/null) == "$recovery" ]] || return 1
              print -r -- "${owner_parts[1]}|$owner_dir"; return 0
            fi
          fi
          [[ $(tmux display-message -p -t "=$session:" '#{pane_dead}') == 0 ]] || {
            print -u2 "t: $session exited during startup; inspect $HOME/.tmux-logs/$session.log"; return 1
          }
          sleep 0.1
        done
        print -u2 "t open: agent startup is not ready; inspect $HOME/.tmux-logs/$session.log"
        return 1
      fi
      _t_history_owner "${session#dev-}" "$sid" "$dir" "$agent" wait ;;
    record)
      local event=$1 session=$2 dir=$3 agent=$4 sid=$5
      if [[ -z $sid && $event == new && -n ${T_LAUNCH_CONTEXT:-} ]]; then
        # The immutable pane was just verified. A fresh Codex may not have a
        # thread yet; its SessionStart hook carries the captured revision later.
        local pid reg registered_dir
        pid=$(_dev_session_claude_pid "$session")
        reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/$pid"
        if [[ -n $pid && -r $reg ]]; then
          IFS=$'\t' read -r sid registered_dir < "$reg"
          [[ ${registered_dir:A} == ${dir:A} ]] || sid=
        fi
      else
        [[ -n $sid ]] || sid=$(_dev_session_sid "$session")
      fi
      [[ $event != new ]] || event=launch
      [[ $event != replace ]] || event=resume
      if [[ $event == (launch|resume) ]]; then
        local fresh=0
        [[ $event == launch ]] && fresh=1
        tmux set-option -t "=$session:" @t_fresh_launch "$fresh" 2>/dev/null || true
      fi
      # Launch history invalidates discovery after its detached publication. Do
      # not wait for cache overlays here or enqueue a launch row that could be
      # delivered after this slot closes. Close/handoff retain their direct rows.
      if [[ ${T_DISCOVERY_COLLECTOR:-} != 1 && -n $session && $event != (launch|resume) ]]; then
        python3 "$T_HOME/bin/t" __cache-record "$event" "$sid" "$agent" "$dir" "${session#dev-}" >/dev/null 2>&1 || true
      fi
      _dev_history_record "$sid" "$agent" "$dir" "$event"
      [[ $event == (launch|resume) ]] && _dev_recovery_watch "$session"
      return 0 ;;
    close-check)
      local session=$1 dir=$2 sid=$3 agent=$4 actual immutable
      immutable=$(tmux display-message -p -t "=$session:" '#{session_id}') || return 1
      [[ $immutable == \$<-> ]] || return 1
      actual=$(tmux display-message -p -t "=$session:" '#{session_path}') || return 1
      [[ ${actual:A} == ${dir:A} && $(_dev_session_sid "$session") == "$sid" &&
          $(_dev_agent_of_session "$session") == "$agent" &&
          $(tmux display-message -p -t "=$session:" '#{session_id}') == "$immutable" ]] || {
        print -u2 't close: selected owner changed; nothing was stopped'; return 1
      }
      print -r -- "$immutable" ;;
    stop)
      local pid immutable=$3
      [[ $immutable == \$<-> &&
         $(tmux display-message -p -t "=$1:" '#{session_id}') == "$immutable" ]] || {
        print -u2 't close: selected owner changed; nothing was stopped'; return 1
      }
      pid=$(_dev_session_claude_pid "$1")
      [[ -z $pid ]] || { _dev_agent_is_service "$pid" && return 1; }
      if [[ -n $4 ]]; then
        _t_terminal_close_guard "$1" "$2" "$4" "$immutable" "$5" "$6" || return 1
        _dev_kill_no_pending_work "$2" && _t_terminal_processes_clear "$1" "$2" || return 1
        [[ $(_dev_session_sid "$1" "$2") == "$7" && $(_dev_agent_of_session "$1") == "$8" &&
           $(tmux display-message -p -t "$immutable:" '#{session_attached}') == 0 ]] || return 1
      fi
      tmux kill-session -t "$immutable:" || return 1
      if [[ -n $pid ]]; then
        _t_wait_for_exit "$pid"
        if kill -0 "$pid" 2>/dev/null && ! _t_pid_is_zombie "$pid"; then
          print -u2 't close: agent has not exited; no release recorded'; return 1
        fi
      fi
      [[ -z $4 && -n $DEV_WORKTREE_ROOT && $2 == $DEV_WORKTREE_ROOT/* ]] && _dev_stop_rooted "$2"
      return 0 ;;
    auto-close-check)
      _t_terminal_close_guard "$1" "$2" "$5" "$6" "$7" "$8" || return 1
      local checked
      checked=$(_t_lifecycle_effect close-check "$1" "$2" "$3" "$4") || return 1
      [[ $checked == "$6" ]] || return 1
      print -r -- "$checked" ;;
    peer)
      local host=$1 payload=$2 worker
      if [[ -z $host ]]; then _t_lifecycle_run peer "$payload"
      else
        worker="_t_lifecycle_run peer ${(q)payload}"
        ssh "$host" "zsh -lic ${(q)worker}"
      fi ;;
    capability)
      [[ -n $1 ]] || return 0
      local worker='python3 "$T_HOME/libexec/t_lifecycle.py" protocol' reply
      reply=$(ssh "$1" "zsh -lic ${(q)worker}") || {
        print -u2 't move: update t on the destination; source was not stopped'; return 1
      }
      [[ $reply == 1 ]] ;;
    copy)
      local source_file=$2 destination_file=$4
      [[ -z $1 ]] || source_file="$1:${(q)2}"
      [[ -z $3 ]] || destination_file="$3:${(q)4}"
      rsync -az -e ssh -- "$source_file" "$destination_file" ;;
    attach)
      if [[ -z $1 ]]; then
        [[ $3 == fg ]] || _t_terminal_close_install "$2"
        if [[ $3 == fg ]]; then _t_pop "$2"; else tmux attach-session -t "=$2:"; fi
      else
        local worker="(( \${+functions[_t_terminal_close_install]} )) && _t_terminal_close_install ${(q)2}; tmux attach-session -t ${(q)"=$2:"}"
        [[ $3 != fg ]] || worker="_t_pop ${(q)2}"
        ssh -t "$1" "zsh -lic ${(q)worker}"
      fi ;;
    transcript) _dev_agent_transcript "$@" ;;
    peer-inspect-saved)
      local T_HISTORY_TOKEN=$1
      _dev_launch_preflight "dev-transfer-$1" "$2" "$3" || return $?
      git -C "$2" remote get-url origin ;;
    peer-inspect)
      local token=$1 dir=$2 sid=$3 agent=$4 row found=0
      local -a f
      for row in "${(@f)$(_dev_session_rows --details)}"; do
        f=("${(@ps:\t:)row}")
        if [[ $f[1] == "$sid" && ${f[2]:A} == ${dir:A} && $f[7] == "$agent" && $f[4] == (attached|detached) ]]; then
          (( found++ ))
        fi
      done
      (( found == 1 )) || { print -u2 't move: source identity changed; nothing was stopped'; return 1; }
      [[ $agent != codex ]] || _dev_codex_rollout_integrity "$sid" || return 1
      git -C "$dir" remote get-url origin ;;
    peer-reserve)
      local token=$1 origin=$2 requested_repo=$3 requested_slot=$4 sid=$5 repo= r candidate dir marker n
      local -a matches
      for r in ${(k)DEV_REPOS}; do
        [[ $(git -C "${DEV_REPOS[$r]}" remote get-url origin 2>/dev/null) == "$origin" ]] && matches+=("$r")
      done
      if (( ${matches[(Ie)$requested_repo]} )); then repo=$requested_repo
      elif (( $#matches == 1 )); then repo=$matches[1]
      else print -u2 't move: destination repository identity is missing or ambiguous'; return 1; fi
      [[ -n $DEV_WORKTREE_ROOT ]] || { print -u2 't move: destination needs isolated worktrees'; return 1; }
      for n in "$requested_slot" {1..99}; do
        [[ $n == <-> ]] || continue
        dir=$(_dev_worktree_path "$repo" "$n") || return 1
        marker="${dir:h}/.${dir:t}.history-reservation"
        if [[ -f $marker && $(<$marker) == "$token" ]]; then print -r -- "$dir"; return 0; fi
        [[ ! -e $dir && ! -e $marker ]] || continue
        _dev_local_slot_live "$repo" "$n" && continue
        git -C "${DEV_REPOS[$repo]}" show-ref --verify --quiet "refs/heads/$(_dev_worktree_branch "$repo" "$n")" && continue
        mkdir -p "${dir:h}" || return 1
        ( setopt noclobber; umask 077; print -r -- "$token" > "$marker" ) 2>/dev/null || continue
        print -r -- "$dir"; return 0
      done
      print -u2 't move: no destination workspace can be reserved'; return 1 ;;
    peer-prepare)
      [[ -n $DEV_WORKTREE_ROOT && $2 == $DEV_WORKTREE_ROOT/*/* ]] || {
        print -u2 't move: source must be an isolated worktree; nothing was stopped'; return 1
      }
      local ref marker="${2:h}/.${2:t}.history-reservation"
      ref=$(_dev_worktree_beam_prepare "$2") || return 1
      if [[ -e $marker ]]; then
        [[ -f $marker && ! -L $marker && $(<$marker) == "$1" ]] || {
          print -u2 't move: source workspace is reserved by another operation'; return 1
        }
      else
        ( setopt noclobber; umask 077; print -r -- "$1" > "$marker" ) || return 1
      fi
      print -r -- "$ref" ;;
    peer-release-source)
      local marker="${2:h}/.${2:t}.history-reservation"
      [[ ! -e $marker ]] && return 0
      [[ -f $marker && ! -L $marker && $(<$marker) == "$1" ]] || return 1
      rm -- "$marker" ;;
    peer-released)
      local row
      local -a f
      for row in "${(@f)$(_dev_session_rows --details)}"; do
        f=("${(@ps:\t:)row}")
        if [[ $f[1] == "$3" || ${f[2]:A} == ${2:A} ]]; then
          print -u2 't move: source has a live owner again; no snapshot published'; return 1
        fi
      done ;;
    peer-stop)
      local token=$1 dir=$2 sid=$3 agent=$4
      local row found=0
      local -a f
      for row in "${(@f)$(_dev_session_rows --details)}"; do
        f=("${(@ps:\t:)row}")
        if [[ ${f[2]:A} == ${dir:A} || $f[1] == "$sid" ]]; then
          [[ $f[1] == "$sid" && ${f[2]:A} == ${dir:A} && $f[7] == "$agent" ]] || {
            print -u2 't move: source owner changed; nothing was stopped'; return 1
          }
          (( found++ ))
        fi
      done
      # No matching process after a lost reply is safe to reconcile; the engine
      # still proves native writer release before publishing or starting a peer.
      (( found )) || return 0
      (( found == 1 )) || return 1
      local stopped_rc=0
      TB_SID=$sid TB_CWD=$dir TB_AGENT=$agent _tbeam_kill_owner && return 0 || stopped_rc=$?
      (( stopped_rc == 1 )) || return $stopped_rc
      local fgpid= fgsid fgcwd fglabel fgagent
      while IFS=$'\t' read -r fgpid fgsid fgcwd fglabel fgagent; do
        [[ $fgsid == "$sid" && ${fgcwd:A} == ${dir:A} && $fgagent == "$agent" ]] || continue
        _dev_agent_is_service "$fgpid" && return 1
        kill -TERM "$fgpid" || return 1
        _t_wait_for_exit "$fgpid"
        if kill -0 "$fgpid" 2>/dev/null; then
          print -u2 't move: source did not exit; no destination started'; return 1
        fi
        return 0
      done < <(_dev_fg_pids)
      print -u2 't move: source still running or changed; no destination started'; return 1 ;;
    peer-launch)
      local token=$1 dir=$2 sid=$3 agent=$4 ref=$5 land repo slot
      local TB_TRANSFER_REF=$ref T_HISTORY_TOKEN=$token T_LIFECYCLE_ID= row
      local -a f
      for row in "${(@f)$(_dev_session_rows --details)}"; do
        f=("${(@ps:\t:)row}")
        if [[ $f[1] == "$sid" || ${f[2]:A} == ${dir:A} ]]; then
          [[ $f[1] == "$sid" && ${f[2]:A} == ${dir:A} && $f[7] == "$agent" ]] || {
            print -u2 't move: destination owner changed; no launch'; return 1
          }
          _t_history_owner "$f[3]" "$sid" "$dir" "$agent" wait >/dev/null || return 1
          print -r -- "dev-$f[3]"; return 0
        fi
      done
      land=$(_dev_beam_land_cwd "$dir" "$sid" '' "$agent") || return 1
      [[ $land == "$dir" ]] || { print -u2 't move: reserved destination changed; no launch'; return 1; }
      _dev_worktree_beam_sync "$land" || return 1
      read -r repo slot < <(_dev_slot_for_cwd "$land")
      [[ -n $repo && -n $slot ]] || return 1
      _dev_resume_session "dev-$repo-$slot" "$land" "$sid" "$agent" history || return $?
      print -r -- "dev-$repo-$slot" ;;
    peer-ready)
      _t_history_owner "${3#dev-}" "$4" "$2" "$5" wait ;;
    peer-finish)
      local token=$1 dir=$2 ref=$3 marker="${2:h}/.${2:t}.history-reservation"
      local TB_TRANSFER_REF=$ref
      _dev_worktree_beam_finish "$dir" || return 1
      [[ ! -e $marker || $(<$marker) != "$token" ]] || rm -- "$marker"
      return 0 ;;
    *) print -u2 't: unsupported lifecycle adapter action'; return 2 ;;
  esac
}

# The source and destination use this same engine, regardless of which host
# received the public command. A self-move runs outside the source process group.
_t_lifecycle_move() {
  local source=$1 destination=$2 dir=$3 sid=$4 agent=$5 repo=$6 slot=$7 detach=$8
  if [[ -z $source && $sid == "$(_dev_self_sid)" ]]; then
    python3 "$T_HOME/libexec/t_lifecycle.py" background move "$@"
  else
    _t_lifecycle_run move "$@"
  fi
}
