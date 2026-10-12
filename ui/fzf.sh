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

# Resume-style session picker with a reversible default filter. Keep both views
# in private files so Ctrl-A can reload inside fzf without dropping the query.
# Arguments: default rows, all rows, action, filter label, legend, show-all, fzf args.
_t_fzf_filtered() (
  _t_picker_default=$1 _t_picker_all=$2 _t_picker_action=$3
  _t_picker_label=$4 _t_picker_legend=$5 _t_picker_show_all=$6
  shift 6
  umask 077
  _t_picker_dir=$(mktemp -d "${TMPDIR:-/tmp}/t-picker.XXXXXX") || exit 1
  trap 'rm -rf -- "$_t_picker_dir"' EXIT
  trap 'exit 130' HUP INT TERM
  export T_PICKER_DEFAULT="$_t_picker_dir/default" T_PICKER_ALL="$_t_picker_dir/all"
  export T_PICKER_STATE="$_t_picker_dir/show-all" T_PICKER_TOGGLE="$_t_picker_dir/toggle"
  export T_PICKER_CURRENT="$_t_picker_dir/current" T_PICKER_CURRENT_HEADER="$_t_picker_dir/current-header"
  export T_PICKER_HEADER_DEFAULT="$_t_picker_dir/header-default" T_PICKER_HEADER_ALL="$_t_picker_dir/header-all"
  : > "$T_PICKER_DEFAULT"
  : > "$T_PICKER_ALL"
  [ -z "$_t_picker_default" ] || printf '%s\n' "$_t_picker_default" > "$T_PICKER_DEFAULT"
  [ -z "$_t_picker_all" ] || printf '%s\n' "$_t_picker_all" > "$T_PICKER_ALL"
  _t_picker_hidden=$(( $(wc -l < "$T_PICKER_ALL") - $(wc -l < "$T_PICKER_DEFAULT") ))
  _t_picker_controls="Space/Tab select · Enter $_t_picker_action · Ctrl-/ details · Esc cancel"
  printf '%s\n%s\n%s' "$_t_picker_controls" \
    "$_t_picker_label · Ctrl-A show all · $_t_picker_hidden hidden" "$_t_picker_legend" > "$T_PICKER_HEADER_DEFAULT"
  printf '%s\n%s\n%s' "$_t_picker_controls" \
    "${T_PICKER_ALL_LABEL:-All sessions} · Ctrl-A restore filter" "$_t_picker_legend" > "$T_PICKER_HEADER_ALL"
  # New fzf versions retain marks for identities still visible after reload.
  # Older versions clear marks explicitly so a hidden target cannot be accepted.
  export T_PICKER_CLEAR_MARKS='deselect-all+'
  case "$(command fzf --help </dev/null 2>/dev/null)" in
    *--id-nth*) set -- --id-nth=1,2,3 "$@"; T_PICKER_CLEAR_MARKS= ;;
  esac
  cat > "$T_PICKER_TOGGLE" <<'SH'
if [ -f "$T_PICKER_STATE" ]; then
  rm -f -- "$T_PICKER_STATE"
  cp "$T_PICKER_DEFAULT" "$T_PICKER_CURRENT"
  cp "$T_PICKER_HEADER_DEFAULT" "$T_PICKER_CURRENT_HEADER"
else
  : > "$T_PICKER_STATE"
  cp "$T_PICKER_ALL" "$T_PICKER_CURRENT"
  cp "$T_PICKER_HEADER_ALL" "$T_PICKER_CURRENT_HEADER"
fi
SH
  _t_picker_input=$T_PICKER_DEFAULT _t_picker_header=$T_PICKER_HEADER_DEFAULT
  if [ -n "$_t_picker_show_all" ]; then
    : > "$T_PICKER_STATE"
    _t_picker_input=$T_PICKER_ALL _t_picker_header=$T_PICKER_HEADER_ALL
  fi
  cp "$_t_picker_input" "$T_PICKER_CURRENT"
  cp "$_t_picker_header" "$T_PICKER_CURRENT_HEADER"
  # Generic transform was added after fzf 0.44. The three actions below are
  # supported by Ubuntu 24.04's fzf: update files, reload rows, then read the
  # matching header. execute-silent finishes before the following actions run.
  # Expanded by fzf's action shell, not by this shell.
  # shellcheck disable=SC2016
  _t_picker_bind="ctrl-a:${T_PICKER_CLEAR_MARKS}"'execute-silent(sh "$T_PICKER_TOGGLE")+reload-sync(cat "$T_PICKER_CURRENT")+transform-header(cat "$T_PICKER_CURRENT_HEADER")'
  _t_fzf --multi --no-hscroll --delimiter="$(printf '\t')" --with-nth=-1 \
    --bind='space:toggle+down,ctrl-/:toggle-preview' \
    --bind="$_t_picker_bind" \
    --preview='printf "%s\n" {-1}' --preview-window='hidden:wrap:down:40%' \
    --header="$(cat "$_t_picker_header")" "$@" < "$T_PICKER_CURRENT"
)
