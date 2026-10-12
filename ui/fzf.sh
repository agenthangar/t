#!/bin/sh
# Shared visual defaults for every t picker. Callers choose a profile and may
# customize content (columns, prompt, preview), never controls or visual chrome.
_t_fzf() {
  command fzf --height=60% --layout=reverse --border=rounded --margin=1,2 \
    --pointer='▸' --marker='✓' \
    --color='fg+:black,bg+:cyan,hl:cyan,hl+:black:underline,pointer:black,marker:green,header:cyan,prompt:cyan,border:bright-black' \
    "$@"
  _t_fzf_rc=$?
  # fzf uses 1 for an empty selection and 130 for Esc/Ctrl-C. Both are an
  # abandoned interaction to the command that opened the picker.
  [ "$_t_fzf_rc" -ne 1 ] || return 130
  return "$_t_fzf_rc"
}

# Reject options that could silently replace the contract. Content options are
# intentionally open so a command can keep its own searchable fields and preview.
_t_fzf_content_options() {
  for _t_fzf_option do
    case $_t_fzf_option in
      --prompt=*|--delimiter=*|--with-nth=*|--nth=*|--no-hscroll|--preview=*|\
      --preview-window=*|--query=*|--exact|--literal|--case-sensitive|\
      --ignore-case|--ansi) ;;
      *)
        printf 't picker: unsupported content option: %s\n' "$_t_fzf_option" >&2
        return 2 ;;
    esac
  done
}

_t_fzf_single() (
  _t_fzf_action=$1 _t_fzf_legend=$2
  shift 2
  _t_fzf_content_options "$@" || exit $?
  _t_fzf_header="Enter $_t_fzf_action · Esc cancel"
  [ -z "$_t_fzf_legend" ] || _t_fzf_header="$_t_fzf_header
$_t_fzf_legend"
  _t_fzf --no-multi --bind='enter:accept,esc:abort,ctrl-c:abort,tab:down,shift-tab:up' \
    --header="$_t_fzf_header" "$@"
)

# A separate header line keeps batch controls visible on narrow terminals.
_t_fzf_multi() (
  _t_fzf_action=$1 _t_fzf_legend=$2
  shift 2
  _t_fzf_content_options "$@" || exit $?
  _t_fzf --multi --bind='enter:accept,esc:abort,ctrl-c:abort,space:toggle+down,tab:toggle+down,shift-tab:up' \
    --header="Space/Tab mark ✓ · Enter $_t_fzf_action · Esc cancel
${_t_fzf_legend}" "$@"
)

# Resume-style session picker with a reversible default filter. Keep both views
# in private files so Ctrl-A can reload inside fzf without dropping the query.
# Arguments: default rows, all rows, action, filter label, legend, show-all, fzf args.
# Session callers opt into independent controls with T_PICKER_SESSION_FILTERS.
# Kill also supplies the two single-filter views via T_PICKER_WITH_ATTACHED and
# T_PICKER_WITH_PENDING. All callers use Ctrl-A for all and Ctrl-F for defaults.
_t_fzf_filtered() (
  _t_picker_default=$1 _t_picker_all=$2 _t_picker_action=$3
  _t_picker_label=$4 _t_picker_legend=$5 _t_picker_show_all=$6
  _t_picker_with_attached=${T_PICKER_WITH_ATTACHED:-}
  _t_picker_with_pending=${T_PICKER_WITH_PENDING:-}
  unset T_PICKER_WITH_ATTACHED T_PICKER_WITH_PENDING
  shift 6
  _t_fzf_content_options "$@" || exit $?
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
    "$_t_picker_label · Ctrl-A show all · Ctrl-F defaults · $_t_picker_hidden hidden" "$_t_picker_legend" > "$T_PICKER_HEADER_DEFAULT"
  printf '%s\n%s\n%s' "$_t_picker_controls" \
    "${T_PICKER_ALL_LABEL:-All items} · Ctrl-A show all · Ctrl-F defaults" "$_t_picker_legend" > "$T_PICKER_HEADER_ALL"
  export T_PICKER_VIEWS="$_t_picker_dir" T_PICKER_SESSION_MASK=
  case ${T_PICKER_SESSION_FILTERS:-} in
    open|resume) T_PICKER_SESSION_MASK=1 ;;
    kill) T_PICKER_SESSION_MASK=3 ;;
  esac
  if [ -n "$T_PICKER_SESSION_MASK" ]; then
    cp "$T_PICKER_DEFAULT" "$_t_picker_dir/rows-0"
    cp "$T_PICKER_ALL" "$_t_picker_dir/rows-$T_PICKER_SESSION_MASK"
    if [ "$T_PICKER_SESSION_MASK" = 3 ]; then
      : > "$_t_picker_dir/rows-1"
      : > "$_t_picker_dir/rows-2"
      [ -z "$_t_picker_with_attached" ] || printf '%s\n' "$_t_picker_with_attached" > "$_t_picker_dir/rows-1"
      [ -z "$_t_picker_with_pending" ] || printf '%s\n' "$_t_picker_with_pending" > "$_t_picker_dir/rows-2"
    fi
    for _t_picker_mask in 0 1 2 3; do
      [ "$_t_picker_mask" -le "$T_PICKER_SESSION_MASK" ] || continue
      _t_picker_first=hidden _t_picker_second=hidden
      [ $(( _t_picker_mask & 1 )) = 0 ] || _t_picker_first=shown
      [ $(( _t_picker_mask & 2 )) = 0 ] || _t_picker_second=shown
      case $T_PICKER_SESSION_FILTERS in
        resume) _t_picker_filters="Ctrl-L live: $_t_picker_first" ;;
        open) _t_picker_filters="Ctrl-T attached: $_t_picker_first" ;;
        kill) _t_picker_filters="Ctrl-T attached: $_t_picker_first · Ctrl-P pending: $_t_picker_second" ;;
      esac
      _t_picker_hidden=$(( $(wc -l < "$T_PICKER_ALL") - $(wc -l < "$_t_picker_dir/rows-$_t_picker_mask") ))
      printf '%s\n%s\n%s\n%s' "$_t_picker_controls" \
        "Ctrl-A show all · Ctrl-F defaults · $_t_picker_hidden hidden" \
        "$_t_picker_filters" "$_t_picker_legend" > "$_t_picker_dir/header-$_t_picker_mask"
    done
    cp "$_t_picker_dir/header-0" "$T_PICKER_HEADER_DEFAULT"
    cp "$_t_picker_dir/header-$T_PICKER_SESSION_MASK" "$T_PICKER_HEADER_ALL"
  fi
  # New fzf versions retain marks for identities still visible after reload.
  # Older versions clear marks explicitly so a hidden target cannot be accepted.
  export T_PICKER_CLEAR_MARKS='deselect-all+'
  case "$(command fzf --help </dev/null 2>/dev/null)" in
    *--id-nth*) set -- --id-nth=1,2,3 "$@"; T_PICKER_CLEAR_MARKS= ;;
  esac
  cat > "$T_PICKER_TOGGLE" <<'SH'
if [ -n "$T_PICKER_SESSION_MASK" ]; then
  current=0
  [ ! -s "$T_PICKER_STATE" ] || read -r current < "$T_PICKER_STATE"
  case ${1:-toggle} in
    all) next=$T_PICKER_SESSION_MASK ;;
    default) next=0 ;;
    first) next=$(( current ^ 1 )) ;;
    second) next=$(( current ^ 2 )) ;;
    toggle) if [ "$current" = 0 ]; then next=$T_PICKER_SESSION_MASK; else next=0; fi ;;
    *) exit 1 ;;
  esac
  [ "$next" -le "$T_PICKER_SESSION_MASK" ] || exit 1
  if [ "$next" = 0 ]; then rm -f -- "$T_PICKER_STATE"
  else printf '%s\n' "$next" > "$T_PICKER_STATE"; fi
  cp "$T_PICKER_VIEWS/rows-$next" "$T_PICKER_CURRENT"
  cp "$T_PICKER_VIEWS/header-$next" "$T_PICKER_CURRENT_HEADER"
  exit
fi
case ${1:-all} in
  default) rm -f -- "$T_PICKER_STATE"; cp "$T_PICKER_DEFAULT" "$T_PICKER_CURRENT"; cp "$T_PICKER_HEADER_DEFAULT" "$T_PICKER_CURRENT_HEADER" ;;
  all) : > "$T_PICKER_STATE"; cp "$T_PICKER_ALL" "$T_PICKER_CURRENT"; cp "$T_PICKER_HEADER_ALL" "$T_PICKER_CURRENT_HEADER" ;;
  toggle) if [ -f "$T_PICKER_STATE" ]; then sh "$T_PICKER_TOGGLE" default; else sh "$T_PICKER_TOGGLE" all; fi ;;
  *) exit 1 ;;
esac
SH
  _t_picker_input=$T_PICKER_DEFAULT _t_picker_header=$T_PICKER_HEADER_DEFAULT
  if [ -n "$_t_picker_show_all" ]; then
    printf '%s\n' "$T_PICKER_SESSION_MASK" > "$T_PICKER_STATE"
    _t_picker_input=$T_PICKER_ALL _t_picker_header=$T_PICKER_HEADER_ALL
  fi
  cp "$_t_picker_input" "$T_PICKER_CURRENT"
  cp "$_t_picker_header" "$T_PICKER_CURRENT_HEADER"
  # Generic transform was added after fzf 0.44. The three actions below are
  # supported by Ubuntu 24.04's fzf: update files, reload rows, then read the
  # matching header. execute-silent finishes before the following actions run.
  # Expanded by fzf's action shell, not by this shell.
  # shellcheck disable=SC2016
  _t_picker_bind="ctrl-a:${T_PICKER_CLEAR_MARKS}"'execute-silent(sh "$T_PICKER_TOGGLE" all)+reload-sync(cat "$T_PICKER_CURRENT")+transform-header(cat "$T_PICKER_CURRENT_HEADER"),ctrl-f:'"${T_PICKER_CLEAR_MARKS}"'execute-silent(sh "$T_PICKER_TOGGLE" default)+reload-sync(cat "$T_PICKER_CURRENT")+transform-header(cat "$T_PICKER_CURRENT_HEADER")'
  if [ -n "$T_PICKER_SESSION_MASK" ]; then
    _t_picker_bind=
    _t_picker_second_key=
    case $T_PICKER_SESSION_FILTERS in
      resume) _t_picker_first_key=ctrl-l:first ;;
      open) _t_picker_first_key=ctrl-t:first ;;
      kill) _t_picker_first_key=ctrl-t:first; _t_picker_second_key=ctrl-p:second ;;
    esac
    for _t_picker_spec in ctrl-a:all ctrl-f:default "$_t_picker_first_key" "$_t_picker_second_key"; do
      [ -n "$_t_picker_spec" ] || continue
      _t_picker_key=${_t_picker_spec%:*} _t_picker_action=${_t_picker_spec#*:}
      _t_picker_bind="${_t_picker_bind}${_t_picker_bind:+,}${_t_picker_key}:${T_PICKER_CLEAR_MARKS}execute-silent(sh \"\$T_PICKER_TOGGLE\" $_t_picker_action)+reload-sync(cat \"\$T_PICKER_CURRENT\")+transform-header(cat \"\$T_PICKER_CURRENT_HEADER\")"
    done
  fi
  _t_fzf --multi --no-hscroll --delimiter="$(printf '\t')" --with-nth=-1 \
    --bind='enter:accept,esc:abort,ctrl-c:abort,space:toggle+down,tab:toggle+down,shift-tab:up,ctrl-/:toggle-preview' \
    --bind="$_t_picker_bind" \
    --preview='printf "%s\n" {-1}' --preview-window='hidden:wrap:down:40%' \
    --header="$(cat "$_t_picker_header")" "$@" < "$T_PICKER_CURRENT"
)
