#!/bin/zsh
# Private Terminal handoff for the optional rnnr app, not a public t command.
emulate -LR zsh
setopt no_aliases

# Pure ownership verification. An empty expected SID only matches an empty SID.
_rnnr_guard() {
  local actual_id actual_dir actual_sid actual_name attached
  actual_id=$(command tmux display-message -p -t "=$_rnnr_name:" '#{session_id}') || return 1
  [[ $actual_id == \$<-> && ( -z $_rnnr_id || $actual_id == "$_rnnr_id" ) ]] || return 1
  actual_name=$(command tmux display-message -p -t "$actual_id:" '#{session_name}') || return 1
  [[ $actual_name == "$_rnnr_name" ]] || return 1
  actual_dir=$(command tmux display-message -p -t "$actual_id:" '#{session_path}') || return 1
  [[ $actual_dir == "$_rnnr_cwd" ]] || return 1
  actual_sid=$(_dev_session_sid "$_rnnr_name" "$_rnnr_cwd") || return 1
  [[ $actual_sid == "$_rnnr_sid" ]] || return 1
  attached=$(command tmux display-message -p -t "$actual_id:" '#{session_attached}') || return 1
  [[ $attached == 0 ]] || return 1
  [[ $(command tmux display-message -p -t "=$_rnnr_name:" '#{session_id}') == "$actual_id" ]] || return 1
  _rnnr_id=$actual_id
}

# Also serialized to a registered peer, so every host runs the same checks.
_rnnr_target() {
  emulate -L zsh
  setopt no_aliases
  local operation=$1 slot=$2 _rnnr_cwd=$3 _rnnr_sid=$4
  local _rnnr_name="dev-$slot" _rnnr_id=
  [[ $operation == (attach|end) && $slot =~ '^[A-Za-z0-9_][A-Za-z0-9_.-]*-[0-9]+$' &&
     $_rnnr_cwd == /* && $_rnnr_cwd != *[[:cntrl:]]* && $_rnnr_sid != *[[:cntrl:]]* ]] || return 1
  (( ${+functions[_dev_session_sid]} )) || return 1
  _rnnr_guard || { print -u2 'rnnr: selected session changed; nothing was opened or closed.'; return 1; }
  if [[ $operation == attach ]]; then
    # Never use session open here: its recovery paths may create a new owner.
    _rnnr_guard || return 1
    command tmux attach-session -t "$_rnnr_id:"
    return $?
  fi
  # Keep t's ordinary confirmation and lifecycle. Recheck after confirmation,
  # at the lifecycle boundary and again before the stable-ID stop effect.
  (( ${+functions[_t_lifecycle_effect]} && ${+functions[_dev_kill_one]} )) || return 1
  functions[_rnnr_original_effect]=${functions[_t_lifecycle_effect]}
  _t_lifecycle_effect() {
    if [[ $1 == (close-check|stop) ]]; then
      [[ $2 == "$_rnnr_name" ]] && _rnnr_guard || return 1
      [[ $1 != stop || $4 == "$_rnnr_id" ]] || return 1
    fi
    _rnnr_original_effect "$@"
  }
  # A vanished local owner must never delegate to a different host.
  local -A REMOTE_HOSTS=()
  local T_REMOTE_ACTION_LOCAL=1
  local T_NO_AUTORELOAD=1
  {
    # Exact endpoint of t session close REPO SLOT. Calling it in-process keeps
    # ownership guards across the normal confirmation; the public command's
    # Python delegation would start a new shell and discard these guards.
    _dev_kill_one "$_rnnr_name"
  } always {
    functions[_t_lifecycle_effect]=${functions[_rnnr_original_effect]}
    unfunction _rnnr_original_effect
  }
}

_rnnr_main() {
  local source_root=$1; shift
  [[ $source_root == /* && -r $source_root/t.plugin.zsh && -x $source_root/bin/t ]] || {
    print -u2 'rnnr: configured t source is unavailable.'; return 1
  }
  # Source selection is private to this shell. The configured binary must be
  # first so t.plugin.zsh's explicit-source resolver cannot pick another install.
  export PATH="$source_root/bin:$PATH:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  export T_DEV_CONTEXT="$source_root" T_DEV_CONTEXT_OWNER=$$
  source "$source_root/t.plugin.zsh" || return 1
  [[ ${T_HOME:A} == ${source_root:A} ]] || {
    print -u2 'rnnr: configured t source could not be loaded.'; return 1
  }
  # App-wide actions are independent of whichever checkout launched the app.
  builtin cd / || return 1
  unset TMUX TMUX_PANE
  case "$1" in
    attach|end)
      (( $# == 5 )) || return 1
      local operation=$1 host=$2 slot=$3 cwd=$4 sid=$5 worker target
      if [[ $host == local ]]; then
        _rnnr_target "$operation" "$slot" "$cwd" "$sid"
      else
        # Only a loaded, registered alias can provide the SSH destination.
        target=${REMOTE_HOSTS[$host]:-}
        [[ -n $host && $host != *[[:cntrl:]]* && -n $target && $target != -* && $target != *[[:cntrl:]]* ]] || return 1
        worker='emulate -L zsh; setopt no_aliases; '
        worker+='[[ -n ${T_HOME:-} && -r "$T_HOME/t.plugin.zsh" ]] || { print -u2 "rnnr: t shell integration is unavailable on this host."; exit 1; }; '
        worker+='source "$T_HOME/t.plugin.zsh" || exit 1; '
        worker+="_rnnr_guard() { ${functions[_rnnr_guard]} }; "
        worker+="_rnnr_target() { ${functions[_rnnr_target]} }; "
        worker+="_rnnr_target ${(q)operation} ${(q)slot} ${(q)cwd} ${(q)sid}"
        command ssh -t -- "$target" "/bin/zsh -lic ${(q)worker}"
      fi ;;
    session)
      case "$*" in
        'session open --new')
          # The public --new path requires a repo outside a checkout. Reuse its
          # existing New picker, exactly as Ctrl-N in Open does.
          (( ${+functions[_t_open_new_local]} )) || return 1
          _t_open_new_local '' ;;
        'session list --all --remote'|'session open'|'session resume --all'|'session move --all --remote')
          t "$@" ;;
        *) return 1 ;;
      esac ;;
    *) return 1 ;;
  esac
}

_rnnr_main "$@"
