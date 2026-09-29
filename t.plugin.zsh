# Source this file from ~/.zshrc after putting the installed t binary on PATH.
# Its path is resolved afresh on every source, including after t update switches
# the installed links between the canonical checkout and a development worktree.
_t_load_plugin() {
  local entry="${${(%):-%x}:A}" bin resolved common
  # %x in a function is its definition file; resolve that file, not a cached root.
  entry="${${(%):-%x}:A}"
  typeset -g T_HOME="${entry:h}"
  export T_HOME T_LOCAL_RC
  # Direct sourcing works in a clean shell; an explicitly earlier t on PATH wins
  # (useful for shell test stubs and custom installs).
  [[ -n $(whence -p t 2>/dev/null) ]] || export PATH="$PATH:$T_HOME/bin"

  if [[ ! -w /tmp ]]; then
    export TMPDIR="${TMPDIR:-$HOME/.cache/tmp}"
    mkdir -p "$TMPDIR" 2>/dev/null
    TMPPREFIX="$TMPDIR/zsh"
    export TMUX_TMPDIR="${TMUX_TMPDIR:-$TMPDIR}"
  fi
  mkdir -p "${XDG_CACHE_HOME:-$HOME/.cache}/t" "${XDG_STATE_HOME:-$HOME/.local/state}/t" 2>/dev/null
  zmodload zsh/datetime 2>/dev/null
  zmodload -F zsh/stat b:zstat 2>/dev/null
  autoload -Uz add-zsh-hook
  _t_fzf() { command fzf "$@"; }
  [[ -r "$T_HOME/ui/fzf.sh" ]] && source "$T_HOME/ui/fzf.sh"

  # The cached local data must be rebuilt before the Python executable runs.
  source "$T_HOME/zsh/config.zsh"
  source "$T_HOME/zsh/agent.zsh"
  source "$T_HOME/zsh/worktree.zsh"
  source "$T_HOME/zsh/sessions.zsh"
  source "$T_HOME/zsh/remote.zsh"
  source "$T_HOME/zsh/resume.zsh"
  source "$T_HOME/zsh/shim.zsh"

  add-zsh-hook -d precmd _dev_worktree_sweep 2>/dev/null
  add-zsh-hook -d precmd _term_reset_mouse 2>/dev/null
  add-zsh-hook -d precmd _t_reload_if_moved 2>/dev/null
  add-zsh-hook precmd _dev_worktree_sweep
  add-zsh-hook precmd _term_reset_mouse
  add-zsh-hook precmd _t_reload_if_moved
  [[ -n ${functions[compdef]-} ]] && compdef _t t

  typeset -g _T_LOADED_PLUGIN="$entry"
  typeset -g _T_LOADED_HEAD="$(git -C "$T_HOME" rev-parse HEAD 2>/dev/null)"
}

# Protect both selected and canonical t code plus an explicit dotfiles live tree.
_t_tree_is_live() {
  local tree=$1 common
  [[ -n $tree && -d $tree ]] || return 1
  [[ ${tree:A} == ${T_HOME:A} ]] && return 0
  [[ -n ${T_DOTFILES_LIVE_TREE:-} && ${tree:A} == ${T_DOTFILES_LIVE_TREE:A} ]] && return 0
  common=$(git -C "$T_HOME" rev-parse --git-common-dir 2>/dev/null)
  [[ -n $common ]] || return 1
  [[ $common == /* ]] || common="$T_HOME/$common"
  [[ ${tree:A} == ${common:A:h} ]]
}

_t_reload() {
  local bin root plugin
  bin=$(whence -p t 2>/dev/null)
  [[ -n $bin ]] && root="${bin:A:h:h}"
  plugin="$root/t.plugin.zsh"
  [[ -r $plugin ]] || plugin="$T_HOME/t.plugin.zsh"
  [[ -r $plugin ]] || { print -u2 -r -- "t: shell integration is missing; source the installed t.plugin.zsh from ~/.zshrc"; return 1; }
  source "$plugin"
}

_t_reload_if_moved() {
  [[ -n ${T_NO_AUTORELOAD:-} ]] && return 0
  local bin root plugin head
  bin=$(whence -p t 2>/dev/null)
  [[ -n $bin ]] && root="${bin:A:h:h}"
  plugin="$root/t.plugin.zsh"
  [[ -r $plugin ]] || plugin="$T_HOME/t.plugin.zsh"
  head=$(git -C "${plugin:h}" rev-parse HEAD 2>/dev/null)
  [[ $plugin == $_T_LOADED_PLUGIN && $head == $_T_LOADED_HEAD ]] && return 0
  _t_reload
}

_stat_birth() {
  local b
  if [[ $OSTYPE == darwin* ]]; then b=$(command stat -f %B "$1" 2>/dev/null)
  else b=$(command stat -c %W "$1" 2>/dev/null); fi
  [[ -n $b && $b != 0 && $b != - ]] || return 1
  print -r -- "$b"
}

_t_load_plugin
unfunction _t_load_plugin
