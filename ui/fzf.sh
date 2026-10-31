#!/bin/sh
# Shared visual defaults for every t picker. Callers choose a profile and may
# customize content (columns, prompt, preview), never controls or visual chrome.
# Interactive zsh's stopped-job handling can spin inside command substitutions.
# Replace each profile's subshell with a noninteractive sh before waiting for fzf.
# Resolve this source file here so direct sourcing also works without T_HOME.
[ "${1:-}" != --t-fzf-worker ] || unset ZSH_VERSION
if [ -n "${ZSH_VERSION:-}" ]; then
  eval '_T_FZF_SOURCE=${${(%):-%x}:A}'
fi
# Run only inside a zsh profile subshell. Send shell-local inputs and arguments
# through a private pipe: row snapshots can exceed exec's argv/environment limit.
# (qq) emits POSIX single-quoted strings, including embedded quotes/newlines.
_t_fzf_worker() {
  # shellcheck disable=SC2016
  eval '
    emulate -L zsh
    _t_fzf_inputs=()
    for _t_fzf_name in COLUMNS LINES TMPDIR NO_COLOR T_HOME PATH \
      T_PICKER_COLUMNS T_PICKER_SOURCE_ROWS T_PICKER_SESSION_FILTERS \
      T_PICKER_WITH_ATTACHED T_PICKER_WITH_PENDING T_PICKER_ALL_LABEL T_PICKER_AUTH_HOSTS; do
      [[ -v $_t_fzf_name ]] && _t_fzf_inputs+=("$_t_fzf_name")
    done
    # Keep large prefix-assigned inputs out of the environment of exec itself.
    (( ${#_t_fzf_inputs} == 0 )) || typeset +x "${_t_fzf_inputs[@]}"
    exec /bin/sh "$_T_FZF_SOURCE" --t-fzf-worker 3< <(
      trap - EXIT
      zshexit_functions=()
      unfunction zshexit 2>/dev/null || :
      for _t_fzf_name in "${_t_fzf_inputs[@]}"; do
        print -r -- "export $_t_fzf_name=${(qq)${(P)_t_fzf_name}}"
      done
      print -rn -- "set --"
      for _t_fzf_arg do
        print -rn -- " ${(qq)_t_fzf_arg}"
      done
      print
    )
  '
}

_t_fzf() {
  if [ -n "${NO_COLOR+x}" ] || [ "${TERM:-}" = dumb ]; then
    set -- "$@" --color=bw
  fi
  command fzf --height=60% --layout=reverse --border=rounded --margin=1,2 \
    --pointer='▸' --marker='✓ ' \
    --color='fg+:white,bg+:#005f5f,hl:cyan,hl+:white:underline,pointer:white,marker:#87ffaf:bold,header:cyan,prompt:cyan,border:bright-black' \
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
  [ -z "${ZSH_VERSION:-}" ] || _t_fzf_worker _t_fzf_single "$@"
  _t_fzf_action=$1 _t_fzf_legend=$2
  shift 2
  _t_fzf_content_options "$@" || return $?
  _t_fzf_size=$(stty size 2>/dev/null </dev/tty) || _t_fzf_size=
  if [ -n "$_t_fzf_size" ] && [ "${_t_fzf_size%% *}" -gt 0 ] && [ "${_t_fzf_size%% *}" -le 14 ]; then
    set -- --height=100% --margin=0 --border=none "$@"
  fi
  _t_fzf_header="Enter $_t_fzf_action · Esc"
  [ -z "$_t_fzf_legend" ] || _t_fzf_header="$_t_fzf_header
$_t_fzf_legend"
  _t_fzf --no-multi --bind='enter:accept,esc:abort,ctrl-c:abort,tab:down,shift-tab:up' \
    --header="$_t_fzf_header" "$@"
)

# A separate header line keeps batch controls visible on narrow terminals.
_t_fzf_multi() (
  [ -z "${ZSH_VERSION:-}" ] || _t_fzf_worker _t_fzf_multi "$@"
  _t_fzf_action=$1 _t_fzf_legend=$2
  shift 2
  _t_fzf_content_options "$@" || return $?
  _t_fzf --multi --bind='enter:accept,esc:abort,ctrl-c:abort,space:toggle+down,tab:toggle+down,shift-tab:up' \
    --header="Enter $_t_fzf_action · Esc · Space/Tab mark ✓
${_t_fzf_legend}" "$@"
)

# A paginated single choice filters the whole data set through a caller-supplied
# helper. Ctrl-F/B page the results; unlike a filtered picker, there is no
# default/all view for Ctrl-F to restore. The caller owns data and page labels,
# while this profile owns keys, cardinality, theme, and cancellation.
# Arguments: page label file, rows file, quoted helper command, preview command,
# initial query, prompt. The helper writes rows and the page label on each event.
_t_fzf_paginated() (
  [ -z "${ZSH_VERSION:-}" ] || _t_fzf_worker _t_fzf_paginated "$@"
  if [ "$#" -lt 6 ] || [ "$#" -gt 7 ]; then
    printf 't picker: paged profile needs six arguments and optional multi mode\n' >&2
    return 2
  fi
  _t_paged_page=$1 _t_paged_rows=$2 _t_paged_helper=$3
  _t_paged_preview=$4 _t_paged_query=$5 _t_paged_prompt=$6
  _t_paged_size=$(stty size </dev/tty 2>/dev/null) || _t_paged_size=
  if [ -n "$_t_paged_size" ] && [ "${_t_paged_size#* }" -le 44 ]; then
    T_PICKER_PAGED_CONTROLS='Enter resume · Esc
Ctrl-F/B pg · Ctrl-R
Ctrl-/ details'
  else
    T_PICKER_PAGED_CONTROLS='Enter resume · Esc · Ctrl-F/B pages · Ctrl-R refresh · Ctrl-/ details'
  fi
  _t_paged_cardinality=--no-multi
  _t_paged_keys='tab:down,shift-tab:up'
  if [ "${7:-}" = multi ]; then
    _t_paged_cardinality=--multi
    _t_paged_keys='space:toggle,tab:toggle+down,shift-tab:toggle+up'
    T_PICKER_PAGED_CONTROLS="$T_PICKER_PAGED_CONTROLS
Space/Tab mark · page/search clears marks"
  fi
  _t_paged_preview_window='hidden:wrap:down:40%:~1'
  _t_paged_margin='1,2' _t_paged_info=default
  if [ -n "$_t_paged_size" ] && [ "${_t_paged_size%% *}" -le 14 ]; then
    _t_paged_margin='0,2' _t_paged_info=inline
    _t_paged_preview_window='hidden:wrap:down:3:border-none:~1'
  fi
  if [ -n "$_t_paged_size" ] && [ "${_t_paged_size%% *}" -ge 22 ] && [ "${_t_paged_size#* }" -ge 70 ]; then
    _t_paged_preview_window='wrap:down:35%:border-top:~1'
  fi
  T_PICKER_PAGED_PAGE=$_t_paged_page T_PICKER_PAGED_ROWS=$_t_paged_rows
  export T_PICKER_PAGED_CONTROLS T_PICKER_PAGED_PAGE T_PICKER_PAGED_ROWS
  umask 077
  _t_paged_dir=$(mktemp -d "${TMPDIR:-/tmp}/t-paged-picker.XXXXXX") || return 1
  trap 'rm -rf -- "$_t_paged_dir"' EXIT
  trap 'exit 130' HUP INT TERM
  T_PICKER_PAGED_RENDER=$_t_paged_dir/header
  export T_PICKER_PAGED_RENDER
  cat > "$T_PICKER_PAGED_RENDER" <<'SH'
printf '%s\n' "$T_PICKER_PAGED_CONTROLS"
cat "$T_PICKER_PAGED_PAGE"
SH
  # Expanded by fzf's action shell after the picker starts.
  # shellcheck disable=SC2016
  _t_paged_rows_command='cat "$T_PICKER_PAGED_ROWS"'
  # shellcheck disable=SC2016
  _t_paged_header_command='sh "$T_PICKER_PAGED_RENDER"'
  _t_paged_bind="change:deselect-all+execute-silent($_t_paged_helper show {q})+reload-sync($_t_paged_rows_command)+transform-header($_t_paged_header_command),ctrl-f:deselect-all+execute-silent($_t_paged_helper next {q})+reload-sync($_t_paged_rows_command)+transform-header($_t_paged_header_command),ctrl-b:deselect-all+execute-silent($_t_paged_helper previous {q})+reload-sync($_t_paged_rows_command)+transform-header($_t_paged_header_command)"
  _t_fzf --height=100% --margin="$_t_paged_margin" --info="$_t_paged_info" --disabled --sync "$_t_paged_cardinality" --delimiter="$(printf '\t')" --with-nth=2 --no-hscroll \
    --print-query --expect=enter,ctrl-r --query="$_t_paged_query" --prompt="$_t_paged_prompt" \
    --header="$(sh "$T_PICKER_PAGED_RENDER")" \
    --bind="esc:abort,ctrl-c:abort,$_t_paged_keys,ctrl-/:toggle-preview,shift-up:preview-up,shift-down:preview-down" \
    --bind="$_t_paged_bind" --preview="$_t_paged_preview" \
    --preview-window="$_t_paged_preview_window" < "$_t_paged_rows"
)

# Resume-style session picker with a reversible default filter. Keep both views
# in private files so Ctrl-A can reload inside fzf without dropping the query.
# Arguments: default rows, all rows, action, filter label, legend, show-all, fzf args.
# Session callers opt into independent controls with T_PICKER_SESSION_FILTERS.
# Kill also supplies the two single-filter views via T_PICKER_WITH_ATTACHED and
# T_PICKER_WITH_PENDING. Session snapshots use Ctrl-F settings, Ctrl-A off,
# Ctrl-L on. Generic filtered lists retain Ctrl-A all and Ctrl-F defaults.
_t_fzf_filtered() (
  [ -z "${ZSH_VERSION:-}" ] || _t_fzf_worker _t_fzf_filtered "$@"
  _t_picker_default=$1 _t_picker_all=$2 _t_picker_action=$3
  _t_picker_label=$4 _t_picker_legend=$5 _t_picker_show_all=$6
  _t_picker_with_attached=${T_PICKER_WITH_ATTACHED:-}
  _t_picker_with_pending=${T_PICKER_WITH_PENDING:-}
  _t_picker_columns=${T_PICKER_COLUMNS:-}
  _t_picker_source=${T_PICKER_SOURCE_ROWS:-}
  unset T_PICKER_SOURCE_ROWS
  _t_picker_margin=1,2
  unset T_PICKER_WITH_ATTACHED T_PICKER_WITH_PENDING
  shift 6
  _t_fzf_content_options "$@" || return $?
  # fzf 0.44 needs one fewer header row to leave a focused item visible at
  # 32×12. The agent legend is secondary to the action and filter controls.
  _t_picker_size=$(stty size 2>/dev/null) || _t_picker_size=
  if [ -n "$_t_picker_size" ] && [ "${_t_picker_size%% *}" -gt 0 ] && [ "${_t_picker_size%% *}" -le 12 ]; then
    _t_picker_legend=
    # Keep the column labels and a focused row visible on short terminals.
    [ -z "$_t_picker_columns" ] || _t_picker_margin=0,2
  fi
  umask 077
  _t_picker_dir=$(mktemp -d "${TMPDIR:-/tmp}/t-picker.XXXXXX") || return 1
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
  _t_picker_controls="Enter $_t_picker_action · Esc · Space/Tab mark ✓"
  _t_picker_preview_bind=,ctrl-/:toggle-preview
  if [ "${T_PICKER_SESSION_FILTERS:-}" = open ]; then
    _t_picker_preview_bind=,ctrl-/:ignore,alt-n:ignore
    _t_picker_controls="Enter open · k kill · Esc · Space/Tab mark ✓"
    set -- --preview= "$@"
  else
    _t_picker_controls="$_t_picker_controls · Ctrl-/ details"
  fi
  _t_picker_compact=
  _t_picker_filter_separator=' · '
  if [ -n "$_t_picker_size" ] && [ "${_t_picker_size#* }" -gt 0 ] && [ "${_t_picker_size#* }" -le 60 ]; then
    _t_picker_compact=1
    _t_picker_filter_separator='
'
    _t_picker_legend=
    _t_picker_margin=0,1
    _t_picker_controls="Enter $_t_picker_action · Esc
Space/Tab mark ✓"
    [ "${T_PICKER_SESSION_FILTERS:-}" != open ] || _t_picker_controls="Enter/k kill/Esc
Space/Tab mark ✓"
    set -- --height=100% "$@"
  fi
  printf '%s\n%s\n' "$_t_picker_controls" \
    "$_t_picker_label · Ctrl-A show all · Ctrl-F defaults · $_t_picker_hidden hidden" > "$T_PICKER_HEADER_DEFAULT"
  printf '%s\n%s\n' "$_t_picker_controls" \
    "${T_PICKER_ALL_LABEL:-All items} · Ctrl-A show all · Ctrl-F defaults" > "$T_PICKER_HEADER_ALL"
  if [ -n "$_t_picker_legend" ]; then
    printf '%s\n' "$_t_picker_legend" >> "$T_PICKER_HEADER_DEFAULT"
    printf '%s\n' "$_t_picker_legend" >> "$T_PICKER_HEADER_ALL"
  fi
  if [ -n "$_t_picker_columns" ]; then
    printf '%s\n' "$_t_picker_columns" >> "$T_PICKER_HEADER_DEFAULT"
    printf '%s\n' "$_t_picker_columns" >> "$T_PICKER_HEADER_ALL"
  fi
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
        kill) _t_picker_filters="Ctrl-T attached: $_t_picker_first${_t_picker_filter_separator}Ctrl-P pending: $_t_picker_second" ;;
      esac
      _t_picker_hidden=$(( $(wc -l < "$T_PICKER_ALL") - $(wc -l < "$_t_picker_dir/rows-$_t_picker_mask") ))
      printf '%s\n%s\n%s\n' "$_t_picker_controls" \
        "Ctrl-A show all · Ctrl-F defaults · $_t_picker_hidden hidden" \
        "$_t_picker_filters" > "$_t_picker_dir/header-$_t_picker_mask"
      if [ -n "$_t_picker_compact" ]; then
        printf '%s\n%s\n%s\n' "$_t_picker_controls" \
          "Ctrl-A all · Ctrl-F reset" "$_t_picker_filters" > "$_t_picker_dir/header-$_t_picker_mask"
        printf '%s hidden\n' "$_t_picker_hidden" >> "$_t_picker_dir/header-$_t_picker_mask"
      fi
      [ -z "$_t_picker_legend" ] || printf '%s\n' "$_t_picker_legend" >> "$_t_picker_dir/header-$_t_picker_mask"
      [ -z "$_t_picker_columns" ] || printf '%s\n' "$_t_picker_columns" >> "$_t_picker_dir/header-$_t_picker_mask"
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
  if [ -n "$_t_picker_source" ]; then
    printf '%s\n' "$_t_picker_source" > "$_t_picker_dir/source"
    if [ "${T_PICKER_SESSION_FILTERS:-}" = resume ]; then
      printf '%s\n' "${T_PICKER_AUTH_HOSTS:-}" > "$_t_picker_dir/auth-hosts"
      # The persistent warning needs another row; fzf 0.44 otherwise exhausts
      # its 60%-height window at 80x24 before rendering a focused session.
      [ -z "${T_PICKER_AUTH_HOSTS:-}" ] || set -- --height=100% "$@"
    fi
    T_PICKER_HEADER_WIDTH=${_t_picker_size#* }
    [ "${T_PICKER_HEADER_WIDTH:-0}" -gt 0 ] || T_PICKER_HEADER_WIDTH=${COLUMNS:-80}
    T_PICKER_HEADER_WIDTH=$(( T_PICKER_HEADER_WIDTH - 12 ))
    if [ -n "$_t_picker_size" ] && [ "${_t_picker_size%% *}" -gt 0 ] && [ "${_t_picker_size%% *}" -le 14 ]; then
      set -- --height=100% --border=none "$@"
      _t_picker_margin=0
      T_PICKER_HEADER_WIDTH=$(( ${_t_picker_size#* } - 4 ))
    fi
    export T_PICKER_HEADER_WIDTH
    export T_PICKER_FILTER_HELPER="$T_HOME/libexec/t_picker_filters.py"
    T_PICKER_COLOR=0
    if [ -z "${NO_COLOR+x}" ] && [ "${TERM:-dumb}" != dumb ] && [ -t 2 ]; then
      T_PICKER_COLOR=1
    fi
    export T_PICKER_COLOR
    set -- --ansi "$@"
  else
    unset T_PICKER_FILTER_HELPER
  fi
  cat > "$T_PICKER_TOGGLE" <<'SH'
if [ -n "${T_PICKER_FILTER_HELPER:-}" ]; then
  exec python3 "$T_PICKER_FILTER_HELPER" "$@"
fi
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
  if [ -n "${T_PICKER_FILTER_HELPER:-}" ]; then
    if [ -n "$_t_picker_show_all" ]; then
      printf '0\n' > "$T_PICKER_STATE"
      printf '0' > "$_t_picker_dir/filters-enabled"
    fi
    sh "$T_PICKER_TOGGLE" current || return $?
    _t_picker_header=$T_PICKER_CURRENT_HEADER
  fi
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
      open)
        _t_picker_first_key=ctrl-t:first
        [ -z "${T_PICKER_FILTER_HELPER:-}" ] || _t_picker_second_key=ctrl-u:unattached
        ;;
      kill) _t_picker_first_key=ctrl-t:first; _t_picker_second_key=ctrl-p:second ;;
    esac
    for _t_picker_spec in ctrl-a:all ctrl-f:default "$_t_picker_first_key" "$_t_picker_second_key"; do
      [ -n "$_t_picker_spec" ] || continue
      _t_picker_key=${_t_picker_spec%:*} _t_picker_action=${_t_picker_spec#*:}
      _t_picker_bind="${_t_picker_bind}${_t_picker_bind:+,}${_t_picker_key}:${T_PICKER_CLEAR_MARKS}execute-silent(sh \"\$T_PICKER_TOGGLE\" $_t_picker_action)+reload-sync(cat \"\$T_PICKER_CURRENT\")+transform-header(cat \"\$T_PICKER_CURRENT_HEADER\")"
    done
  fi
  if [ -n "${T_PICKER_FILTER_HELPER:-}" ]; then
    _t_picker_bind=
    for _t_picker_spec in ctrl-a:off ctrl-l:on ctrl-f:settings; do
      _t_picker_key=${_t_picker_spec%:*} _t_picker_action=${_t_picker_spec#*:}
      _t_picker_execute=execute-silent
      [ "$_t_picker_action" != settings ] || _t_picker_execute=execute
      _t_picker_bind="${_t_picker_bind}${_t_picker_bind:+,}${_t_picker_key}:${T_PICKER_CLEAR_MARKS}${_t_picker_execute}(sh \"\$T_PICKER_TOGGLE\" $_t_picker_action)+reload-sync(cat \"\$T_PICKER_CURRENT\")+transform-header(cat \"\$T_PICKER_CURRENT_HEADER\")"
    done
  fi
  if [ "${T_PICKER_SESSION_FILTERS:-}" = resume ]; then
    set -- --no-sort "$@"
    for _t_picker_spec in alt-t:turn alt-r:repo alt-g:agent alt-o:host alt-v:session; do
      _t_picker_key=${_t_picker_spec%:*} _t_picker_action=${_t_picker_spec#*:}
      _t_picker_bind="${_t_picker_bind},${_t_picker_key}:${T_PICKER_CLEAR_MARKS}execute-silent(sh \"\$T_PICKER_TOGGLE\" sort-$_t_picker_action)+reload-sync(cat \"\$T_PICKER_CURRENT\")+transform-header(cat \"\$T_PICKER_CURRENT_HEADER\")"
    done
  fi
  if [ "${T_PICKER_SESSION_FILTERS:-}" = open ]; then
    # Keep Enter's row-only output unchanged; k adds a private action sentinel.
    # shellcheck disable=SC2016
    set -- --bind='k:execute-silent(touch "$T_PICKER_VIEWS/kill")+accept' "$@"
  fi
  _t_fzf --multi --no-hscroll --margin="$_t_picker_margin" --delimiter="$(printf '\t')" --with-nth=-1 \
    --bind="enter:accept,esc:abort,ctrl-c:abort,space:toggle+down,tab:toggle+down,shift-tab:up${_t_picker_preview_bind}" \
    --bind="$_t_picker_bind" \
    --preview='printf "%s\n" {-1}' --preview-window='hidden:wrap:down:40%' \
    --header="$(cat "$_t_picker_header")" "$@" < "$T_PICKER_CURRENT" > "$_t_picker_dir/result"
  _t_picker_rc=$?
  if [ "$_t_picker_rc" = 0 ]; then
    [ ! -f "$_t_picker_dir/kill" ] || printf '__t_open_kill__\n'
    cat "$_t_picker_dir/result"
  fi
  return "$_t_picker_rc"
)

# Private script entry point; sh callers sourcing the profiles need no worker.
if [ "${1:-}" = --t-fzf-worker ]; then
  # Read the complete pipe before parsing (some sh versions truncate sourced pipes).
  _t_fzf_handoff=$(/bin/cat <&3) || exit $?
  exec 3<&-
  eval "$_t_fzf_handoff" || exit $?
  unset _t_fzf_handoff
  case ${1:-} in
    _t_fzf_single|_t_fzf_multi|_t_fzf_paginated|_t_fzf_filtered) "$@" ;;
    *) exit 2 ;;
  esac
fi
