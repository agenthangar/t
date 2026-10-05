# functions below now forward to `t on`.)

# t — Claude session manager: open/list/move tmux'd Claude sessions (gh-style)
# The single front door (replacing the dev/tpush/tpop/tbeam/tread/tplan/tpaste/
# tfind/on family). This zsh function is the thin SHIM over the bin/t executable:
#   • bin-native verbs (ls, read, plan, paste, kill, on, beam orchestration, and the
#     internal session-rows/land/kill-owner) run straight through via `command t`
#     (the `command` builtin reaches ~/bin/t past this function, dodging the
#     name collision — same trick the claude() wrapper uses).
#   • shell-bound verbs (open/pop/push/resume/find/cd) map to the existing zsh functions,
#     which ALREADY do the cd + `claude -r` in the current terminal and the tpush
#     sentinel handoff correctly *because they run in the calling shell* — a bin
#     subprocess cannot. No resolve protocol is needed: the shim just calls them.
# `t help <verb>` and `t <verb> -h` show the bin's gh-style help. Full per-verb
# help + the verb list live in bin/t.
t() {
  emulate -L zsh
  # Help belongs to the executable, including nested command help. A remote
  # command's own --help is data and must pass through unchanged.
  local a machine_output= help_requested= help_boundary= verb="$1" action="$2"
  [[ $verb == hosts ]] && action=${action:-list}
  for a in "$@"; do
    [[ $a == -- ]] && help_boundary=1
    [[ -z $help_boundary && ( $a == -h || $a == --help ) ]] && help_requested=1
    case $a in --dry-run|--json|--dump|--statusline) machine_output=1 ;; esac
  done
  [[ $verb == hosts && $action == run ]] && help_requested=
  [[ -n $help_requested ]] && { command t "$@"; return; }
  [[ -z $verb ]] && { command t; return; }
  # Only commands that enter a checkout, launch a foreground session, or edit
  # shell settings need local dispatch. The executable owns all other groups.
  local shell_verb=$verb
  local -a shell_args=("${@:2}")
  if [[ $verb == session ]]; then
    shell_verb=
    case $action in
      open) shell_verb=open ;;
      pop) shell_verb=pop ;;
      push) shell_verb=push ;;
      resume) shell_verb=resume ;;
      cd) shell_verb=cd ;;
      move) shell_verb=beam ;;
      search) shell_verb=find ;;
      *) shell_verb= ;;
    esac
    [[ -n $shell_verb ]] && shell_args=("${@:3}")
  elif [[ $verb == repo ]]; then
    shell_verb=
    case $action in
      cd) shell_verb=repos_cd ;;
      create) shell_verb=new ;;
      clone) shell_verb=checkout ;;
    esac
    [[ -n $shell_verb ]] && shell_args=("${@:3}")
  elif [[ $verb == config && -n $action ]]; then
    shell_verb=
    case $action in
      open|show|edit) shell_verb=config; shell_args=("${@:3}") ;;
      setup) shell_verb=setup; shell_args=("${@:3}") ;;
      -*) shell_verb=config ;;
    esac
  elif [[ $verb == agent ]]; then
    shell_verb=
    [[ $action == install ]] && { shell_verb=install; shell_args=("${@:3}"); }
  elif [[ $verb == system ]]; then
    shell_verb=
    case $action in
      integrate|update) shell_verb=$action; shell_args=("${@:3}") ;;
    esac
  elif [[ $verb == hosts ]]; then
    shell_verb=
    case $action in
      add|edit|remove|delete|rm) shell_verb=host_config ;;
      default) [[ -n $3 ]] && shell_verb=host_config ;;
    esac
  elif [[ $verb == profile || $verb == policy || $verb == cursor ]]; then
    shell_verb=
  fi
  # Forward bin-owned groups, including the bare hosts listing. Bare config
  # keeps its settings menu.
  [[ $verb == session || $verb == repo || $verb == cursor || $verb == hosts ||
     $verb == profile || $verb == policy || $verb == agent || $verb == system ||
     ( $verb == config && -n $action ) ]] && [[ -z $shell_verb ]] && {
    command t "$@"; return
  }
  # Shell-bound commands must run in this process. Check before dispatch so a
  # successful update can reload their definitions before using them. The bin
  # checks its own commands; prompting here for those would ask twice.
  local check_update=
  case "$shell_verb" in
    open|pop|push|resume|find|cd|beam|setup|config|host_config|new|checkout|install|integrate)
      check_update=1 ;;
    repos_cd) check_update=1 ;;
    repos) [[ ${shell_args[1]} == cd ]] && check_update=1 ;;
  esac
  if [[ -n $check_update && -z $machine_output && -o interactive && -t 0 && -t 1 && -t 2 \
        && -z ${CI:-} && -z ${CLAUDECODE:-} && -z ${CODEX_THREAD_ID:-} \
        && -z ${T_NO_UPDATE_CHECK:-} && ${T_UPDATE_PROMPTED:-} != $$ ]]; then
    command t __update-prompt
    local update_rc=$?
    case $update_rc in
      0) ;;
      10) _t_reload || return $? ;;
      *) return $update_rc ;;
    esac
    # Suppress nested t calls in this shell. A child shell inherits the marker
    # but has a different PID, so its next interactive command can check again.
    local -x T_UPDATE_PROMPTED=$$
  fi
  case "$shell_verb" in
    open)   _t_open "${shell_args[@]}" ;;           # → _t_dev (local / -r or auto-detect remote attach / -f / fg adopt)
    pop)    _t_pop "${shell_args[@]}" ;;            # → cd + claude -r in THIS terminal
    resume) _t_resume "${shell_args[@]}" ;;         # → revive a dead slot's chat (rebuilds a reaped worktree; --fg → here)
    push)   _t_push "${shell_args[@]}" ;;           # → sentinel handoff; claude() wrapper spawns post-exit
    find)   _t_find "${shell_args[@]}" ;;           # → rank/pick then cd + claude -r here
    cd)     _t_cd "${shell_args[@]}" ;;             # → cd THIS shell into a slot's worktree
    repos)
      if [[ ${shell_args[1]} == cd ]]; then
        _t_repos_cd "${shell_args[@]:1}"
      else
        command t "$@"
      fi ;;
    repos_cd) _t_repos_cd "${shell_args[@]}" ;;
    beam)   _t_beam_xlate "${shell_args[@]}" ;;     # → _t_beam (host moves from --host to a positional)
    # setup runs in the bin (it only edits ${T_LOCAL_RC}), but on success the
    # SHELL must reload so the new cd aliases, host shorthand functions, and the
    # _t_sync_config cache go live at once (precedent: dots reloads every run).
    # T_SETUP_SHIM tells the bin to skip its "source ~/.zshrc" hint.
    setup)  _t_install setup "$@" ;;
    config) _t_install config "$@" ;;
    host_config) _t_install hosts "$@" ;;
    # new writes DEV_REPOS too (the repo it just created) → the same reload.
    new)    T_SETUP_SHIM=1 command t "$@" && _t_reload ;;
    checkout) _t_install checkout "$@" ;;
    # install can END in `t setup` (it opens it when ~/code holds repos DEV_REPOS does
    # not know yet), so it owes the same reload — but only when that setup actually
    # wrote: ${T_LOCAL_RC}'s mtime is the evidence, since install's rc says nothing
    # about it (quitting setup is not an install failure, and most runs never open it).
    install) _t_install install "$@" ;;
    integrate) T_SETUP_SHIM=1 command t "$@" && _t_reload ;;
    update) T_SETUP_SHIM=1 command t "$@" && _t_reload ;;
    *)      command t "$@" ;; # bin-owned canonical and legacy commands
  esac
}

# Enter a registered checkout, including aliases such as `t` that cannot be
# generated as shell shortcuts because they name the command itself.
_t_repos_cd() {
  emulate -L zsh
  if (( $# != 1 )); then
    print -u2 -- 'usage: t repos cd <repo>'
    return 2
  fi
  local repo=$1 dir=${DEV_REPOS[$1]-}
  if [[ -z $dir ]]; then
    print -u2 -- "t repos cd: unknown repo '$repo' (known: ${(j:, :)${(ok)DEV_REPOS}})"
    return 1
  fi
  if [[ ! -d $dir ]]; then
    print -u2 -- "t repos cd: checkout does not exist: $dir"
    return 1
  fi
  builtin cd -- "$dir"
}

# _t_install <reload-kind> <command...> — reload iff ${T_LOCAL_RC} changed.
# Compare content too: an editor can save the same size inside one clock tick.
_t_install() {
  local before= after= verb="$1"
  shift
  [[ -f ${T_LOCAL_RC} ]] && before=$(<${T_LOCAL_RC})
  T_SETUP_SHIM=1 command t "$@"
  local rc=$?
  [[ -f ${T_LOCAL_RC} ]] && after=$(<${T_LOCAL_RC})
  if [[ $before != $after && ( $verb != config || $rc == 0 ) ]]; then
    if [[ $verb == config ]]; then
      # A removed registration must disappear from this shell as well as the next
      # one. Rebuild only the known local settings and their generated shortcuts.
      local key
      for key in ${(k)DEV_REPOS}; do unalias "$key" 2>/dev/null; done
      for key in ${(k)REMOTE_HOSTS}; do unfunction "$key" 2>/dev/null; done
      DEV_REPOS=() DEV_BRANCHES=() REMOTE_HOSTS=() DEV_WORKTREE=() DEV_AGENT=() DEV_MODEL=() DEV_EFFORT=() DEV_FAST=()
      unset DEV_AGENT_DEFAULT DEV_BRANCH DEV_WORKTREE_ROOT DEV_WORKTREE_DEFAULT TBEAM_HOST MINI_HOST
    fi
    _t_reload
  fi
  return $rc
}

# _t_cd — cd the CALLING shell into a dev slot's worktree (gh-grammar `t cd [repo] [slot]`).
#   t cd <repo> <slot>   → that slot's worktree ($DEV_WORKTREE_ROOT/<basename>/<slot>)
#   t cd <slot>          → that slot of the repo cwd is in (mirrors t read/paste)
#   t cd <repo>          → the repo's only worktree; several → fzf pick; none → the repo dir
#   t cd                 → fzf-pick across every worktree on disk
# Navigation only: it never creates a worktree (`t open` owns creation) and never
# touches tmux — a missing slot is an error with the `t open` hint. The picker shows
# the branch actually checked out in each worktree (worktrees parked on a non-dev/*
# branch by hand are common), read via git, not derived from the slot name.
_t_cd() {
  emulate -L zsh
  local repo="$1" slot="$2"
  # slot-only shorthand: `t cd 4` ≡ slot 4 of the repo cwd is in
  if [[ $repo == <-> && -z $slot ]]; then
    slot=$repo
    repo=$(_t_infer_repo "$slot") || {
      echo "t cd: not inside a DEV_REPOS repo — name one: t cd <repo> $slot" >&2; return 1 }
  fi
  if [[ -n $repo && -z ${DEV_REPOS[$repo]:-} ]]; then
    echo "t cd: unknown repo '$repo' (known: ${(k)DEV_REPOS})" >&2; return 1
  fi
  if [[ -n $repo && -n $slot ]]; then
    local wt; wt=$(_dev_worktree_path "$repo" "$slot")
    [[ -e $wt/.git ]] || {
      echo "t cd: no worktree at $wt  ('t open $repo $slot' creates one)" >&2; return 1 }
    cd "$wt"; return
  fi
  # No slot: pick among the worktrees that exist on disk — the named repo's, else all.
  # Match the rest of the worktree tooling (_dev_worktree_create / _dev_worktree_sweep_run):
  # require .git so a leftover / hand-made numeric dir under $DEV_WORKTREE_ROOT is not offered.
  local -a wts
  if [[ -n $repo ]]; then
    wts=("$DEV_WORKTREE_ROOT/${DEV_REPOS[$repo]:t}"/<->(N/ne:'[[ -e $REPLY/.git ]]':))
    (( ${#wts} )) || { cd "${DEV_REPOS[$repo]}"; return }   # none yet → the repo itself
  else
    wts=("$DEV_WORKTREE_ROOT"/*/<->(N/ne:'[[ -e $REPLY/.git ]]':))
    (( ${#wts} )) || {
      echo "t cd: no worktrees under $DEV_WORKTREE_ROOT ('t open <repo>' creates one)" >&2; return 1 }
  fi
  (( ${#wts} == 1 )) && { cd "${wts[1]}"; return }
  if [[ -t 0 && -t 1 ]] && command -v fzf >/dev/null 2>&1; then
    local wt br line; local -a rows
    for wt in "${wts[@]}"; do
      br=$(git -C "$wt" branch --show-current 2>/dev/null)
      rows+=("${wt}"$'\t'"${wt#$DEV_WORKTREE_ROOT/}"$'\t'"${br:-?}")
    done
    line=$(print -rl -- "${rows[@]}" \
             | _t_fzf --with-nth=2.. --delimiter='\t' --prompt='t cd > ' --height=40% --reverse) || return 1
    cd "${line%%$'\t'*}"; return
  fi
  local -a short; short=("${wts[@]#$DEV_WORKTREE_ROOT/}")
  print -rl -- "t cd: several worktrees — name one (t cd <repo> <slot>):" \
    "${short[@]/#/  }" >&2
  return 1
}

# _t_open — map gh-grammar `t open <repo> [slot] [--new|--fg|--remote] [--host H]`
# onto the existing `dev` grammar: --new→the `new` slot keyword, --fg→-f, --remote→-r;
# repo/slot/-r/-f pass through (dev parses flags in any position).
#
# Remote model (--remote and --host are CONSOLIDATED, two angles on one thing):
#   --host H names WHICH host; -r/--remote means "remote, pick the host for me".
#   • -r --new (no --host)  → START a fresh session on the default host (_dev_default_host,
#                             the first $REMOTE_HOSTS); name one with --host to override.
#   • --host H [--new]      → start/attach on H (the explicit-host path; -r is redundant
#                             here and is dropped before forwarding).
#   • -r (no --new)         → cross-host ATTACH of a live slot (host auto-inferred); handled
#                             by _t_dev/_dev_remote below, NOT here.
# The host paths dispatch via _dev_remote_open with the original gh-style args (minus
# --host / -r), so H's own `t open` re-parses them from ITS $PWD.
_t_open() {
  local -a a rest mode_pos; local arg want_host= host= remote= isnew= local_only= app= cli= cli_intent=
  for arg in "$@"; do
    if [[ -n $want_host ]]; then host=$arg; want_host=; continue; fi
    case "$arg" in
      --host)   want_host=1; continue ;;
      --host=*) host=${arg#--host=}; continue ;;
    esac
    rest+=("$arg")
    case "$arg" in
      --new|new)         a+=(new); isnew=1 ;;
      --app)             app=1 ;;
      --cli)             cli=1 ;;
      --fg)              cli_intent=1; a+=(-f) ;;
      --claude)          cli_intent=1; a+=("$arg") ;;
      -r|--remote)       remote=1; a+=(-r) ;;
      -l|--local|--here) local_only=1; a+=(--local) ;;
      --codex)           a+=("$arg") ;;
      *)                 a+=("$arg"); [[ $arg == -* ]] && cli_intent=1 ;;
    esac
  done
  [[ -n $want_host ]] && { echo "t open: --host requires a value" >&2; return 2; }
  [[ -n $app && -n $cli ]] && { echo "t open: --app and --cli are mutually exclusive" >&2; return 2; }

  # Explicit CLI/remote intents take precedence over reservations and defaults.
  if [[ -z $app && -z $cli && -z $cli_intent && -z $host && -z $remote ]]; then
    for arg in "${rest[@]}"; do
      [[ $arg == -* ]] || mode_pos+=("$arg")
    done
    local mode_repo=${mode_pos[1]:-} mode_slot=${mode_pos[2]:-}
    # Foreground selectors and conversation IDs continue through CLI routing.
    if [[ ( -n ${DEV_REPOS[$mode_repo]:-} || -z $mode_repo || $mode_repo == <-> || $mode_repo == new ) &&
          ( -z $mode_slot || $mode_slot == <-> || $mode_slot == new ) ]]; then
      if [[ -z ${DEV_REPOS[$mode_repo]:-} ]]; then
        [[ $mode_repo == <-> && -z $mode_slot ]] && mode_slot=$mode_repo
        mode_repo=$(_t_infer_repo "$mode_repo")
        if [[ -z $mode_slot && -z $isnew ]]; then
          local mode_row; mode_row=$(_dev_repo_of_dir "$PWD" 2>/dev/null)
          [[ $mode_row == *$'\t'* ]] && mode_slot=${mode_row#*$'\t'}
        fi
      fi
      # A desktop reservation is the slot's owner, even when new slots default
      # to CLI. Reuse the app path so reopening never freshens its worktree.
      if [[ -n ${DEV_REPOS[$mode_repo]:-} && $mode_slot == <-> && -z $isnew ]] &&
          _dev_app_slot_reserved "$(_dev_worktree_path "$mode_repo" "$mode_slot")"; then
        app=1
      elif [[ ${DEV_OPEN_MODE[$mode_repo]:-$DEV_OPEN_MODE_DEFAULT} == app ]]; then
        app=1
      fi
    fi
  fi

  if [[ -n $app ]]; then
    _t_open_app "${rest[@]}" ${host:+--host "$host"}
    return
  fi

  # -l/--local/--here and -r/--remote are opposite intents — reject the combination
  # here too, so the `-r --new` default-host shortcut below cannot silently win over a
  # user-forced local. (_t_dev re-checks the same for its fall-through path.)
  if [[ -n $local_only && -n $remote ]]; then
    echo "t open: --local/--here and -r/--remote are mutually exclusive" >&2
    return 2
  fi

  # `-r --new` with no --host: START fresh on the default remote host. (Plain `-r`
  # with no --new falls through to _t_dev's cross-host ATTACH; --host below wins if set.)
  if [[ -z $host && -n $remote && -n $isnew ]]; then
    host=$(_dev_default_host) || {
      echo "t open -r --new: no remote hosts configured (set REMOTE_HOSTS in ${T_LOCAL_RC})." >&2
      return 1
    }
    echo "(no --host given — starting on $host; pass --host <h> to choose another)"
  fi

  if [[ -n $host ]]; then
    # The remote `t open` re-parses these positionals from $host's own $PWD
    # (login dir, not this laptop's repo tree), so apply _t_dev's repo-aware
    # rewrite HERE — a lone slot/keyword or bare `t open --host h` would
    # otherwise hit the wrong repo (or none) on the far side. -r/--remote is
    # dropped: the host is already chosen, and forwarding it would make the far
    # side try its OWN remote attach instead of acting locally.
    local -a flags pos
    for arg in "${rest[@]}"; do
      case "$arg" in
        -r|--remote) ;;
        -l|--local|--here) ;;   # --host already names the machine; "force local" is moot
        --*) flags+=("$arg") ;;
        *)   pos+=("$arg") ;;
      esac
    done
    local p1=${pos[1]:-} p2=${pos[2]:-}
    if [[ -z ${DEV_REPOS[$p1]:-} && ( $p1 == <-> || $p1 == new || $p1 == fg ) \
          && ( -z $p2 || $p2 == new || $p2 == fg ) ]]; then
      p2=$p1; p1=
    fi
    [[ -z $p1 ]] && p1=$(_t_infer_repo "$p2")
    rest=()
    [[ -n $p1 ]] && rest+=("$p1")
    [[ -n $p2 ]] && rest+=("$p2")
    (( ${#pos} > 2 )) && rest+=("${pos[@]:2}")
    rest+=("${flags[@]}")
    # A remote open remains a CLI operation even when that host prefers its app.
    (( ${flags[(Ie)--cli]} )) || rest+=(--cli)
    _dev_remote_open "$host" "${rest[@]}"
    return
  fi
  _t_dev "${a[@]}"
}

# Open a dedicated t worktree in the Codex desktop app. A running Codex CLI slot
# uses `t app` so its conversation has only one owner; a new workspace needs no
# tmux pane or throwaway CLI process.
_t_open_app() {
  local repo= slot= arg isnew= claude= host= want_host= inferred= reopen=
  local -a pos
  for arg in "$@"; do
    if [[ -n $want_host ]]; then host=$arg; want_host=; continue; fi
    case "$arg" in
      --app) ;;
      --new|new) isnew=1 ;;
      --codex) ;;
      --claude) claude=1 ;;
      --host) want_host=1 ;;
      --host=*) host=${arg#--host=} ;;
      --fg|-f|--no-tmux|-r|--remote|-l|--local|--here)
        print -u2 -- "t open --app: --app opens a local desktop workspace; omit '$arg'"
        return 2 ;;
      -*) print -u2 -- "t open --app: unknown flag '$arg'"; return 2 ;;
      *) pos+=("$arg") ;;
    esac
  done
  [[ -z $want_host && -z $host ]] || {
    print -u2 -- 't open --app: remote desktop launch is not supported; run this on the Mac that owns the app'
    return 2
  }
  [[ -z $claude ]] || {
    print -u2 -- 't open --app: --claude conflicts with the Codex desktop app'
    return 2
  }
  [[ $OSTYPE == darwin* ]] || {
    print -u2 -- 't open --app: the Codex desktop app requires macOS'
    return 1
  }
  (( ${#pos} <= 2 )) || { print -u2 -- 't open --app: expected a repo and optional slot number'; return 2; }
  repo=${pos[1]:-}; slot=${pos[2]:-}
  if [[ $repo == <-> && -z $slot ]]; then slot=$repo; repo=; fi
  [[ -z $repo ]] && inferred=1
  [[ -n $repo ]] || repo=$(_t_infer_repo "$slot")
  [[ -n $repo && -n ${DEV_REPOS[$repo]:-} && -d ${DEV_REPOS[$repo]} ]] || {
    print -u2 -- 't open --app: name a registered repository with an existing checkout'
    return 1
  }
  [[ -z $slot || $slot == <-> ]] || {
    print -u2 -- 't open --app: slot must be a number'
    return 2
  }
  [[ -z $slot || -z $isnew ]] || {
    print -u2 -- 't open --app: choose a slot number or --new, not both'
    return 2
  }
  if [[ -n $inferred && -z $slot && -z $isnew ]]; then
    local row here_slot
    row=$(_dev_repo_of_dir "$PWD" 2>/dev/null)
    here_slot=${row#*$'\t'}
    [[ $row == *$'\t'* && -n $here_slot ]] && slot=$here_slot
  fi
  _dev_worktree_enabled "$repo" || {
    print -u2 -- "t open --app: $repo has worktrees disabled; enable them to isolate the desktop workspace"
    return 1
  }
  command -v codex >/dev/null 2>&1 || {
    print -u2 -- 't open --app: Codex CLI is missing (run t install codex)'
    return 1
  }
  if [[ -z $slot ]]; then
    local n=1
    while ! _dev_slot_fresh "$repo" "$n"; do (( n++ )); done
    slot=$n
  fi
  # Aliases pointing at one canonical checkout share slot ownership. Handoff the
  # existing owner's session name rather than opening its worktree a second time.
  if ! tmux has-session -t "=dev-${repo}-${slot}:" 2>/dev/null; then
    local sibling
    for sibling in ${(k)DEV_REPOS}; do
      [[ $sibling == $repo || ${DEV_REPOS[$sibling]} != ${DEV_REPOS[$repo]} ]] && continue
      if tmux has-session -t "=dev-${sibling}-${slot}:" 2>/dev/null; then
        repo=$sibling
        break
      fi
    done
  fi
  local session="dev-${repo}-${slot}" dir
  if tmux has-session -t "=$session:" 2>/dev/null; then
    [[ $(_dev_agent_of_session "$session") == codex ]] || {
      print -u2 -- "t open --app: $session is running Claude; choose --new or another slot"
      return 1
    }
    command t app "$repo" "$slot"
    return
  fi
  if (( ${#REMOTE_HOSTS} )); then
    local remote_owner
    remote_owner=$(_dev_remote_app_owner "$repo" "$slot")
    if [[ -n $remote_owner ]]; then
      print -u2 -- "t open --app: $repo $slot is reserved by the Codex desktop app on $remote_owner; close it there and release the reservation before opening here"
      return 1
    fi
    remote_owner=$(_dev_remote_resolve "$repo" "$slot" 2>/dev/null)
    if [[ -n $remote_owner ]]; then
      print -u2 -- "t open --app: $repo $slot is live on ${remote_owner%%$'\t'*}; move it here with t beam before opening the app"
      return 1
    fi
  fi
  dir=$(_dev_worktree_path "$repo" "$slot")
  if _dev_app_slot_reserved "$dir"; then
    reopen=1 # Preserve its branch and reopen the saved conversation.
    # A foreground CLI can own a reserved workspace without a tmux slot. Do not
    # load that conversation a second time in the app. Shared backends are not
    # CLI owners; their cwd can belong to an unrelated conversation.
    local pid workspace_pids
    workspace_pids=$(_dev_cwd_pids "$dir") || {
      print -u2 -- 't open --app: could not check workspace processes; retry before reopening'
      return 1
    }
    _DEV_PS_AT=0
    _dev_ps_snapshot || {
      print -u2 -- 't open --app: could not check terminal agents; retry before reopening'
      return 1
    }
    for pid in ${(f)workspace_pids}; do
      [[ -n ${_DEV_PS_COMM[$pid]:-} ]] || {
        kill -0 "$pid" 2>/dev/null || continue # The cwd probe itself may have exited.
        print -u2 -- 't open --app: workspace processes changed; retry before reopening'
        return 1
      }
      [[ -n ${_DEV_PS_APP[$pid]:-} ]] && continue
      _dev_agent_is_proc "${_DEV_PS_COMM[$pid]:-}" || continue
      _dev_agent_is_service "$pid" && continue
      print -u2 -- "t open --app: a terminal agent still owns this workspace (pid $pid); exit it before reopening in the app"
      return 1
    done
  else
    dir=$(_dev_worktree_create "$repo" "$slot")
  fi
  [[ -n $dir && -e $dir/.git ]] || { _dev_worktree_refuse "$repo" "$slot"; return 1; }
  _dev_auto_trust "$dir"
  _dev_app_slot_reserve "$dir" || {
    print -u2 -- "t open --app: could not reserve $dir against automatic cleanup"
    return 1
  }
  print -r -- "Opening $repo $slot in the Codex desktop app: $dir"
  command t _app-workspace "$dir" ${reopen:+--reopen} || {
    print -u2 -- "t open --app: app launch failed; the worktree remains reserved at $dir for a retry"
    return 1
  }
}

# _t_beam_xlate — map gh-grammar `t beam [repo] [slot] [--host H] [flags]` onto the
# _t_beam impl's `[repo [slot]] [host]` positional grammar (--host's value moves to the
# end as the trailing host positional); -f/--fg/-d/-p/-a/-s and --from <h> (the receive
# flag) pass through unchanged — _t_beam parses --from itself.
_t_beam_xlate() {
  local -a a; local arg host want_host=
  for arg in "$@"; do
    if [[ -n $want_host ]]; then host=$arg; want_host=; continue; fi
    case "$arg" in
      --host) want_host=1 ;;
      *)      a+=("$arg") ;;
    esac
  done
  _t_beam "${a[@]}" ${host:+"$host"}
}

# help — show this command list, grouped by purpose
# Each command's name + description are parsed live from the leading
# `# name … — description` comment above each ~/.zshrc function and the header
# line of each ~/bin script, so descriptions stay current as you add commands.
# Grouping is the `groups` list below; anything not placed there shows under
# "Other" so it's never hidden — except the few internal/automatic commands in
# the `_hide` list (a transparent wrapper, a hook, a guard), which are dropped
# entirely since you never invoke them by hand.
# (zsh's own help is `run-help` / ESC-h; this doesn't touch it.)
# --- tab completion for our commands -------------------------------------
# compinit already ran at the top of this file, so compdef is available here.
# These helper names start with `_` so the `help` parser above skips them. (csync
# takes no args, so it needs no completion.)
#
# `_t` — subcommand-aware completion for the single `t` command: verbs at position
# 1; then the per-verb positional (a DEV_REPOS key for repo verbs, a REMOTE_HOSTS
# key for `on`), and slot/flags after. Pulls live from the ${(k)DEV_REPOS} /
# ${(k)REMOTE_HOSTS} arrays so it stays current with ${T_LOCAL_RC}.
_t() {
  local -a nouns=(session repo cursor hosts config profile policy agent system help)
  if (( CURRENT == 2 )); then
    _describe -t nouns 't command group' nouns
    return
  fi
  local group=${words[2]} action=${words[3]} first=4
  if [[ $group == help ]]; then
    if (( CURRENT == 3 )); then
      local -a topics=($nouns agents aliases)
      _describe -t topics 't help topic' topics
      return
    fi
    group=${words[3]} action=${words[4]} first=5
  fi
  local -a actions
  case $group in
    session) actions=(open list close restart push pop resume cd move read view-plan paste search open-app) ;;
    repo) actions=(list cd locate create clone) ;;
    cursor) actions=(list resume send) ;;
    hosts) actions=(list show add edit remove default run) ;;
    config) actions=(open show edit setup) ;;
    profile) actions=(init edit show apply) ;;
    policy) actions=(check show apply) ;;
    agent) actions=(install status trust) ;;
    system) actions=(integrate update diagnose) ;;
  esac
  [[ $group == hosts && ( $action == delete || $action == rm ) ]] && action=remove
  if (( ${#actions} )) && (( CURRENT == first - 1 )); then
    if [[ $group == hosts && ${words[CURRENT]} == -* ]]; then
      _values 'flag' --json -h --help
      return
    fi
    if [[ $group == config && ${words[CURRENT]} == -* ]]; then
      _values 'legacy flag' --show --edit -h --help
      return
    fi
    _describe -t actions 't action' actions
    return
  fi
  # Old spellings remain usable once typed, but are not offered at top level.
  case $group in
    open|ls|kill|restart|push|pop|resume|cd|beam|read|plan|paste|find|app)
      action=$group; group=session; first=3 ;;
    repos)
      group=repo; first=4
      [[ $action == ls ]] && action=list
      [[ $action == path ]] && action=locate
      (( CURRENT == 3 )) && { _values 'action' ls cd path; return; } ;;
    new) group=repo; action=create; first=3 ;;
    checkout) group=repo; action=clone; first=3 ;;
    on) group=hosts; action=run; first=3 ;;
    setup) group=config; action=setup; first=3 ;;
    instructions) group=profile; action=apply; first=3 ;;
    permissions) group=policy; action=check; first=3 ;;
    install|trust) action=$group; group=agent; first=3 ;;
    integrate|update|doctor) action=$group; [[ $action == doctor ]] && action=diagnose; group=system; first=3 ;;
  esac
  if [[ $group == session && $first == 3 ]]; then
    case $action in
      ls) action=list ;; kill) action=close ;; beam) action=move ;;
      plan) action=view-plan ;; find) action=search ;; app) action=open-app ;;
    esac
  fi
  case "$group/$action" in
    repo/list|repo/cd|repo/locate)
      if (( CURRENT == first )) && [[ $action == cd || $action == locate ]]; then _values 'repo' ${(k)DEV_REPOS}
      else _values 'flag' -h --help; fi ;;
    session/open-app)
      local -a app_flags=(--url --no-preview --plan --no-plan --reuse-window --dry-run -h --help)
      local app_direction=${words[first]}
      [[ $app_direction == pull ]] && app_flags=(--thread --dry-run -h --help)
      if [[ $app_direction == push || $app_direction == pull ]] && (( CURRENT > first )); then
        (( first++ ))
      fi
      if [[ ${words[CURRENT-1]} == --url ]]; then _message 'preview URL'
      elif [[ ${words[CURRENT-1]} == --plan ]]; then _files -g '*.md'
      elif [[ ${words[CURRENT-1]} == --thread ]]; then _message 'saved conversation ID'
      elif [[ ${words[CURRENT]} == -* ]]; then _values 'flag' $app_flags
      elif (( CURRENT == first )); then
        if [[ $app_direction == push || $app_direction == pull ]]; then _values 'repo' ${(k)DEV_REPOS}
        else _values 'direction or repo' push pull ${(k)DEV_REPOS}; fi
      elif (( CURRENT == first + 1 )); then _message 'local slot number'
      else _values 'flag' $app_flags; fi ;;
    session/restart)
      if [[ ${words[CURRENT]} == -* ]]; then _values 'flag' --dry-run -h --help
      elif (( CURRENT == first )); then _values 'repo' ${(k)DEV_REPOS}
      elif (( CURRENT == first + 1 )); then _message 'local slot number'
      else _values 'flag' --dry-run -h --help; fi ;;
    cursor/list|cursor/resume|cursor/send)
      if [[ ${words[CURRENT-1]} == --host || ${words[CURRENT-1]} == --from ]]; then
        _values 'host' ${(k)REMOTE_HOSTS}
      elif [[ $group/$action == cursor/list ]]; then
        _values 'flag' --host -h --help
      elif [[ $group/$action == cursor/resume ]]; then
        (( CURRENT == first )) && [[ ${words[CURRENT]} != -* ]] && _message 'chat id or unique prefix' || _values 'flag' --host -h --help
      else
        (( CURRENT == first )) && [[ ${words[CURRENT]} != -* ]] && _message 'chat id or unique prefix' || _values 'flag' --host --from -p --pick -a --attach -h --help
      fi ;;
    session/open|session/close|session/read|session/view-plan|session/paste|session/move|session/resume|session/cd|session/pop)
      if [[ ${words[CURRENT-1]} == --host || ${words[CURRENT-1]} == --from ]]; then
        _values 'host' ${(k)REMOTE_HOSTS}
      elif [[ ${words[CURRENT-1]} == --url ]]; then _message 'preview URL'
      elif [[ ${words[CURRENT-1]} == --plan ]]; then _files -g '*.md'
      elif [[ ${words[CURRENT]} == -* ]] || (( CURRENT > first + 1 )); then
        local -a flags=(-h --help)
        case $action in
          open) flags+=(--new --fg --app --cli -l --local --here -r --remote --host --codex --claude -y --yes) ;;
          close) flags+=(-y --yes -r --remote) ;;
          read) flags+=(-d --dump -n --tail) ;;
          view-plan) flags+=(-a --all) ;;
          paste) flags+=(-p --pick -n --newest) ;;
          move) flags+=(--host --from --here --fg -d --detach -p --pick -a --all -s --session) ;;
          resume) flags+=(-f --fg -a --all -l --live -r --remote --days --host) ;;
        esac
        _values 'flag' "${flags[@]}"
      elif (( CURRENT == first )); then _values 'repo' ${(k)DEV_REPOS}
      else _values 'slot' 1 2 3 4; fi ;;
    hosts/run)
      (( CURRENT == first )) && _values 'host' ${(k)REMOTE_HOSTS} || _normal ;;
    hosts/show|hosts/remove|hosts/default|hosts/edit)
      if (( CURRENT == first )) && [[ ${words[CURRENT]} != -* ]]; then
        _values 'host' ${(k)REMOTE_HOSTS}
      elif [[ $group/$action == hosts/edit && $CURRENT == $(( first + 1 )) && ${words[CURRENT]} != -* ]]; then
        _message 'SSH config name, address or user@host'
      elif [[ $action == default ]]; then _values 'flag' --clear -h --help
      elif [[ $action == edit ]]; then _values 'flag' --default -h --help
      else _values 'flag' -h --help; fi ;;
    hosts/add)
      if [[ ${words[CURRENT]} == -* ]]; then _values 'flag' --default -h --help
      elif (( CURRENT == first )); then _message 'new host alias'
      elif (( CURRENT == first + 1 )); then _message 'SSH config name, address or user@host'
      else _values 'flag' --default -h --help; fi ;;
    hosts/list)
      _values 'flag' --json -h --help ;;
    session/list)
      if (( CURRENT == first )) && [[ ${words[CURRENT]} != -* ]]; then _values 'repo' ${(k)DEV_REPOS}
      else _values 'flag' -r --remote -a --all -h --help; fi ;;
    session/push)
      _values 'flag' -p --pick -a --all -h --help ;;
    session/search)
      _values 'flag' -k --keyword -h --help ;;
    config/setup)
      if [[ ${words[CURRENT]} == -* ]]; then _values 'flag' --hosts --no-hosts --instructions --no-instructions --dry-run -h --help
      else _files -/; fi ;;   # scan-dir arguments
    config/*)
      _values 'flag' -h --help ;;
    repo/create)
      if (( CURRENT == first )) && [[ ${words[CURRENT]} != -* ]]; then _message 'repo name'
      else _values 'flag' --owner --public --private --alias --hosts --no-hosts -y --yes --dry-run -h --help; fi ;;
    repo/clone)
      if [[ ${words[CURRENT-1]} == --path ]]; then _files -/
      elif [[ ${words[CURRENT]} == -* ]]; then _values 'flag' --path --instructions --no-instructions --dry-run -h --help
      elif (( CURRENT == first )); then _message 'GitHub clone URL'
      elif (( CURRENT == first + 1 )); then _message 'local alias'
      else _values 'flag' --path --instructions --no-instructions --dry-run -h --help; fi ;;
    profile/init|profile/edit|profile/show)
      _values 'flag' -h --help ;;
    profile/apply)
      if [[ ${words[CURRENT]} == -* ]]; then _values 'flag' -h --help
      else _values 'repo' ${(k)DEV_REPOS}; fi ;;
    agent/install)
      if [[ ${words[CURRENT]} == -* ]]; then _values 'flag' --update --reinstall --no-login --headless --hosts --no-hosts -y --yes --dry-run -h --help
      else _values 'agent' claude codex cursor; fi ;;
    agent/trust)
      if [[ ${words[CURRENT]} == -* ]]; then _values 'flag' -a --all --status -q --quiet -h --help
      else _files -/; fi ;;
    policy/check|policy/apply)
      _values 'flag' --defaults -h --help ;;
    policy/show|agent/status|system/integrate|system/diagnose)
      _values 'flag' -h --help ;;
    system/update)
      _values 'flag' --dev --local --relink --check -h --help ;;
  esac
}
