# Standalone nosleep runtime, also sourced by the optional shell adapter.
# Loading definitions never starts monitoring or changes machine settings.
typeset -g _NOSLEEP_ROOT="${${(%):-%x}:A:h:h}"
zmodload zsh/datetime

_nosleep_help_for() {
  local name="${1:?_nosleep_help_for: need a function name}"
  awk -v fn="$name" '
    /^#/                  { blk = blk $0 ORS; next }
    $0 ~ "^" fn "\\(\\)"  { printf "%s", blk; found = 1; exit }
                          { blk = "" }
    END                   { exit !found }
  ' "$_NOSLEEP_ROOT/zsh/nosleep.zsh" | sed 's/^# \{0,1\}//' | _nosleep_help_style
}

# _nosleep_help_style — the gh-style RENDERER, split out from _nosleep_help_for so the dynamic
# no-arg/error paths (which list the actual ${(k)DEV_REPOS}, not a static comment)
# render identically to `-h`. Reads plain text on stdin and styles it by the same
# conventions documented above _nosleep_help_for: line 1 "<name> — <tagline>" bolds the
# name; a "Usage:" line bolds the label; a bare "Section name:" is a header;
# 2-space-indented "term  <2+sp>desc" rows become cyan/dim two-column rows, their
# descriptions auto-aligned to the widest term. Color only when stdout is a TTY —
# and because this is the LAST stage of every help pipe, `-t 1` here is the real
# terminal test (so `cmd -h | cat` still comes out plain).
_nosleep_help_style() {
  local b='' d='' c='' r=''
  [[ -t 1 && -z ${NO_COLOR:-} && ${TERM:-} != dumb ]] && { b=$'\e[1m' d=$'\e[2m' c=$'\e[36m' r=$'\e[0m'; }
  awk -v b="$b" -v d="$d" -v c="$c" -v r="$r" '
    # Pass 1: buffer every line, detecting two-column rows and the widest term so
    # descriptions can align to a common column regardless of input spacing. A line
    # indented 3+ spaces that is NOT a 2-space kv row is a description CONTINUATION
    # (a wrapped second line of a row description) — remembered de-indented so pass 2
    # can hang it under the aligned description column, not its hand-typed indent.
    {
      raw[NR] = $0
      kvline = 0; contline = 0
      if ($0 ~ /^  [^ ]/ || $0 ~ /^      --/) {
        body = substr($0, 3)
        prefix = ""
        if (body ~ /^    --/) { prefix = "    "; body = substr(body, 5) }
        if (match(body, / {2,}/)) {
          iskv[NR]  = 1; kvline = 1
          kterm[NR] = prefix substr(body, 1, RSTART - 1)
          kdesc[NR] = substr(body, RSTART + RLENGTH)
          if (length(kterm[NR]) > maxw) maxw = length(kterm[NR])
        }
      } else if (eligible && $0 ~ /^   +[^ ]/) {
        # an indented line right after a kv row (or its continuation) is a wrapped
        # second line of that description — re-indented in pass 2. A Usage: line or
        # paragraph is NOT eligible, so its indented follow-on stays verbatim.
        iscont[NR] = 1; contline = 1
        ct = $0; sub(/^ +/, "", ct); cont[NR] = ct
      }
      eligible = (kvline || contline)
    }
    # Pass 2: classify and colorize each line.
    END {
      for (n = 1; n <= NR; n++) {
        line = raw[n]
        if (n == 1 && index(line, " — ")) {
          i = index(line, " — ")
          printf "%s%s%s%s\n", b, substr(line, 1, i - 1), r, substr(line, i)
        } else if (iskv[n]) {
          printf "  %s%s%s%*s%s%s%s\n", \
            c, kterm[n], r, maxw - length(kterm[n]) + 2, "", d, kdesc[n], r
        } else if (iscont[n]) {
          printf "%*s%s%s%s\n", maxw + 4, "", d, cont[n], r
        } else if (line ~ /^Usage:/) {
          printf "%s%s%s%s\n", b, "Usage:", r, substr(line, 7)
        } else if (line ~ /^[A-Z][A-Za-z &-]*:$/ || line ~ /^[A-Z][A-Z &-]+$/) {
          printf "%s%s%s\n", b, line, r
        } else {
          print line
        }
      }
    }
  '
}

# nosleep implementation notes (kept separate from its short user-facing help).
#
# Blocks SYSTEM sleep via `pmset disablesleep 1` + a background caffeinate, and holds
# the DISPLAY on while the lid is open (no idle dim, sleep or screensaver — the screen
# stays on for as long as nosleep runs), then LOCKS the screen and turns the display
# OFF the moment the lid closes — with sleep disabled a closed lid no longer sleeps, so it no longer locks,
# and the panel would stay lit behind the lid until the displaysleep timer; the Mac
# keeps running throughout (not when docked to an external display: macOS never
# slept on that lid close, so there is no lock to replace, and the closed lid is
# simply how the Mac sits). --display-on keeps the display hold and user-activity
# ping while shut without changing brightness. --dim implies that hold, dims to
# NOSLEEP_DIM_LEVEL (default 0), and restores brightness on open.
# It still requests a lock. --no-lock independently skips that request; use both
# flags for computer-use agents that need an unlocked, lit session. With the lid
# open, failed checks are advisories and nosleep runs until Ctrl-C. With the lid shut,
# by default it keeps holding only while BOTH signals stay fresh: internet (an HTTPS exchange with
# api.anthropic.com or api.openai.com) and tokens burning — a local claude, codex, cursor-agent or
# Claude/ChatGPT app mid-turn (Claude Code runs its own caffeinate while a turn is
# in flight; for every agent, bytes moving on its sockets since the last check — at least
# NOSLEEP_NET_MIN a probe, 48 KiB, and NOSLEEP_NET_BPS over longer gaps, 1 KiB/s,
# both overridable in ~/.zshrc.local). Network activity needs two samples: the first
# check records counters, and the next (after --every seconds) can detect work.
# Sleep stays blocked while sampling and retrying. Behind a shut lid, after grace
# expires, checks retry after 30, 60 and 120 seconds by default before restoring
# sleep and exiting. Recovery resets the retry budget; reconnecting also restarts
# activity sampling and its grace.
# --no-agent and --no-internet independently skip those signals; with neither
# enabled (or --forever), only lid handling and hold maintenance continue.
# Lid handling and sudo refresh continue throughout the backoff.
#
# Reconnection uses macOS auto-join: in System Settings > Wi-Fi, enable Automatically
# join this network for trusted networks. On macOS 26+, set Ask to join hotspots to
# Automatic for a nearby iPhone/iPad on the same Apple Account (Wi-Fi and Bluetooth on).
# Hotspot auto-join requires no known Wi-Fi network to be available; it may not switch
# away from connected Wi-Fi with a broken internet uplink. nosleep does not toggle the
# Wi-Fi radio, change saved networks, or store passwords.
# For a persistent, unconditional block use `sleep-manager disable` instead.

# Keep configuration as validated data; never evaluate the saved file as shell code.
_nosleep_config() { python3 "$_NOSLEEP_ROOT/libexec/nosleep_config.py" "$@"; }
source "$_NOSLEEP_ROOT/zsh/nosleep-fallback.zsh"

# nosleep — prevent system sleep
#
# Closing the lid locks the screen and turns off the display by default.
# Activity and connection checks allow sleep only while the lid is closed.
#
# USAGE
#   nosleep [flags]
#   nosleep config [--show | --edit]
#   t config                Nosleep settings, nearby networks and Keychain setup
#
# ON CLOSE
#   -d, --display MODE  off | on | dim
#   -l, --lock MODE     on | off
#
# CHECKS
#   -c, --checks MODE   all | agent | internet | none (until Ctrl-C)
#                      Replaces saved checks and clears forever
#   --require VALUE    all | none (until Ctrl-C) | N passing checks required
#                      Saved default: all; passing means busy or online
#
# CHECK SETTINGS
#   -e, --every SECS    Check interval in seconds (default 30)
#   -g, --grace SECS    Wait before retries when idle or offline (default 900)
#   -r, --retries N     Retry limit, 0–10 (default 3)
#   -b, --backoff SECS  Retry delay; doubles to 300 seconds (default 30)
#
# BATTERY PROTECTION
#   nosleep config     Optional battery cutoff, including with --forever
#                      Off by default; exit below 20% on battery when enabled
#
# HOTSPOT FALLBACK
#   --hotspot MODE     on | off
#   -h, --help          Show help
#
# EXAMPLES
#   nosleep -d dim -l off -c agent -e10
#   nosleep --require 1             # stay awake while busy OR online
#   nosleep --display=on --lock=off --checks=none
#
# SHORTCUTS
#   Existing switches and groups still work: -dML, -mLI, -AI, -f / --forever.
#   Bare -d / -l / -c mean display on / lock on / all checks.
#   -m / --dim / -dim dim; -M / --undim clear saved dimming;
#   -D / --display-off turn it off; -L / --no-lock skip locking.
#   -a / -A and -i / -I enable/disable agent and internet checks.
#   -C / --no-checks skips checks; -F / --no-forever resumes selected checks.
#   Modes take separate or =values; numeric settings allow attached values.
nosleep() {
  # Like the binary, select and load current code on every invocation, including
  # cd && nosleep and uncommitted edits. Preserve shell-local tuning and config.
  if (( $+functions[_t_reload_if_moved] )) && [[ -z ${_NOSLEEP_SOURCE_SELECTED:-} ]]; then
    _t_reload_if_moved || return $?
    local _NOSLEEP_SOURCE_SELECTED=1
    source "$_NOSLEEP_ROOT/zsh/nosleep.zsh" || return $?
    nosleep "$@"
    return $?
  fi
  [[ "$1" == config ]] && { shift; _nosleep_config "$@"; return $?; }
  if [[ "$1" == hotspot ]]; then
    [[ $# == 2 && $2 == setup ]] || { print -u2 -- 'usage: nosleep hotspot setup'; return 2; }
    python3 "$_NOSLEEP_ROOT/libexec/nosleep_hotspot.py" setup
    return $?
  fi
  [[ "$1" == -h || "$1" == --help ]] && { _nosleep_help_for nosleep; return 0; }
  local forever=0 display_on=0 dim=0 dim_requested=0 lock=1 grace=900 every=30 retries=3 backoff=30 defaults setting value
  local agent=1 internet=1 monitoring=1 requirement=all required_count=0 enabled_count=0
  local hotspot=0 hotspot_ssid='' hotspot_after=300 hotspot_timeout=30
  local battery_protection=0 battery_below=20
  defaults=$(_nosleep_config --values) || return $?
  for setting in ${(f)defaults}; do
    value=${setting#*=}
    case ${setting%%=*} in
      display_on) display_on=$value ;;
      dim) dim=$value ;;
      lock) lock=$value ;;
      forever) forever=$value ;;
      agent) agent=$value ;;
      internet) internet=$value ;;
      requirement) requirement=$value ;;
      grace) grace=$value ;;
      every) every=$value ;;
      retries) retries=$value ;;
      backoff) backoff=$value ;;
      hotspot) hotspot=$value ;;
      hotspot_ssid) hotspot_ssid=$value ;;
      hotspot_after) hotspot_after=$value ;;
      hotspot_timeout) hotspot_timeout=$value ;;
      battery_protection) battery_protection=$value ;;
      battery_below) battery_below=$value ;;
    esac
  done
  # Expand short-option groups before parsing. Value-taking options consume the
  # remainder of their group (or the next argument). Preserve the legacy -dim
  # spelling: it has always meant --dim, not -d -i -m.
  local arg group flag
  local -a nosleep_args
  while (( $# )); do
    arg=$1
    shift
    if [[ $arg == -?* && $arg != --* && $arg != -dim ]]; then
      group=${arg#-}
      while [[ -n $group ]]; do
        flag=${group[1]}
        group=${group[2,-1]}
        if [[ $flag == [dlc] && $group == \=* ]]; then
          nosleep_args+=("-$flag$group")
          break
        fi
        nosleep_args+=("-$flag")
        if [[ $flag == [egrb] ]]; then
          if [[ -n $group ]]; then
            nosleep_args+=("$group")
          elif (( $# )); then
            nosleep_args+=("$1")
            shift
          fi
          break
        fi
      done
    else
      nosleep_args+=("$arg")
      # Keep a long option's value intact too, including malformed negatives.
      if [[ $arg == --(every|grace|retries|backoff|require|hotspot) ]] && (( $# )); then
        nosleep_args+=("$1")
        shift
      fi
    fi
  done
  set -- "${nosleep_args[@]}"
  while (( $# )); do
    case $1 in
      -h|--help) _nosleep_help_for nosleep; return 0 ;;
      -f|--forever|-C|--no-checks) forever=1 ;;
      -F|--no-forever) forever=0 ;;
      -c|--checks|-c=*|--checks=*)
        value=all
        if [[ $1 == *=* ]]; then
          value=${1#*=}
        elif (( $# >= 2 )) && [[ $2 != -* ]]; then
          value=$2; shift
        fi
        case $value in
          all) agent=1 internet=1 ;;
          agent) agent=1 internet=0 ;;
          internet) agent=0 internet=1 ;;
          none) agent=0 internet=0 ;;
          *) echo 'nosleep: --checks takes all, agent, internet, or none' >&2; return 2 ;;
        esac
        forever=0 ;;
      -a|--agent) agent=1 ;;
      -A|--no-agent) agent=0 ;;
      -i|--internet) internet=1 ;;
      -I|--no-internet) internet=0 ;;
      --hotspot|--hotspot=*)
        if [[ $1 == *=* ]]; then value=${1#*=}
        elif (( $# >= 2 )) && [[ $2 != -* ]]; then
          value=$2; shift
        else
          print -u2 -- 'nosleep: --hotspot needs on or off'; return 2
        fi
        case $value in
          on) hotspot=1 ;;
          off) hotspot=0 ;;
          *) print -u2 -- 'nosleep: --hotspot takes on or off'; return 2 ;;
        esac ;;
      -d|--display|-d=*|--display=*)
        if [[ $1 == *=* ]]; then
          value=${1#*=}
        elif (( $# >= 2 )) && [[ $2 != -* ]]; then
          value=$2; shift
        elif [[ $1 == -d ]]; then
          # Preserve bare -d, including its saved-dimming behavior.
          display_on=1; shift; continue
        else
          echo 'nosleep: --display needs off, on, or dim' >&2; return 2
        fi
        case $value in
          off) display_on=0 dim=0 ;;
          on) display_on=1 dim=0 ;;
          dim) display_on=1 dim=1 ;;
          *) echo 'nosleep: --display takes off, on, or dim' >&2; return 2 ;;
        esac
        dim_requested=0 ;;
      --display-on) display_on=1 ;;
      -m|--dim|-dim) dim_requested=1 ;;
      -M|--undim) dim=0 ;;
      -D|--display-off|--no-dim) display_on=0 dim=0 ;;
      -L|--no-lock) lock=0 ;;
      -l|--lock|-l=*|--lock=*)
        value=on
        if [[ $1 == *=* ]]; then
          value=${1#*=}
        elif (( $# >= 2 )) && [[ $2 != -* ]]; then
          value=$2; shift
        fi
        case $value in
          on) lock=1 ;;
          off) lock=0 ;;
          *) echo 'nosleep: --lock takes on or off' >&2; return 2 ;;
        esac ;;
      --require|--require=*)
        if [[ $1 == *=* ]]; then
          requirement=${1#*=}
        else
          (( $# >= 2 )) || { echo 'nosleep: --require needs all, none, or a positive count' >&2; return 2; }
          requirement=$2; shift
        fi
        [[ $requirement == all || $requirement == none || $requirement =~ '^0*([1-9][0-9]*)$' ]] || {
          echo 'nosleep: --require takes all, none, or a positive count' >&2; return 2
        } ;;
      -g|--grace|-e|--every|-r|--retries|-b|--backoff)
        (( $# >= 2 )) || { echo "nosleep: $1 needs a value" >&2; return 2; }
        case $1 in
          -g|--grace) grace=$2 ;;
          -e|--every) every=$2 ;;
          -r|--retries) retries=$2 ;;
          -b|--backoff) backoff=$2 ;;
        esac
        shift ;;
      *) echo "nosleep: unknown option '$1' (see nosleep -h)" >&2; return 2 ;;
    esac
    shift
  done
  # Legacy --dim wins over legacy display switches. An explicit --display MODE
  # replaces the whole display choice and clears any earlier legacy dim request.
  (( dim_requested )) && dim=1
  (( dim )) && display_on=1
  monitoring=$(( ! forever && (agent || internet) ))
  [[ $requirement == none ]] && monitoring=0
  enabled_count=$(( agent + internet ))
  case $requirement in
    all) required_count=$enabled_count ;;
    none) required_count=0 ;;
    *)
      # Normalize as text before arithmetic: even huge input must not wrap around.
      if [[ $requirement =~ '^0*([1-9][0-9]*)$' ]]; then
        requirement=$match[1]
      else
        echo 'nosleep: --require takes all, none, or a positive count' >&2; return 2
      fi
      if (( monitoring )) && [[ $requirement != 1 && $requirement != 2 ]]; then
        echo 'nosleep: --require exceeds the number of enabled checks' >&2; return 2
      fi
      (( monitoring )) && required_count=$requirement ;;
  esac
  if (( monitoring && required_count > enabled_count )); then
    echo 'nosleep: --require exceeds the number of enabled checks' >&2; return 2
  fi
  [[ $grace == <-> && $every == <-> && $every -gt 0 ]] || { echo "nosleep: --grace/--every take a number of seconds" >&2; return 2; }
  [[ $retries == <-> && $retries -le 10 && $backoff == <-> && $backoff -ge 1 && $backoff -le 300 ]] || {
    echo "nosleep: --retries must be 0–10 and --backoff must be 1–300 seconds" >&2; return 2
  }
  [[ $hotspot_after == <-> && ${#hotspot_after} -le 10 && $hotspot_after -le 2147483647 && $hotspot_timeout == <-> && ${#hotspot_timeout} -le 3 && $hotspot_timeout -ge 1 && $hotspot_timeout -le 300 ]] || {
    print -u2 -- 'nosleep: hotspot delay must be nonnegative and timeout must be 1–300 seconds'; return 2
  }
  if (( hotspot )); then
    python3 "$_NOSLEEP_ROOT/libexec/nosleep_hotspot.py" validate --ssid="$hotspot_ssid" || return 2
  fi

  if [[ $(uname -s) != Darwin ]]; then
    print -u2 -- 'nosleep: macOS-only; help and saved-default configuration remain available'
    return 1
  fi

  local battery_power=unknown battery_level=-1 battery_text='Not checked (protection off)' battery_next_check=0
  if (( battery_protection )); then
    _nosleep_battery_update
    battery_next_check=$(( EPOCHSECONDS + 10 ))
    if [[ $battery_power == battery ]] && (( battery_level < battery_below )); then
      print -r -- "nosleep: battery ${battery_level}% is below ${battery_below}%; sleep hold not started"
      return 0
    fi
  fi

  # Teardown is idempotent: it runs from the INT trap, the EXIT trap (zsh scopes a
  # function's EXIT trap to the function, so it fires on every return path), and
  # the let-go branch below — whichever gets there first does the work. Its state
  # is GLOBAL on purpose: the EXIT trap fires after the function's locals are
  # unwound (verified — a local pid read as empty there, leaving caffeinate running
  # and the restore firing twice).
  typeset -g _NOSLEEP_CAF='' _NOSLEEP_DONE=0 _NOSLEEP_WHO='' _NOSLEEP_BRIGHT=''
  typeset -g _NOSLEEP_STATUS_ROWS=0 _NOSLEEP_TTY=0
  typeset -g _NOSLEEP_HOTSPOT_PID=''
  typeset -g _NOSLEEP_LOCK_FAILED=0 _NOSLEEP_DISPLAY_FAILED=0 _NOSLEEP_DIM_FAILED=0 _NOSLEEP_RESTORE_FAILED=0
  [[ -t 1 && ${TERM:-dumb} != dumb ]] && _NOSLEEP_TTY=1
  _NOSLEEP_NET=() _NOSLEEP_NET_AT=()
  _nosleep_restore() {
    (( _NOSLEEP_DONE )) && return 0; _NOSLEEP_DONE=1
    _nosleep_status_clear
    _nosleep_fallback_cancel
    _nosleep_wifi_cancel
    [[ -n $_NOSLEEP_CAF ]] && kill "$_NOSLEEP_CAF" 2>/dev/null
    # Restore a dimmed panel even when stopping behind a closed lid, then release
    # the display hold. Locking is independent of brightness restoration.
    if [[ -n $_NOSLEEP_BRIGHT ]] && ! _nosleep_undim; then
      print -u2 -- 'nosleep: could not restore display brightness'
    fi
    if _nosleep_lid_closed; then
      _nosleep_display_off || print -u2 -- 'nosleep: could not turn the display off'
    fi
    if sudo -n pmset -a disablesleep 0 2>/dev/null || sudo pmset -a disablesleep 0; then
      echo "nosleep: stopped"
    fi
  }
  trap '_nosleep_restore' EXIT
  trap '_nosleep_restore; return 130' INT TERM

  # A caffeinate -dimsu that outlived its shell (an old unconditional nosleep whose
  # terminal closed) holds the Mac awake no matter what this run decides — two were
  # found from days earlier, and a mere warning left a third running. Stop it: it
  # is exactly the hold this run exists to end. sleep-manager's own caffeinate is
  # the one deliberate persistent hold, so it is named, not killed.
  local -a strays; local pid keep
  keep=''; [[ -f /tmp/sleep-manager-caffeinate.pid ]] && keep=$(</tmp/sleep-manager-caffeinate.pid)
  strays=( ${(f)"$(ps -Axo pid=,ppid=,command= 2>/dev/null | awk '$2 == 1 && $3 ~ /caffeinate$/ && $4 == "-dimsu" {print $1}')"} )
  for pid in "${strays[@]}"; do
    [[ -n $pid ]] || continue
    if [[ $pid == "$keep" ]]; then
      echo "nosleep: sleep-manager's caffeinate (pid $pid) is holding sleep off too — \`sleep-manager enable\` releases it" >&2
    elif kill "$pid" 2>/dev/null; then
      echo "nosleep: stopped an orphaned caffeinate -dimsu (pid $pid) left by an earlier run"
    fi
  done
  # nomonitor: the backgrounded caffeinate is a plain child, not a job — no "[2] 82804"
  # notice on the terminal, and it stays parented to this shell, so it never reads
  # as an orphan to the sweep above.
  setopt localoptions nomonitor
  sudo pmset -a disablesleep 1 || return 1
  # The display is held on while the lid is open (-dims) — "the screen stays on unless
  # the lid is closed" (2026-09-25: the old -ims let pmset's 10-min battery displaysleep
  # blank the screen mid-turn). A lid close swaps to -ims so the display assertion never
  # fights display sleep behind the lid; --display-on and --dim keep -dims throughout.
  _nosleep_hold -dims

  # LAST-SEEN timestamps preserve grace across failed probes. Once a signal expires,
  # schedule bounded retries without sleeping through lid changes or sudo refresh.
  # Always take two activity samples, even with --grace 0, before judging idleness.
  local now busy_at online_at=$EPOCHSECONDS why checked_at=0 next_check=0 lid_was=0 lid_shut=0 lid_shut_was=0 pinged_at=0 probes=0
  local retry_count=0 retry_delay=$backoff retry_for='' missing online=1 was_online=1 refreshed_at=0
  local failed_agent=0 failed_internet=0 effective_passes=0
  local fallback_outage_at=-1 fallback_started_at=-1 fallback_deadline=0 fallback_done=0
  local wifi_text='Checking availability…' wifi_next_scan=0 wifi_observed_at=0
  typeset -g _NOSLEEP_WIFI_PID='' _NOSLEEP_WIFI_DIR=''
  # Presentation tracks observed signals separately from the grace timestamps,
  # which are seeded at startup and reset on reconnect without observing work.
  local busy_now=0 detected_at=0 online_checked_at=0 sleep_held=1 show_snapshot=1 status_shown=0
  busy_at=$online_at
  while :; do
    # A signal delivered inside a helper returns from that helper, not this loop.
    # Teardown has already run; never re-arm power assertions on the next tick.
    (( _NOSLEEP_DONE )) && return 130
    now=$EPOCHSECONDS
    if (( battery_protection && now >= battery_next_check )); then
      _nosleep_battery_update
      battery_next_check=$(( EPOCHSECONDS + 10 ))
      show_snapshot=1
      if [[ $battery_power == battery ]] && (( battery_level < battery_below )); then
        _nosleep_status_clear
        print -r -- "nosleep: battery ${battery_level}% is below ${battery_below}%; releasing sleep hold"
        _nosleep_restore
        return 0
      fi
    fi
    lid_shut=0
    _nosleep_lid_shut && lid_shut=1
    if (( lid_shut != lid_shut_was )); then
      # Recheck immediately on closure; opening the lid cancels a pending retry.
      next_check=0 retry_count=0 retry_delay=$backoff retry_for=''
      lid_shut_was=$lid_shut
      show_snapshot=1
    fi
    if _nosleep_lid_closed; then
      if (( ! lid_was )); then
        _nosleep_close_display
        lid_was=1
      fi
    else
      (( lid_was && ! display_on )) && _nosleep_hold -dims
      # Retry a transient restore failure on later open-lid ticks and at exit.
      [[ -n $_NOSLEEP_BRIGHT ]] && { _nosleep_undim || true; }
      lid_was=0
      _NOSLEEP_LOCK_FAILED=0 _NOSLEEP_DISPLAY_FAILED=0 _NOSLEEP_DIM_FAILED=0
    fi
    # The ping runs while the display is held on: lid open, or with display_on.
    # (behind a shut lid a user-activity ping would relight the panel _nosleep_lock slept)
    if (( (display_on || ! lid_was) && now - pinged_at >= every )); then
      pinged_at=$now
      # the display assertion holds display sleep, not the screensaver's idle timer —
      # a user-activity ping resets that (and would relight a panel that slept anyway)
      caffeinate -u -t 1 2>/dev/null &!
      # auto-brightness (the ambient sensor behind a shut lid) can move the level back
      (( lid_was && dim )) && { _nosleep_dim || true; }
    fi
    # Keep the sudo timestamp warm even during long retries (or --forever).
    if (( now - refreshed_at >= 30 )); then
      sudo -n -v 2>/dev/null
      refreshed_at=$now
      if ! _nosleep_pmset_held; then
        # Cleared under us — another nosleep's exit resets it for everyone (its restore
        # is global, and the survivor's caffeinate still holds IDLE sleep off, so nothing
        # looks wrong until the lid closes and the Mac sleeps). Re-arm rather than fight
        # over exits: while this run lives, the flag is its invariant.
        _nosleep_status_clear
        if sudo -n pmset -a disablesleep 1 2>/dev/null; then
          sleep_held=1
          echo "nosleep: the sleep-disable flag was reset under this run (another nosleep exiting?) — re-armed"
        else
          sleep_held=0
          echo "nosleep: the sleep-disable flag was reset under this run and sudo could not re-arm it — closing the laptop would let it sleep" >&2
        fi
        show_snapshot=1
      else
        sleep_held=1
      fi
    fi
    # Show the hold immediately, even while the first connectivity probe runs.
    if (( _NOSLEEP_TTY && ! status_shown )); then
      _nosleep_status
      status_shown=1
    fi
    _nosleep_fallback_tick
    _nosleep_wifi_tick
    if (( monitoring && now >= next_check )); then
      show_snapshot=1
      checked_at=$now
      next_check=$(( checked_at + every ))
      if (( internet )); then
        online=0
        if _nosleep_online; then
          online=1
          online_at=$EPOCHSECONDS
          if (( ! was_online )); then
            # Offline silence cannot establish that the task finished. Give a resumed
            # agent fresh baselines, grace and retries instead of exiting on stale work.
            busy_at=$online_at
            _NOSLEEP_NET=() _NOSLEEP_NET_AT=()
            probes=0 retry_count=0 retry_delay=$backoff retry_for='' detected_at=0
            _nosleep_status_clear
            if (( agent )); then
              echo "nosleep: network recovered — restarting agent activity sampling and grace"
            else
              echo "nosleep: network recovered — resuming internet checks"
            fi
          fi
        elif (( was_online )); then
          _nosleep_status_clear
          echo "nosleep: connection lost — holding awake while macOS auto-join can reconnect"
        fi
        (( _NOSLEEP_DONE )) && return 130
        online_checked_at=$EPOCHSECONDS
        was_online=$online
        now=$EPOCHSECONDS
        _nosleep_fallback_observe
        _nosleep_fallback_tick observed
      fi
      if (( agent )); then
        _nosleep_busy_at
        busy_now=$REPLY
      fi
      (( busy_now )) && busy_at=$busy_now detected_at=$busy_now
      (( _NOSLEEP_DONE )) && return 130
      now=$EPOCHSECONDS
      (( ++probes ))
      missing=''
      failed_internet=$(( internet && ! online && (! lid_shut || now - online_at > grace) ))
      failed_agent=$(( agent && probes > 1 && ! busy_now && (! lid_shut || now - busy_at > grace) ))
      effective_passes=$(( enabled_count - failed_agent - failed_internet ))
      if (( effective_passes < required_count )); then
        if (( failed_internet )); then
          missing=network why="the network has been down for $(( now - online_at ))s"
        else
          missing=activity why="no local agent activity for $(( now - busy_at ))s"
        fi
      fi
      if [[ -n $missing ]]; then
        if (( ! lid_shut )); then
          (( _NOSLEEP_TTY )) || echo "nosleep: advisory — $why; laptop open, sleep stays blocked"
          retry_count=0 retry_delay=$backoff retry_for=''
        else
          # A different missing signal gets its own retry budget. Network recovery
          # above also resets activity, since agents often stop sending while offline.
          if [[ $retry_for != $missing ]]; then
            retry_count=0 retry_delay=$backoff retry_for=$missing
          fi
          if (( retry_count >= retries )); then
            # Activity can exhaust before network grace. Every offline release
            # must allow the configured outage delay and bounded fallback first.
            if _nosleep_fallback_holds; then
              (( next_check > now + 30 )) && next_check=$(( now + 30 ))
            else
              _nosleep_status_clear
              echo "nosleep: letting go — $why; $retries retries exhausted"
              _nosleep_restore
              return 0
            fi
          else
            (( ++retry_count ))
            next_check=$(( now + retry_delay ))
            (( _NOSLEEP_TTY )) || echo "nosleep: $why — retry $retry_count/$retries in ${retry_delay}s (sleep stays blocked)"
            retry_delay=$(( retry_delay < 150 ? retry_delay * 2 : 300 ))
          fi
        fi
      else
        if (( retry_count )); then
          _nosleep_status_clear
          echo "nosleep: activity recovered — resuming normal checks"
        fi
        retry_count=0 retry_delay=$backoff retry_for=''
      fi
    fi
    if (( _NOSLEEP_TTY || show_snapshot )); then
      _nosleep_status
      status_shown=1 show_snapshot=0
    fi
    sleep 2
  done
}
# _nosleep_status uses the control loop's dynamically scoped locals. It only
# formats observed state; it never probes, resets grace, or changes power policy.
_nosleep_duration() {
  local seconds=$(( $1 > 0 ? $1 : 0 ))
  REPLY="${seconds}s"
  if (( seconds >= 60 )); then
    REPLY="$(( seconds / 60 ))m"
    (( seconds % 60 )) && REPLY+=" $(( seconds % 60 ))s"
  fi
  return 0
}
_nosleep_status_clear() {
  if (( ${_NOSLEEP_STATUS_ROWS:-0} )); then
    printf '\e[%dA\r\e[J' "$_NOSLEEP_STATUS_ROWS"
    _NOSLEEP_STATUS_ROWS=0
  fi
}
_nosleep_status() {
  local agent_text network_text next_text notice_text
  local sleep_text='Prevented' monitoring_text='Activity and internet checks'
  local grace_text every_text backoff_text retries_text=$retries item
  local requirement_text="$requirement (need $required_count of $enabled_count)"
  local display_text='Off' lock_text='Lock screen'
  local remaining signal_age line width=$(( ${COLUMNS:-80} - 1 )) row=0 styled=0 value primary detail
  local heading_style=1 agent_style=33 network_style=36 next_style=36
  local sleep_style=0 notice_style=33
  local -a rows styles config_labels config_values config_styles
  (( _NOSLEEP_TTY )) && [[ -z ${NO_COLOR:-} ]] && styled=1
  if (( dim )); then
    display_text='Dim'
  elif (( display_on )); then
    display_text='On'
  fi
  if (( ! lock )); then
    lock_text='No lock requested'
  fi
  if (( dim && lid_shut && lid_was && _NOSLEEP_DIM_FAILED )); then
    notice_text='Could not dim · display remains held on'
  fi
  if (( forever )); then
    agent_style=2 network_style=2 next_style=2
    agent_text='Not checked (--forever)' network_text='Not checked (--forever)'
    next_text='No checks (--forever)'
    sleep_text='Prevented until Ctrl-C' monitoring_text='Disabled (--forever)'
  elif [[ $requirement == none ]]; then
    agent_style=2 network_style=2 next_style=2
    agent_text='Not checked (--require none)' network_text='Not checked (--require none)'
    next_text='No checks (--require none)'
    sleep_text='Prevented until Ctrl-C' monitoring_text='Disabled (--require none)'
  else
    if (( ! agent )); then
      agent_style=2 agent_text='Not checked (--no-agent)'
      monitoring_text='Internet checks only'
    elif (( ! probes )); then
      agent_text='Checking activity…'
    elif (( busy_now )); then
      agent_style=32
      _nosleep_duration $(( now - detected_at ))
      agent_text="Active · ${_NOSLEEP_WHO:-local agent} (checked $REPLY ago)"
    elif (( probes == 1 )); then
      agent_text='Sampling activity… need a second check'
    elif (( detected_at )); then
      _nosleep_duration $(( now - detected_at ))
      agent_text="Quiet · ${_NOSLEEP_WHO:-local agent} last active $REPLY ago"
    else
      agent_text='No activity detected'
    fi
    network_text='Checking connection…'
    if (( ! internet )); then
      network_style=2 network_text='Not checked (--no-internet)'
      monitoring_text='Agent activity checks only'
    elif (( online_checked_at )); then
      _nosleep_duration $(( now - online_checked_at ))
      network_text="Online · checked $REPLY ago"
      network_style=32
      if (( ! online )); then
        network_text="Offline · checked $REPLY ago"
        network_style=31
      fi
    fi
    _nosleep_duration $(( next_check - now ))
    next_text="Check in $REPLY"
    (( ! probes )) && next_text='Taking the first sample…'
    if (( retry_count )); then
      next_style=33
      next_text="Retry $retry_count/$retries in $REPLY · waiting for $retry_for"
    elif (( lid_shut && probes && (agent * !!busy_now + internet * online < required_count) )); then
      next_style=33
      signal_age=0
      (( agent && ! busy_now )) && signal_age=$(( now - busy_at ))
      if (( internet && ! online && now - online_at > signal_age )); then
        signal_age=$(( now - online_at ))
      fi
      # Requiring one of two checks means both grace periods must expire.
      if (( required_count == 1 && enabled_count == 2 )); then
        signal_age=$(( now - busy_at < now - online_at ? now - busy_at : now - online_at ))
      fi
      remaining=$(( grace - signal_age ))
      _nosleep_duration "$remaining"
      next_text+=" · grace $REPLY left"
    fi
    if (( ! monitoring )); then
      next_style=2 next_text='No checks (both disabled)'
      sleep_text='Prevented until Ctrl-C' monitoring_text='Disabled (both checks off)'
    fi
  fi
  if (( _NOSLEEP_RESTORE_FAILED )); then
    notice_text='Could not restore display brightness · retrying'
  fi
  if (( _NOSLEEP_DISPLAY_FAILED )); then
    notice_text='Could not turn the display off'
  fi
  if (( _NOSLEEP_LOCK_FAILED )); then
    [[ -n $notice_text ]] && notice_text+=' · '
    notice_text+='Screen lock failed'
    notice_style='1;31'
  fi
  if (( ! sleep_held )); then
    heading_style='1;31' notice_style='1;31' sleep_style='1;31'
    sleep_text='Not prevented'
    notice_text='Sleep protection unavailable · closing may allow sleep'
  fi
  (( battery_protection && sleep_held )) && sleep_text='Prevented · battery cutoff active'
  rows=('nosleep'
        ''
        'Live status'
        "  Sleep     $sleep_text"
        "  Agents    $agent_text"
        "  Internet  $network_text"
        "  Next      $next_text")
  styles=("$heading_style" 0 '1;36' "$sleep_style" "$agent_style" "$network_style" "$next_style")
  rows+=("  Battery   $battery_text")
  styles+=(36)
  local hotspot_text='Off' hotspot_style=2
  if (( hotspot )); then
    hotspot_style=0
    if (( ! monitoring || ! internet )); then
      hotspot_text='Inactive (requires internet checks)'
    elif (( fallback_done )); then
      hotspot_text='Attempt complete · no verified internet recovery'
    elif (( fallback_started_at >= 0 )); then
      _nosleep_duration $(( fallback_deadline - now ))
      hotspot_text="Joining/rechecking internet · $REPLY left"
    elif (( fallback_outage_at >= 0 )); then
      _nosleep_duration $(( hotspot_after - now + fallback_outage_at ))
      hotspot_text="Waiting for continuous outage · $REPLY left"
    else
      _nosleep_duration "$hotspot_after"
      hotspot_text="On · after $REPLY offline"
    fi
  fi
  rows+=("  Hotspot   $hotspot_text")
  styles+=("$hotspot_style")
  if [[ -n $hotspot_ssid ]]; then
    rows+=("  Network   $hotspot_ssid · $wifi_text")
  else
    rows+=('  Network   Not configured')
  fi
  styles+=(0)
  if [[ -n $notice_text ]]; then
    rows+=("  Notice    $notice_text")
    styles+=("$notice_style")
  fi
  _nosleep_duration "$grace"; grace_text="$REPLY before retrying"
  _nosleep_duration "$every"; every_text=$REPLY
  _nosleep_duration "$backoff"; backoff_text="$REPLY, doubles to 5m"
  if (( ! monitoring )); then
    _nosleep_duration "$grace"; grace_text="$REPLY (inactive)"
    every_text+=' (inactive)'
    retries_text+=' (inactive)'
    _nosleep_duration "$backoff"; backoff_text="$REPLY (inactive)"
  fi
  (( ! monitoring )) && requirement_text="$requirement (inactive)"
  config_labels=('On close' Display Locking '' Checks Mode Require '' 'Check settings' Interval Grace Retries Backoff)
  config_values=('' "$display_text" "$lock_text" '' '' "$monitoring_text" "$requirement_text" '' ''
                 "$every_text" "$grace_text" "$retries_text" "$backoff_text")
  config_styles=('1;36' 0 0 0 '1;36' 0 0 0 '1;36' 0 0 0 0)
  local battery_policy=Off battery_cutoff="${battery_below}% (inactive)"
  if (( battery_protection )); then
    battery_policy=On battery_cutoff="${battery_below}% on battery"
  fi
  config_labels+=('' 'Battery protection' Enabled 'Below')
  config_values+=('' '' "$battery_policy" "$battery_cutoff")
  config_styles+=(0 '1;36' 0 0)
  rows+=('')
  styles+=(0)
  for (( item = 1; item <= ${#config_labels}; ++item )); do
    if [[ -n ${config_values[item]} ]]; then
      printf -v line '  %-8s  %s' "${config_labels[item]}" "${config_values[item]}"
    else
      line=${config_labels[item]}
    fi
    rows+=("$line")
    styles+=("${config_styles[item]}")
  done
  rows+=('' '  Options   nosleep --help · nosleep config' '  Stop      Ctrl-C')
  styles+=(0 2 2)
  _nosleep_status_clear
  for line in "${rows[@]}"; do
    (( ++row ))
    # Leave one column spare to avoid terminal autowrap breaking cursor-up redraws.
    if (( _NOSLEEP_TTY && width > 0 && ${#line} > width )); then
      line="${line[1,$(( width - 1 ))]}…"
    fi
    # Style only after clipping visible text so ANSI sequences never consume columns.
    # Keep the words as well as their colors; plain output carries the same meaning.
    if (( ! styled )); then
      print -r -- "$line"
    elif [[ $line != '  '* || ${styles[row]} == 2 ]] || (( width < 13 )); then
      printf '\e[%sm%s\e[0m\n' "${styles[row]}" "$line"
    else
      value=${line[13,-1]}
      primary=${value%% · *}
      detail=${value#"$primary"}
      printf '\e[1m%s\e[0m\e[%sm%s\e[0m%s\n' "${line[1,12]}" "${styles[row]}" "$primary" "$detail"
    fi
  done
  (( _NOSLEEP_TTY )) && _NOSLEEP_STATUS_ROWS=${#rows}
  return 0
}
# _nosleep_lid_shut — physical lid state, including docked clamshell mode.
# Macs without a lid are treated like an open laptop: checks remain advisories.
_nosleep_lid_shut() {
  local out
  out=$(ioreg -r -k AppleClamshellState -d 1 2>/dev/null) || return 1
  [[ $out == *'"AppleClamshellState" = Yes'* ]]
}
# _nosleep_hold <caffeinate flags> — (re)start nosleep's own caffeinate with these flags,
# stopping the previous one; its pid lives in the global _NOSLEEP_CAF for the restore.
# The gap between the kill and the start is covered by the pmset disablesleep flag.
_nosleep_hold() {
  [[ -n $_NOSLEEP_CAF ]] && kill "$_NOSLEEP_CAF" 2>/dev/null
  caffeinate "$1" &
  _NOSLEEP_CAF=$!
}
# _nosleep_lid_closed — true while the lid is shut AND macOS would sleep on that closure
# (both keys sit on IOPMrootDomain: one ~10ms ioreg). AppleClamshellCausesSleep is the
# kernel's own verdict — shouldSleepOnClamshellClosed(): no external display on power,
# no clamshell-disable — and it does NOT consult `pmset disablesleep` (that flag lives
# in the sleep-allowed gate, userDisabledAllSleep). So a docked Mac (lid shut, an
# external display driving it) reads No and nosleep leaves it alone: macOS never slept
# on that lid close, so there is no lock to replace — the first version locked the
# external display the moment nosleep started. A plain laptop stays Yes under nosleep's
# disablesleep, and the lock still lands.
_nosleep_lid_closed() {
  local out
  out=$(ioreg -r -k AppleClamshellState -d 1 2>/dev/null) || return 1
  [[ $out == *'"AppleClamshellState" = Yes'* && $out == *'"AppleClamshellCausesSleep" = Yes'* ]]
}
# Locking and display treatment are independent: --display-on and --dim lock unless
# --no-lock is selected. Helpers return status so failures are visible.
_nosleep_lock() {
  python3 -c 'import ctypes; ctypes.CDLL("/System/Library/PrivateFrameworks/login.framework/login").SACLockScreenImmediate()' 2>/dev/null
}
_nosleep_display_off() { pmset displaysleepnow 2>/dev/null; }
_nosleep_close_display() {
  local lock_result='no lock requested' display_result='display off'
  _NOSLEEP_LOCK_FAILED=0 _NOSLEEP_DISPLAY_FAILED=0
  if (( lock )); then
    if _nosleep_lock; then
      lock_result='screen locked'
    else
      lock_result='screen lock failed'
      _NOSLEEP_LOCK_FAILED=1
    fi
  fi
  if (( dim )); then
    if _nosleep_dim; then
      display_result='display dimmed'
    else
      display_result='could not dim display'
    fi
  elif (( display_on )); then
    display_result='display held on'
  else
    _nosleep_hold -ims
    if ! _nosleep_display_off; then
      display_result='could not turn display off'
      _NOSLEEP_DISPLAY_FAILED=1
    fi
  fi
  _nosleep_status_clear
  print -r -- "nosleep: laptop closed — $lock_result, $display_result"
}
# _nosleep_brightness [level] — the BUILT-IN display's brightness (0–1): prints it, and
# with a level sets it first (prints the level it was at before). DisplayServices is the
# private framework behind the brightness keys — no CLI ships one — called through python
# ctypes as _nosleep_lock calls login.framework. rc 1 with no built-in display or no call.
_nosleep_brightness() {
  python3 - "$@" 2>/dev/null <<'PY'
import ctypes, sys
cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
ids = (ctypes.c_uint32 * 16)(); n = ctypes.c_uint32()
cg.CGGetOnlineDisplayList(16, ids, ctypes.byref(n))
for d in ids[:n.value]:
    if not cg.CGDisplayIsBuiltin(d):
        continue
    b = ctypes.c_float()
    if ds.DisplayServicesGetBrightness(d, ctypes.byref(b)) != 0:
        sys.exit(1)
    if len(sys.argv) > 1 and ds.DisplayServicesSetBrightness(d, float(sys.argv[1])) != 0:
        sys.exit(1)
    print("%.4f" % b.value)
    sys.exit(0)
sys.exit(1)
PY
}
# Save the built-in panel's brightness once, dim it, and restore on open/exit.
# This helper has no locking or display-sleep side effects.
_nosleep_dim() {
  local was
  _NOSLEEP_DIM_FAILED=1
  was=$(_nosleep_brightness "${NOSLEEP_DIM_LEVEL:-0}") || return 1
  [[ -n $was ]] || return 1
  _NOSLEEP_DIM_FAILED=0
  [[ -n $_NOSLEEP_BRIGHT ]] || _NOSLEEP_BRIGHT=$was
  return 0
}
_nosleep_undim() {
  [[ -n $_NOSLEEP_BRIGHT ]] || return 0
  if ! _nosleep_brightness "$_NOSLEEP_BRIGHT" >/dev/null; then
    _NOSLEEP_RESTORE_FAILED=1
    return 1
  fi
  _NOSLEEP_BRIGHT=''
  _NOSLEEP_RESTORE_FAILED=0
}
# _nosleep_pmset_held — true while pmset's disablesleep flag is set (one ~10 ms read).
# The flag is what keeps a CLOSED lid from sleeping the Mac; caffeinate alone holds
# only idle sleep. It is global state that any nosleep's restore clears.
_nosleep_pmset_held() { pmset -g 2>/dev/null | grep -qE '^ *SleepDisabled[[:space:]]+1'; }

# Read only the internal battery, never an attached UPS. Malformed or failed
# observations clear the previous reading so stale charge cannot trigger an exit.
_nosleep_battery_read() {
  reply=(unknown '')
  local sample line power=unknown level=101 found=0 percent
  local -a lines
  sample=$(LC_ALL=C pmset -g batt 2>/dev/null) || return 0
  lines=("${(@f)sample}")
  case $lines[1] in
    "Now drawing from 'Battery Power'") power=battery ;;
    "Now drawing from 'AC Power'") power=ac ;;
    *) return 0 ;;
  esac
  for line in "${lines[@]}"; do
    [[ $line == *InternalBattery-* ]] || continue
    found=1
    [[ $line =~ 'InternalBattery-[0-9]+[[:space:]].*[[:space:]]([0-9]{1,3})%;' ]] || return 0
    percent=$(( 10#$match[1] ))
    (( percent <= 100 )) || return 0
    (( percent < level )) && level=$percent
  done
  if (( found )); then
    reply=("$power" "$level")
  else
    reply=(none '')
  fi
}

_nosleep_battery_update() {
  local -a reply
  _nosleep_battery_read
  battery_power=$reply[1] battery_level=${reply[2]:--1}
  case $battery_power in
    battery) battery_text="${battery_level}% · on battery" ;;
    ac) battery_text="${battery_level}% · AC power" ;;
    none) battery_text='No battery' ;;
    *) battery_text='Unknown · reading unavailable' ;;
  esac
}
# _nosleep_online — true when an HTTPS exchange with either agent provider completes.
# Any HTTP status counts (no -f): a 404 proves the network path; only DNS/connect/
# TLS failures — the outages that actually stop tokens burning — return nonzero.
_nosleep_online() {
  local endpoint
  for endpoint in https://api.anthropic.com/ https://api.openai.com/; do
    curl -s -o /dev/null --max-time 4 "$endpoint" 2>/dev/null && return 0
  done
  return 1
}
# NOSLEEP_NET_MIN / NOSLEEP_NET_BPS — what an agent CLI must move on its sockets (in +
# out) between two probes to count as working: at least NOSLEEP_NET_MIN bytes, AND at
# least NOSLEEP_NET_BPS × the seconds since its last probe. Two bounds because idle
# traffic is bursty, not a rate — measured at the prompt (5 s samples over 150 s):
# a claude keeps a 39 B keepalive every 15 s and opens a short-lived telemetry
# connection (~2.4 KB in / 3.1 KB out) every 20–60 s; a codex posts ~1.5 KB in /
# 14–17 KB out on its one persistent connection every ~56 s plus a ~7 KB short
# connection — so one 30 s window can hold ~25 KB of idle bytes, and the first
# version's single rate floor (256 B/s × 30 s = 7.7 KB) read every idle codex as
# working. A working agent is another scale: each request carries the whole context
# out (60–300 KB bodies here) and the stream answers at 8–10 KB per 5 s sustained.
# 48 KiB a probe clears the idle worst case ~2×; the 1 KiB/s rate keeps a long
# --every from letting idle bursts add up to it. Override either in ~/.zshrc.local.
typeset -g NOSLEEP_NET_MIN=${NOSLEEP_NET_MIN:-49152}
typeset -g NOSLEEP_NET_BPS=${NOSLEEP_NET_BPS:-1024}
# _nosleep_net_working <agent> <bytes> <elapsed> — true when <bytes> moved on an agent's
# sockets over <elapsed> seconds reads as a turn rather than idle chatter. The one place
# the network signal's POLICY lives (the accounting in _nosleep_busy_at is mechanical),
# so it can be reshaped — per-agent floors, a direction test — without touching the
# probe; test_zsh_nosleep_idle_bursts_stay_under_the_floor pins the measured idle
# patterns any rule must keep reading as idle.
_nosleep_net_working() {
  local agent=$1 bytes=$2 elapsed=$3 floor
  (( elapsed < 1 )) && elapsed=1
  floor=$(( NOSLEEP_NET_BPS * elapsed ))
  (( floor < NOSLEEP_NET_MIN )) && floor=$NOSLEEP_NET_MIN
  (( bytes >= floor ))
}
# _nosleep_agent_of_comm <comm> — REPLY = the agent a process name belongs to (claude ·
# codex · cursor), empty for anything else. Keep the CLI process-name match here
# because nosleep also works when the standalone t plugin is absent. (ps
# reports argv[0], and cursor-agent's launcher `exec -a`s its own path — verified.)
# The desktop apps run the same agent cores from inside a bundle, and those count too
# (their work is tokens burning like any CLI's): the ChatGPT app's Codex is
# ChatGPT.app/…/CodexCLI.app/Contents/MacOS/codex, the Claude app's Code sessions run
# ~/Library/Application Support/Claude/claude-code/<ver>/claude.app/Contents/MacOS/claude.
# Any OTHER bundled binary (the apps' node helpers, Computer Use, renderers) is not an agent.
_nosleep_agent_of_comm() {
  REPLY=''
  if [[ $1 == *.app/Contents/* ]]; then
    if [[ $1 == */ChatGPT.app/Contents/* && ${1:t} == codex ]]; then REPLY=ChatGPT
    elif [[ $1 == */Application\ Support/Claude/claude-code/* && ${1:t} == claude ]]; then REPLY='Claude app'
    fi
  else
    case "${1:t}" in
      claude) REPLY=claude ;;
      codex|codex-aarch64-*|codex-x86_64-*) REPLY=codex ;;
      cursor-agent) REPLY=cursor ;;
    esac
  fi
}
# _nosleep_busy_at — REPLY = now when a local agent CLI is working, else 0; _NOSLEEP_WHO
# names the agents last seen at it (the status line). Three signals, any suffices:
#   1. a `caffeinate` whose parent is `claude` — Claude Code holds a `caffeinate -i -t 300`
#      under itself while a turn is in flight (a refcount in the binary: respawned every
#      240 s while held, killed 30 s after the count drops to zero — so a claude reads
#      working for ≤30 s past its turn, a tail the grace absorbs), read off one ps.
#      Instant, no baseline; claude only — codex and cursor-agent spawn nothing of the kind.
#   2. bytes moved on the agent's sockets since the previous probe, judged by
#      _nosleep_net_working. nettop lists each process's open sockets with their LIFETIME
#      byte counts, and the accounting is PER SOCKET (_NOSLEEP_NET, keyed "<pid> <socket>"):
#      a socket seen before contributes its growth, one that appeared since the last probe
#      contributes all of it, a socket that closed drops its baseline, and a pid's first
#      sighting only records baselines. NOT the -P per-process total the first version
#      read as cumulative: it is a sum over the sockets open RIGHT NOW, so it drops when
#      a pooled connection is replaced (idle claudes' totals were seen falling 7 KB
#      between probes) and reads a busy turn on a fresh connection as negative.
# Rejected on a live machine, both wrong the same way (an IDLE agent reads busy):
#   - an ESTABLISHED :443 connection — idle agents keep pooled ones open (a codex at
#     its prompt held eight, a claude two) and they outlive the turn indefinitely;
#   - CPU time — an idle codex TUI burned 0.85 s in 47 s while a claude waiting on a
#     tool burned 1.36 s: the separation is inside the noise.
#   (And transcript mtime before both: file transfers can touch IDLE transcripts, and a
#   session mid-way through a long tool call had not written its transcript in 31 min.)
# Returns through $REPLY, never stdout: a $(…) caller runs in a SUBSHELL, where the
# baselines written to _NOSLEEP_NET would vanish and every probe would be a first
# sighting (the _pr_state_tag lesson). `nettop -n` is load-bearing — with name
# resolution a probe took 5.1 s, without it 40 ms — and a process with no socket
# gets NO row, not a zero one. The desktop apps' agent cores (the ChatGPT app's codex,
# the Claude app's claude — see _nosleep_agent_of_comm) are counted like any CLI: an
# early version skipped every *.app/Contents binary as _dev_ps_snapshot does for t ls,
# and nosleep let the Mac sleep under a ChatGPT/Claude app mid-task (2026-09-25). The
# byte floor is what keeps their idle chatter out — the ChatGPT app's codex held no
# socket at all while idle.
#   3. a power assertion the Claude or ChatGPT APP itself holds (_nosleep_app_asserting)
#      — the Claude app takes an Electron NoIdleSleep assertion while it works (5-min
#      refcounted, like Claude Code's caffeinate), which covers work whose process is
#      invisible here (Cowork runs its claude inside a VM).
# NOSLEEP_APPS — owner name (as `pmset -g assertions` prints it) → status-line label for
# the desktop apps whose own idle-sleep assertion reads as "working". Override or extend
# in ~/.zshrc.local.
typeset -gA NOSLEEP_APPS
(( ${#NOSLEEP_APPS} )) || NOSLEEP_APPS=( Claude 'Claude app' ChatGPT ChatGPT )
# _nosleep_app_asserting — true when a NOSLEEP_APPS app holds a system-sleep assertion
# right now; reply = their labels. One ~10 ms `pmset -g assertions`, read from its
# "Listed by owning process" lines: `pid 81176(Claude): [0x…] 00:03:35
# PreventUserIdleSystemSleep named: "Electron"`. Display-only assertions (the ChatGPT
# app's "Capturing") and user-activity pings are not work and do not count.
_nosleep_app_asserting() {
  # Match pmset's ASCII structure as bytes; descriptions need not be valid UTF-8.
  local LC_ALL=C line owner; reply=()
  while IFS= read -r line; do
    [[ $line =~ '^[[:space:]]*pid [0-9]+\(([^)]+)\):.*(PreventUserIdleSystemSleep|PreventSystemSleep|NoIdleSleepAssertion) named' ]] || continue
    owner=${match[1]}
    [[ -n ${NOSLEEP_APPS[$owner]:-} ]] && reply+=("${NOSLEEP_APPS[$owner]}")
  done < <(pmset -g assertions 2>/dev/null)
  reply=( "${(@u)reply}" )
  (( ${#reply} ))
}
typeset -gA _NOSLEEP_NET _NOSLEEP_NET_AT
typeset -g _NOSLEEP_WHO=''
_nosleep_busy_at() {
  local pid ppid comm prev now=$EPOCHSECONDS; local -A pcomm agent moved socks at; local -a caf args who
  while read -r pid ppid comm; do
    [[ $pid == <-> ]] || continue
    pcomm[$pid]=${comm:t}
    [[ ${comm:t} == caffeinate ]] && caf+=("$ppid")
    _nosleep_agent_of_comm "$comm"
    [[ -n $REPLY ]] && { agent[$pid]=$REPLY; args+=(-p "$pid"); }
  done < <(ps -Axo pid=,ppid=,comm= 2>/dev/null)
  for ppid in "${caf[@]}"; do
    [[ ${pcomm[$ppid]:-} == claude ]] && who+=("${agent[$ppid]:-claude}")
  done
  _nosleep_app_asserting && who+=("${reply[@]}")
  if (( ${#args} && $+commands[nettop] )); then
    local name bin bout rest cur='' key bytes
    while IFS=, read -r name bin bout rest; do
      if [[ $name == (tcp|udp)* ]]; then               # a socket of the process row above it
        [[ -n $cur && $bin == <-> && $bout == <-> ]] || continue
        key="$cur $name"; bytes=$(( bin + bout )); prev=${_NOSLEEP_NET[$key]:-}
        socks[$key]=$bytes
        if [[ -z $prev ]]; then (( moved[$cur] += bytes ))             # appeared since the last probe
        elif (( bytes > prev )); then (( moved[$cur] += bytes - prev )); fi
      else                                              # "<name>.<pid>" — an agent we asked about, or the header
        pid=${name##*.}; cur=''
        [[ $pid == <-> && -n ${agent[$pid]:-} ]] && cur=$pid
      fi
    done < <(nettop -x -n -s 1 -L 1 "${args[@]}" -J bytes_in,bytes_out 2>/dev/null)
  fi
  for pid in "${(k)agent[@]}"; do
    prev=${_NOSLEEP_NET_AT[$pid]:-}; at[$pid]=$now
    [[ -n $prev ]] || continue                          # first sighting: baselines only
    _nosleep_net_working "${agent[$pid]}" "${moved[$pid]:-0}" $(( now - prev )) && who+=("${agent[$pid]}")
  done
  # only what this probe saw survives: a closed socket, or a pid that is no longer an
  # agent (exited; the number may be reused), drops its baseline
  _NOSLEEP_NET=( "${(@kv)socks}" ); _NOSLEEP_NET_AT=( "${(@kv)at}" )
  if (( ${#who} )); then
    _NOSLEEP_WHO=${(j:, :)${(u)who}}; REPLY=$now
  else
    REPLY=0
  fi
}

