#!/bin/sh
# Shared visual defaults for every dotfiles fzf picker. Call-specific arguments
# come last, so selection behavior, previews, and requested heights still win.
_t_fzf() {
  command fzf --height=60% --layout=reverse --border=rounded --margin=1,2 \
    --pointer='▸' --marker='✓' \
    --color='fg+:black,bg+:cyan,hl:cyan,hl+:black:underline,pointer:black,marker:green,header:cyan,prompt:cyan,border:bright-black' \
    "$@"
}

# A separate header line keeps batch controls visible on narrow terminals.
_t_fzf_sessions() (
  _t_session_legend=$1
  shift
  _t_fzf --multi --marker='✓' --bind 'space:toggle+down' \
    --header="MULTI-SELECT · Space/Tab mark ✓
Enter opens: here + extra tabs
${_t_session_legend}" "$@"
)
