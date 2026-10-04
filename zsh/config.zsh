# Per-machine configuration is shell code, loaded after the public arrays exist.
typeset -gA DEV_REPOS DEV_BRANCHES REMOTE_HOSTS DEV_WORKTREE DEV_OPEN_MODE DEV_AGENT DEV_MODEL DEV_EFFORT DEV_FAST
typeset -ga _T_OWNED_ALIASES _T_OWNED_HOSTS
typeset -gA _T_OWNED_ALIAS_VALUES _T_OWNED_HOST_VALUES

# Remove only shortcuts that t created on an earlier source.
local _t_name
for _t_name in $_T_OWNED_ALIASES; do
  [[ ${aliases[$_t_name]-} == ${_T_OWNED_ALIAS_VALUES[$_t_name]-} ]] && unalias "$_t_name" 2>/dev/null
done
for _t_name in $_T_OWNED_HOSTS; do
  [[ ${functions[$_t_name]-} == ${_T_OWNED_HOST_VALUES[$_t_name]-} ]] && unfunction "$_t_name" 2>/dev/null
done
_T_OWNED_ALIASES=() _T_OWNED_HOSTS=() _T_OWNED_ALIAS_VALUES=() _T_OWNED_HOST_VALUES=()
DEV_REPOS=() DEV_BRANCHES=() REMOTE_HOSTS=() DEV_WORKTREE=() DEV_OPEN_MODE=() DEV_AGENT=() DEV_MODEL=() DEV_EFFORT=() DEV_FAST=()
unset DEV_OPEN_MODE_DEFAULT DEV_BRANCH DEV_WORKTREE_ROOT DEV_WORKTREE_DEFAULT DEV_AGENT_DEFAULT TBEAM_HOST MINI_HOST

export T_LOCAL_RC=${T_LOCAL_RC:-${XDG_CONFIG_HOME:-$HOME/.config}/t/local.zsh}
[[ -f $T_LOCAL_RC ]] && source "$T_LOCAL_RC"

: ${DEV_BRANCH:=dev/claude-1}
: ${DEV_WORKTREE_ROOT:=$HOME/code/.worktrees}
: ${DEV_WORKTREE_DEFAULT:=1}
: ${DEV_AGENT_DEFAULT:=claude}
: ${DEV_OPEN_MODE_DEFAULT:=cli}
_dev_branch_for() { print -r -- "${DEV_BRANCHES[$1]:-$DEV_BRANCH}" }

_t_shortcut_available() {
  local key=$1
  [[ $key =~ '^[A-Za-z_][A-Za-z0-9_-]*$' && $key != t ]] || return 1
  [[ -z ${aliases[$key]-} && -z ${functions[$key]-} && -z ${commands[$key]-} ]] || return 1
}
local _t_value
for _t_name in ${(ok)DEV_REPOS}; do
  _t_shortcut_available "$_t_name" || continue
  _t_value="cd ${(q)DEV_REPOS[$_t_name]}"
  alias "$_t_name=$_t_value"
  _T_OWNED_ALIASES+=("$_t_name")
  _T_OWNED_ALIAS_VALUES[$_t_name]=$_t_value
done
(( ${#REMOTE_HOSTS} == 0 )) && [[ -n ${MINI_HOST:-${TBEAM_HOST:-}} ]] \
  && REMOTE_HOSTS[mini]="${MINI_HOST:-$TBEAM_HOST}"
for _t_name in ${(ok)REMOTE_HOSTS}; do
  _t_shortcut_available "$_t_name" || continue
  _t_value="t on ${(q)_t_name} \"\$@\""
  functions[$_t_name]=$_t_value
  _T_OWNED_HOSTS+=("$_t_name")
  _T_OWNED_HOST_VALUES[$_t_name]=${functions[$_t_name]}
done
unset _t_name _t_value

# Python parses this as data, never shell code. Rewrite on each source so a
# symlink switch between main and a worktree refreshes the identity.
_t_sync_config() {
  local cache="${XDG_CONFIG_HOME:-$HOME/.config}/t/config.sh" k tmp
  mkdir -p "${cache:h}" 2>/dev/null || return
  tmp="$cache.$$.$RANDOM.tmp"
  {
    for k in ${(k)DEV_REPOS};    do print -r -- "DEV_REPOS[$k]=${(q)DEV_REPOS[$k]}"; done
    for k in ${(k)DEV_BRANCHES}; do print -r -- "DEV_BRANCHES[$k]=${(q)DEV_BRANCHES[$k]}"; done
    for k in ${(k)REMOTE_HOSTS}; do print -r -- "REMOTE_HOSTS[$k]=${(q)REMOTE_HOSTS[$k]}"; done
    for k in ${(k)DEV_WORKTREE};  do print -r -- "DEV_WORKTREE[$k]=${(q)DEV_WORKTREE[$k]}"; done
    for k in ${(k)DEV_AGENT};     do print -r -- "DEV_AGENT[$k]=${(q)DEV_AGENT[$k]}"; done
    for k in ${(k)DEV_MODEL};     do print -r -- "DEV_MODEL[$k]=${(q)DEV_MODEL[$k]}"; done
    for k in ${(k)DEV_EFFORT};    do print -r -- "DEV_EFFORT[$k]=${(q)DEV_EFFORT[$k]}"; done
    for k in ${(k)DEV_FAST};      do print -r -- "DEV_FAST[$k]=${(q)DEV_FAST[$k]}"; done
    for k in ${(k)DEV_OPEN_MODE}; do print -r -- "DEV_OPEN_MODE[$k]=${(q)DEV_OPEN_MODE[$k]}"; done
    print -r -- "DEV_OPEN_MODE_DEFAULT=${(q)DEV_OPEN_MODE_DEFAULT}"
    print -r -- "DEV_AGENT_DEFAULT=${(q)DEV_AGENT_DEFAULT}"
    print -r -- "TBEAM_HOST=${(q)TBEAM_HOST}"
    print -r -- "MINI_HOST=${(q)MINI_HOST}"
    print -r -- "DEV_BRANCH=${(q)DEV_BRANCH}"
    print -r -- "DEV_WORKTREE_ROOT=${(q)DEV_WORKTREE_ROOT}"
    print -r -- "DEV_WORKTREE_DEFAULT=${(q)DEV_WORKTREE_DEFAULT}"
    print -r -- "T_LOCAL_RC=${(q)T_LOCAL_RC}"
  } >| "$tmp" || { rm -f -- "$tmp"; return 1; }
  command mv -f -- "$tmp" "$cache" || { rm -f -- "$tmp"; return 1; }
}
_t_sync_config
