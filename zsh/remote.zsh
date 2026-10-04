# → stderr (stdout is captured).
# A desktop-only slot has no tmux process to attach, but it still owns its
# worktree and conversation. Keep this separate from _dev_remote_resolve, whose
# result is strictly an attachable tmux slot.
_dev_remote_app_owner() {
  local repo="$1" slot="$2" dir="${DEV_REPOS[$1]:-}" base wtr
  [[ -n $dir ]] || return 1
  base=${dir:t}; wtr=${DEV_WORKTREE_ROOT:-}
  _dev_rows_all 2>/dev/null | awk -F'\t' \
    -v d="$(_dev_homerel "$dir")" -v s="$slot" \
    -v wtr="$(_dev_homerel "$wtr")" -v b="$base" '
      $1 != "local" && $5 == "app" {
        c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c)
        n=$4; sub(/^.*-/, "", n)
        if ((c==d || (wtr != "" && b != "" && index(c, wtr "/" b "/") == 1)) && (s == "" || n == s)) { print $1; exit }
      }'
}

_dev_remote_resolve() {
  local repo="$1" slot="$2"
  # _dev_rows_all columns: host(1) sid(2) cwd(3) slot(4) state(5) context(6) summary(7)
  local rows; rows=$(_dev_rows_all 2>/dev/null | awk -F'\t' '$1 != "local" && $5 != "app"')
  [[ -n $rows ]] || { echo "dev: no live dev sessions on any remote host (\`dev ls -r\`)." >&2; return 1; }
  # Match on the repo DIRECTORY (field 3 = session cwd), NOT the alias-derived
  # slot label (field 4): the same repo dir can carry different DEV_REPOS aliases
  # on different machines — a slot is `dev-dot-2` on mini but `dev-dotfiles-2`
  # here, both keying ~/code/dotfiles — so an alias-string compare silently MISSES
  # a slot that is genuinely live remotely, and `t open 2` then mints a duplicate
  # LOCAL session (the bug this fixes). Repos live at the same ~/code/<name> path
  # on every host, so the dir is the stable cross-host key; the slot NUMBER is
  # field 4's trailing -N. Fall back to the legacy alias-string match only when
  # the local alias has no dir (not a DEV_REPOS key here). The dir match also
  # excludes FOREGROUND rows (labels like `<repo>:<id>` / `<repo>:fg`, marked by
  # `:`) — they share the cwd but are bound to their terminal, not a tmux slot;
  # admitting them would make the trailing-dash split below yield a bogus
  # repo/slot pair (`dot:a4aa5f6a` has no `-`) and `_dev_remote_attach`/
  # `_dev_remote_delegate` would ssh with a malformed `t` command.
  local dir="${DEV_REPOS[$repo]:-}"
  # Worktree-aware: a slot's session may root at $DEV_WORKTREE_ROOT/<basename>/<slot>
  # (per-session worktree) instead of the canonical repo dir. The basename + worktree
  # root are stable across hosts (same as _dev_dir_in_scope), so accept either path
  # — else live worktree slots silently miss and `t open` mints a duplicate locally.
  local base="${dir:t}" wtr="${DEV_WORKTREE_ROOT:-}"
  local match
  if [[ -z $repo ]]; then
    match=$rows                                                  # bare → all remote
  elif [[ -n $dir && -n $slot ]]; then
    match=$(print -r -- "$rows" | awk -F'\t' -v d="$(_dev_homerel "$dir")" -v s="$slot" -v wtr="$(_dev_homerel "$wtr")" -v b="$base" '{ c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c) } (c==d || (wtr != "" && b != "" && index(c, wtr "/" b "/") == 1)) && $5 != "app" && $4 !~ /:/ && $4 ~ ("-" s "$")')
  elif [[ -n $dir ]]; then
    match=$(print -r -- "$rows" | awk -F'\t' -v d="$(_dev_homerel "$dir")" -v wtr="$(_dev_homerel "$wtr")" -v b="$base" '{ c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c) } (c==d || (wtr != "" && b != "" && index(c, wtr "/" b "/") == 1)) && $5 != "app" && $4 !~ /:/')
  elif [[ -n $slot ]]; then
    match=$(print -r -- "$rows" | awk -F'\t' -v w="${repo}-${slot}" '$4==w')
  else
    match=$(print -r -- "$rows" | awk -F'\t' -v w="${repo}-" 'index($4,w)==1')
  fi
  local n; n=$(print -r -- "$match" | grep -c .)
  (( n )) || { echo "dev: no live '$repo${slot:+ $slot}' session on any remote host (\`dev ls -r\` to see what's live where)." >&2; return 1; }

  local host hostslot
  if (( n == 1 )); then
    host=${match%%$'\t'*}
    hostslot=$(print -r -- "$match" | awk -F'\t' '{print $4}')
  elif [[ -t 1 ]] && command -v fzf >/dev/null 2>&1; then
    local picked
    picked=$(print -r -- "$match" | awk -F'\t' '{printf "%s\t%s\t%s/%-12s %s\n", $1, $4, $1, $4, $7}' \
          | _t_fzf --delimiter=$'\t' --with-nth=3 --no-hscroll \
                --prompt="dev -r ${repo:-pick} > " --height=40% --reverse) || return 1
    [[ -n $picked ]] || return 1
    host=${picked%%$'\t'*}
    hostslot=${${picked#*$'\t'}%%$'\t'*}
  else
    echo "dev: '$repo${slot:+ $slot}' is live in more than one place (install fzf or name a slot):" >&2
    print -r -- "$match" | awk -F'\t' '{printf "  %s  %s  %s\n", $1, $4, $7}' >&2
    return 1
  fi
  printf '%s\t%s\t%s\n' "$host" "${hostslot%-*}" "${hostslot##*-}"
}

# _term_title <text> — set the terminal/tab title via an OSC escape. When Terminal.app
# honors it, it STOPS auto-titling from the foreground process's argv — which is why
# a remote attach otherwise reads as the raw `ssh -t mini zsh -lic dev\ dotfiles\ 2`
# (nothing set a title, so Terminal fell back to the ssh command line). Empty <text>
# clears it so process tracking resumes. No-op when stdout is not a tty (cmd | cat).
# CAVEAT: a Terminal profile (or recent macOS) that composes the title from "active
# process name AND arguments" appends the ssh argv REGARDLESS of this OSC. The robust
# defense is therefore the ssh-call quoting itself: every remote `ssh -t … zsh -lic
# ${(qq)rcmd}` uses (qq) (single-quote), NOT (q) (backslash) — so even when the argv
# leaks into the title it reads one quoted command rather than escaped spaces.
_term_title() { [[ -t 1 ]] && printf '\e]0;%s\a' "$1" }

# _term_reset_mouse — disable every terminal mouse-tracking + bracketed-paste mode.
# WHY: a remote attach (`t open`/`t beam`/`on`, all `ssh -t … zsh -lic`) runs a TUI
# (tmux/claude) that turns mouse reporting ON. When that session dies UNCLEANLY — broken
# pipe / "Connection reset by peer" on a sleeping/dropped host — the remote tmux never
# sends its mouse-mode reset back, so the LOCAL Terminal is left in mouse-reporting mode:
# the scroll wheel then emits SGR mouse events that print as literal `35;..M` garbage and
# scrollback is dead until you `reset`. Registered as a precmd hook below so EVERY return
# to the prompt clears it — this is the only catch-all that also covers `on`, which
# `os.execvp`s into ssh in bin/t and so cannot clean up in-process (same reason its tab
# title is refreshed by precmd, not cleared in bin/t). At the zsh prompt mouse mode is
# always meant to be off (TUIs that want it enable+disable it themselves), so an
# unconditional reset here is safe; the sequences are invisible (no cursor move/output).
# Modes: 1000 press · 1002 drag · 1003 any-motion (the spammer) · 1005/1006/1015 encodings · 2004 bracketed paste.
_term_reset_mouse() { [[ -t 1 ]] && printf '\e[?1000l\e[?1002l\e[?1003l\e[?1005l\e[?1006l\e[?1015l\e[?2004l' }
add-zsh-hook precmd _term_reset_mouse

# _dev_local_slot_live <repo> <slot> — true if a dev-<repo>-<slot> tmux session is
# live on THIS machine (any dev-<repo>-* when <slot> is empty). Cheap (tmux only, no
# ssh): the gate that lets `t open` prefer a local slot before paying for a remote
# probe. The new/fg keywords are never "a live slot" (they mean fresh / foreground).
_dev_local_slot_live() {
  local repo="$1" slot="$2"
  [[ -n $repo ]] || return 1
  if [[ -n $slot && $slot != new && $slot != fg ]]; then
    tmux has-session -t "=dev-${repo}-${slot}:" 2>/dev/null
  else
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -q "^dev-${repo}-"
  fi
}

# _dev_default_host — the host `t open -r --new` starts a fresh remote session on when
# no --host is given. REMOTE_HOSTS is an UNORDERED assoc array, so "first remote host"
# is made deterministic as the alphabetically-first key (with one or two machines, the
# common case, there is nothing to disambiguate). Override per-invocation with --host.
# Returns 1 (and prints nothing) when no remote hosts are configured.
_dev_default_host() {
  (( ${#REMOTE_HOSTS} )) || return 1
  local -a hk; hk=(${(ok)REMOTE_HOSTS})
  print -r -- "$hk[1]"
}

# _dev_remote <repo> <slot> <fg> — `dev -r [repo [slot]]`: resolve a live REMOTE slot
# (host auto-inferred, _dev_remote_resolve) then ATTACH IN PLACE on its host. The
# explicit `-r` entry — `t open` also auto-detects a remote slot when none is live
# locally and calls _dev_remote_attach directly with an already-resolved row (no second
# scan). Moving a session between machines is `tbeam`, not this. <fg> forwards `-f`.
_dev_remote() {
  local repo="$1" slot="$2" fg="$3"
  # Repo-aware: a lone slot-shaped arg (`t open -r 4`) means slot 4 of the repo $PWD is
  # in — mirroring the local `t open 4` and `t kill -r 4` (see _dev_remote_kill). Without
  # this the bare `4` is mis-read as a remote repo alias and never matches. Only triggers
  # for a pure-digit positional that is not itself a DEV_REPOS key, and only when cwd maps
  # to a repo; a truly bare `t open -r` (no positional) stays the documented all-remote
  # picker. Inference failure leaves $repo as the raw digit so the resolver still errors.
  if [[ -z ${DEV_REPOS[$repo]:-} && -z $slot && $repo == <-> ]]; then
    local inferred; inferred=$(_t_infer_repo "$repo") && { slot=$repo; repo=$inferred; }
  fi
  local res
  if ! res=$(_dev_remote_resolve "$repo" "$slot"); then
    # Nothing live to attach. `-r` is attach-only; starting one is `-r --new`.
    local dh; dh=$(_dev_default_host) \
      && echo "  to START one remotely: t open ${repo:-<repo>}${slot:+ $slot} -r --new   (on $dh; --host <h> to choose)" >&2
    return 1
  fi
  _dev_remote_attach "$res" "$fg"
}

# _dev_remote_attach <res> <fg> — ATTACH IN PLACE on an already-resolved remote slot
# ("<host>\t<repo>\t<slot>", from _dev_remote_resolve): ssh -t + remote `t open`, so the
# session stays put on its host (the old `tgo`). `zsh -lic` for the usual reason
# (Homebrew/tmux login PATH, claude interactive PATH). <fg> forwards `-f`. Shared by the
# explicit `_dev_remote` entry and `t open`'s auto-detect path (slot resolved once).
_dev_remote_attach() {
  local res="$1" fg="$2"
  local host=${res%%$'\t'*} prepo=${${res#*$'\t'}%%$'\t'*} pslot=${res##*$'\t'}
  local target="${REMOTE_HOSTS[$host]:-$host}"

  if [[ ! -t 1 ]]; then
    echo "dev: attaching to a remote session needs a terminal." >&2
    echo "  From a terminal: t open $prepo $pslot   (it attaches on $host; \`t beam $prepo $pslot --from $host\` pulls it here)" >&2
    return 1
  fi
  echo "→ Attaching $host:dev-${prepo}-${pslot} (stays on $host; Ctrl-b d to detach)"
  # Let the remote login shell expand $$ so this delegated command is marked as
  # already checked there. Later commands in that shell can still check updates.
  local rcmd="T_UPDATE_PROMPTED=\$\$ t open ${(q)prepo} ${(q)pslot}"
  [[ -n $fg ]] && rcmd+=" --fg"
  _term_title "$host: $prepo $pslot"
  local rc=0
  ssh -t "$target" "zsh -lic ${(qq)rcmd}" || rc=$?
  _term_title ""
  return $rc
}

# _dev_remote_open <host> [open-args…] — start/attach a session on a SPECIFIC host
# (the `t open --host <h>` path). Unlike the `-r`/auto-detect attach — which only
# reaches an ALREADY-LIVE remote slot (host inferred via _dev_remote_resolve) — this
# forces <host>, so it can start a FRESH session there: `t open ff --host mini` opens a
# new mini session, `t open ff 3 --host mini` its slot 3. ssh -t + remote `t open`
# (zsh -lic for the Homebrew-tmux / interactive-claude PATH split); the session stays
# on <host> (Ctrl-b d to detach). The repo must exist at the same ~/code path there.
_dev_remote_open() {
  local host="$1"; shift
  local target="${REMOTE_HOSTS[$host]:-$host}"
  if [[ ! -t 1 ]]; then
    echo "t open --host: starting a session on $host needs a terminal." >&2
    return 1
  fi
  local rcmd="T_UPDATE_PROMPTED=\$\$ t open"; local a
  for a in "$@"; do rcmd+=" ${(q)a}"; done
  echo "→ $host: ${rcmd#T_UPDATE_PROMPTED=\$\$ } (stays on $host; Ctrl-b d to detach)"
  _term_title "$host: open ${(j: :)@}"
  local rc=0
  ssh -t "$target" "zsh -lic ${(qq)rcmd}" || rc=$?
  _term_title ""
  return $rc
}

# _dev_remote_delegate <repo> <slot> <verb> [extra…] — remote-aware shim for the
# per-slot read verbs (plan/read/…). If dev-<repo>-<slot> is NOT live locally but IS
# live on a $REMOTE_HOSTS host, run `t <verb> <repo> <slot> [extra]` there over ssh -t
# and return 0 (handled); else return 1 so the caller falls back to its local path.
# Needed because a beamed/remote slot's artifacts are host-local — the plan .md lives
# in ~/.claude/plans and the tmux log in ~/.tmux-logs on the slot's host, neither of
# which csync syncs — so the verb must run THERE. Host auto-inferred (_dev_remote_
# resolve); no TTY → print a hint and still return 0 (do not fall through to a local
# "No such session"). Mirrors _t_dev's auto-detect attach, but for non-attach verbs.
_dev_remote_delegate() {
  local repo="$1" slot="$2" verb="$3"; shift 3
  (( ${#REMOTE_HOSTS} )) || return 1
  [[ -n $repo ]] || return 1
  # An empty <slot> means the caller already failed to find its specific local target
  # (bare `t read` whose default-slot log is absent, bare `t pop` with no cwd-matched
  # session) and is asking us to probe ANY remote slot of <repo>. Don't gate on a
  # sibling local slot in that case — _dev_local_slot_live with an empty slot matches
  # any `dev-<repo>-*`, which would block delegation whenever an unrelated slot of the
  # same repo happens to be live here.
  [[ -n $slot ]] && _dev_local_slot_live "$repo" "$slot" && return 1
  local res; res=$(_dev_remote_resolve "$repo" "$slot") || return 1
  [[ -n $res ]] || return 1
  local host=${res%%$'\t'*} prepo=${${res#*$'\t'}%%$'\t'*} pslot=${res##*$'\t'}
  local target="${REMOTE_HOSTS[$host]:-$host}"
  if [[ ! -t 1 ]]; then
    echo "t $verb: dev-${prepo}-${pslot} is live on $host — run it from a terminal (or \`t beam $prepo $pslot --from $host\` to pull it here)." >&2
    return 0
  fi
  local rcmd="T_UPDATE_PROMPTED=\$\$ t $verb ${(q)prepo} ${(q)pslot}"; local a
  for a in "$@"; do rcmd+=" ${(q)a}"; done
  echo "→ $host:dev-${prepo}-${pslot}" >&2
  _term_title "$host: $verb $prepo $pslot"
  # Once the slot has resolved on a remote host, treat the delegation as handled
  # regardless of ssh's exit — ssh/the remote verb prints its own errors, and we
  # must not let the caller fall back to a misleading "No such session" message.
  ssh -t "$target" "zsh -lic ${(qq)rcmd}"
  _term_title ""
  return 0
}

# _dev_session_remote_fallback <session-name> <verb> [extra…] — the ONE shared
# "not live HERE, maybe live on another host" check that every per-slot verb routes
# its local miss through, so remote detection is uniform (pop/plan/kill all call this;
# t open uses the attach-flavoured _dev_remote_attach; t read is bin-native and calls
# _try_remote_delegate). Splits repo/slot off the dev-<repo>-<slot> name on the LAST
# dash (so multi-dash repos like dotfiles resolve) and hands them to
# _dev_remote_delegate. Returns 0 = handled on the slot's host over ssh -t (caller
# should `return`); 1 = no remote slot (caller falls through to its local error).
_dev_session_remote_fallback() {
  local session="$1" verb="$2"; shift 2
  local repo=${${session#dev-}%-*} slot=${session##*-}
  _dev_remote_delegate "$repo" "$slot" "$verb" "$@"
}

# _dev_remote_fg_kill <handle> <force> — the cross-host companion of _dev_kill_fg: kill a
# FOREGROUND / non-dev-slot claude (`<repo>:<id>` / `:fg` row) living on a REMOTE host.
# _dev_remote_resolve deliberately drops `:`-labelled rows, so those handles can't be
# resolved as dev slots; here we scan _dev_rows_all (which DOES include each host's fg
# rows) for the hosts carrying a match — exact label, the label's repo part (when <handle>
# is not a local DEV_REPOS key), or a bare id prefix — then delegate `t kill <handle>` to
# each host, letting its own _dev_kill_fg re-match and tear down every local target.
# Hosts are de-duped so one `t kill dotfiles-pr47` reaches all of a host's matches in one
# ssh. Returns 0 if every addressed host succeeded, 1 if a host was addressed but an ssh
# failed, 2 if nothing matched — so _dev_remote_kill only falls through to the dev-slot
# resolver on a true miss (2), not after a real (if failed) fg delegation.
_dev_remote_fg_kill() {
  local handle="$1" force="$2"
  local idpart="${handle##*:}"
  # _dev_rows_all columns: host(1) sid(2) cwd(3) label(4) state(5) context(6) summary(7).
  # fg rows carry a `:` in the label; dev slots use `-`. isrepo!="" ⇒ <handle> is a real
  # DEV_REPOS key, so the repo-part branch is suppressed (a bare `t kill -r dotfiles`
  # must resolve dev slots, not a same-repo fg row).
  local hosts; hosts=$(_dev_rows_all 2>/dev/null \
    | awk -F'\t' -v h="$handle" -v idp="$idpart" -v isrepo="${DEV_REPOS[$handle]:+1}" \
        "$_DEV_FG_MATCH_AWK"'
        $1 != "local" && $4 ~ /:/ && fgm($4, $2) { if (!seen[$1]++) print $1 }')
  [[ -n $hosts ]] || return 2
  local host rtarget rcmd rc=0
  for host in ${(f)hosts}; do
    rtarget="${REMOTE_HOSTS[$host]:-$host}"
    rcmd="T_UPDATE_PROMPTED=\$\$ t kill ${(q)handle}"
    [[ -n $force ]] && rcmd+=" -y"
    echo "→ Killing foreground '$handle' on $host"
    _term_title "$host: kill $handle"
    ssh -t "$rtarget" "zsh -lic ${(qq)rcmd}" || rc=1
  done
  _term_title ""
  return $rc
}

# _dev_remote_fg_open <handle> — the cross-host companion of _dev_attach_fg, mirroring
# _dev_remote_fg_kill: _dev_remote_resolve deliberately drops `:`-labelled rows, so an fg
# handle can never resolve as a dev slot; here we scan _dev_rows_all (which DOES carry
# each host's fg rows) for the ones matching — exact label, the label's repo part, or a
# bare id prefix — and run `t open` on the winner's host over ssh -t, letting that host's
# own _dev_open_fg attach it in place (or adopt it into the ssh terminal, as it would
# locally). This is what makes `t open <label>` work from the laptop for a session in a
# plain tmux session on mini (the retired pr-watch's `pr-dotfiles-N` was the case that
# surfaced it), in place of the hand-written `t on mini tmux attach -t …` the `t ls -r`
# footer used to have to recommend. One match → go; several → fzf-pick (no TTY: print the
# exact handles); none → 2, so the caller falls back to the local adopt path.
#
# It forwards the matched row's own LABEL, never <handle>: `t open ff fg` resolves here to
# a row labelled `ff:fg`, and forwarding `ff` would have the far side open a dev SLOT of
# repo ff instead (the label always carries a `:`, so _dev_fg_handle reads it as an fg
# handle there). Unlike the kill twin, the repo-part rule is NOT suppressed for a
# DEV_REPOS key — `t open <repo> fg` is exactly that ask, and a bare `t open <repo>`
# never reaches this path.
_dev_remote_fg_open() {
  local handle="$1"
  [[ -n $handle ]] || return 2
  local idpart="${handle##*:}"
  # _dev_rows_all columns: host(1) sid(2) cwd(3) label(4) state(5) context(6) summary(7).
  # fg rows carry a `:` in the label; dev slots use `-`.
  local rows; rows=$(_dev_rows_all 2>/dev/null \
    | awk -F'\t' -v h="$handle" -v idp="$idpart" -v isrepo="${DEV_REPOS[$handle]:+1}" -v ro=1 \
        "$_DEV_FG_MATCH_AWK"'
        $1 != "local" && $4 ~ /:/ && fgm($4, $2) { if (!seen[$1 "\t" $4]++) print $1 "\t" $4 "\t" $7 }')
  [[ -n $rows ]] || return 2
  local sel n; n=$(print -r -- "$rows" | grep -c .)
  if (( n == 1 )); then
    sel=$rows
  elif [[ -t 0 && -t 1 ]] && command -v fzf >/dev/null 2>&1; then
    sel=$(print -r -- "$rows" | _t_fzf --with-nth=2,1,3 --delimiter=$'\t' --prompt="t open > ") || return 0
  else
    echo "t open: '$handle' matches foreground sessions on several hosts — name one:" >&2
    print -r -- "$rows" | awk -F'\t' '{printf "  t open %s   (on %s — %s)\n", $2, $1, $3}' >&2
    return 0
  fi
  local host=${sel%%$'\t'*} label
  label=$(print -r -- "$sel" | awk -F'\t' '{print $2}')
  local target="${REMOTE_HOSTS[$host]:-$host}"
  [[ -t 1 ]] || { echo "t open: '$label' is on $host — attach it with: t on $host t open $label" >&2; return 1; }
  local rcmd="T_UPDATE_PROMPTED=\$\$ t open ${(q)label}"
  echo "→ Attaching foreground '$label' on $host"
  _term_title "$host: $label"
  ssh -t "$target" "zsh -lic ${(qq)rcmd}"
  local rc=$?
  _term_title ""
  return $rc
}

# _dev_remote_kill <repo> <slot> <force> — `dev -r kill <repo> [slot]`: resolve a live
# REMOTE slot (host auto-inferred / fzf-picked, _dev_remote_resolve) and tear it down
# ON that host by running `dev kill <repo> <slot>` there over ssh -t (so its confirm
# prompt — unless <force>/-y — works through the TTY). The mirror of a local dev kill.
_dev_remote_kill() {
  local repo="$1" slot="$2" force="$3"
  # Repo-aware: `t kill -r 4` / `t kill -r all` mean the repo $PWD is in, mirroring
  # the local `t kill` slot-only forms (see _dev_kill / _t_infer_repo). Without this
  # the raw `4`/`all` would be resolved as a remote repo alias.
  if [[ -z ${DEV_REPOS[$repo]:-} && -z $slot && ( $repo == <-> || $repo == all ) ]]; then
    slot=$repo; repo=$(_t_infer_repo "$slot")
  elif [[ -z $repo ]]; then
    repo=$(_t_infer_repo)
  fi
  # `all` can't be resolved as a single row (slot names are `<repo>-<N>`, never
  # `<repo>-all`); enumerate the remote hosts with any live dev-<repo>-* slot and
  # delegate `t kill <repo> all` to each — the remote _dev_kill iterates its own.
  if [[ $slot == all ]]; then
    [[ -n $repo ]] || { echo "dev: kill --remote all needs a repo (none inferred from \$PWD)." >&2; return 1; }
    # Discover hosts by repo DIRECTORY (field 3), not the alias-derived slot label
    # (field 4) — same reason _dev_remote_resolve does: a slot is `dev-dot-2` on
    # mini but `dev-dotfiles-2` here, both keying ~/code/dotfiles, so an alias
    # compare silently misses cross-host slots. Exclude foreground rows (`:` in
    # field 4). For each match keep the REMOTE's alias (slot field's prefix, last
    # dash split) so the delegated `t kill` keys off a name that exists there —
    # passing our local alias would have remote `_dev_kill` grep for sessions it
    # does not have. (Remote `_dev_kill` already unions sibling aliases for `all`
    # — see lines 1162–1175 — so one alias per host is enough to reach every slot
    # in that tree.) Fall back to the alias-string match when the local alias has
    # no DEV_REPOS dir.
    local dir="${DEV_REPOS[$repo]:-}"
    local pairs
    if [[ -n $dir ]]; then
      pairs=$(_dev_rows_all 2>/dev/null \
        | awk -F'\t' -v d="$(_dev_homerel "$dir")" '
          { c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c) }
          $1 != "local" && c==d && $4 !~ /:/ {
            a=$4; sub(/-[0-9]+$/, "", a);
            print $1 "\t" a
          }' | awk -F'\t' '!seen[$1]++')
    else
      pairs=$(_dev_rows_all 2>/dev/null \
        | awk -F'\t' -v r="$repo" -v w="${repo}-" '$1 != "local" && index($4,w)==1 {print $1 "\t" r}' \
        | awk -F'\t' '!seen[$1]++')
    fi
    [[ -n $pairs ]] || { echo "dev: no live '$repo' sessions on any remote host (\`dev ls -r\`)." >&2; return 1; }
    local pair h ralias rtarget rcmd rc=0
    for pair in ${(f)pairs}; do
      h=${pair%%$'\t'*}
      ralias=${pair#*$'\t'}
      rtarget="${REMOTE_HOSTS[$h]:-$h}"
      rcmd="T_UPDATE_PROMPTED=\$\$ t kill ${(q)ralias} all"
      [[ -n $force ]] && rcmd+=" -y"
      echo "→ Killing all dev-${ralias}-* on $h"
      _term_title "$h: kill $ralias all"
      # rc is a sticky failure flag — set on any host failure and never reset on
      # a later success — so the final exit is a clean boolean over the whole
      # fan-out (any host failed → non-zero). Was rc=$?: that captured the last
      # failure's exact code but never cleared on success, which made the value
      # arbitrary across iterations.
      ssh -t "$rtarget" "zsh -lic ${(qq)rcmd}" || rc=1
    done
    _term_title ""
    return $rc
  fi
  # Foreground / non-dev-slot sessions (`<repo>:<id>` / `:fg` rows) live outside the
  # dev-slot model _dev_remote_resolve handles (it excludes `:`-labelled rows), so try
  # them first when no slot was named. On a hit we delegate to each host, whose own
  # `t kill` re-runs the fg match and tears down its targets (see _dev_kill_fg). rc 2 =
  # no fg row matched → fall through to the dev-slot resolver; 0/1 = handled → done.
  if [[ -z $slot ]]; then
    _dev_remote_fg_kill "$repo" "$force"; local _frc=$?
    (( _frc != 2 )) && return $_frc
  fi
  local res; res=$(_dev_remote_resolve "$repo" "$slot") || return 1
  local host=${res%%$'\t'*} prepo=${${res#*$'\t'}%%$'\t'*} pslot=${res##*$'\t'}
  local target="${REMOTE_HOSTS[$host]:-$host}"
  echo "→ Killing $host:dev-${prepo}-${pslot}"
  local rcmd="T_UPDATE_PROMPTED=\$\$ t kill ${(q)prepo} ${(q)pslot}"
  [[ -n $force ]] && rcmd+=" -y"
  _term_title "$host: kill $prepo $pslot"
  local rc=0
  ssh -t "$target" "zsh -lic ${(qq)rcmd}" || rc=$?
  _term_title ""
  return $rc
}

# _dev_pull <host> <target> <repo> <slot> <fg> — pull a remote dev slot's session
# onto THIS machine and land it (the engine behind `tbeam <repo> <slot> --from <host>` —
# the RECEIVE direction, mirror of the default send). Finds the live dev-<repo>-<slot> on <host> via its
# _dev_session_rows (sid + cwd; slot omitted → the repo's first live slot), rsync's
# the transcript back, then stops the origin copy there (the MOVE — _tbeam_kill_owner,
# so exactly one live claude owns the id) and resumes it locally: a dev-<repo>-<slot>
# by default, or this terminal with <fg>. Reuses a local slot already running that id.
_dev_pull() {
  local host="$1" target="$2" repo="$3" slot="$4" fg="$5"
  command -v rsync >/dev/null 2>&1 || { echo "dev: rsync not found" >&2; return 1; }
  local rows; rows=$(ssh "$target" "zsh -lic _dev_session_rows" 2>/dev/null)
  [[ -n $rows ]] || { echo "dev: no live sessions on $host (check that t is installed and up to date there)" >&2; return 1; }
  # Match the slot column (field 3 = "<repo>-<num>"): exact when slot given, else the
  # repo's first live slot. Empty fields can't collapse — _dev_session_rows sentinels.
  local row
  if [[ -n $slot ]]; then
    row=$(print -r -- "$rows" | awk -F'\t' -v w="${repo}-${slot}" '$3==w {print; exit}')
    [[ -n $row ]] || { echo "dev: no live slot dev-${repo}-${slot} on $host" >&2; return 1; }
  else
    row=$(print -r -- "$rows" | awk -F'\t' -v w="${repo}-" 'index($3,w)==1 {print; exit}')
    [[ -n $row ]] || { echo "dev: no live slot for '$repo' on $host" >&2; return 1; }
  fi
  local sid=${row%%$'\t'*}
  local cwd=${${row#*$'\t'}%%$'\t'*}
  local fslot=${${${row#*$'\t'}#*$'\t'}%%$'\t'*}
  local agent=${${(ps:\t:)row}[7]:-claude}; _dev_agent_valid "$agent" || agent=claude   # field 7 (a 6-field host = claude)
  [[ -n $sid && $sid != - ]] || { echo "dev: $host/$fslot has no active conversation to pull" >&2; return 1; }

  # Check the source host's durable Codex rollout before stopping its owner.
  # A newer SQLite projection with a shorter JSONL file cannot be transferred
  # faithfully; the remote helper names the damaged file and leaves it running.
  if [[ $agent == codex ]]; then
    ssh "$target" "TB_SID=${(q)sid} zsh -lic _dev_codex_rollout_integrity" || {
      echo "tbeam: could not verify $host's Codex rollout; source session was not stopped" >&2
      return 1
    }
  fi

  echo "⟳ Pulling ${sid[1,8]}… ($cwd) from $host → here"
  # It's a MOVE: stop the origin copy on <host> FIRST, before snapshotting its
  # worktree + transcript, so a still-live claude there can't keep editing files
  # (the worktree push below would miss them) or appending to the transcript (the
  # pull below would miss the tail) between the snapshot and the move — the same
  # invariant tpush/tpop/tbeam protect.
  local killed
  killed=$(ssh "$target" "TB_SID=${(q)sid} zsh -lic _tbeam_kill_owner" 2>/dev/null | tail -1)
  [[ $killed == dev-* ]] && echo "✂ Stopped the live copy on $host ($killed)"

  # Carry the origin worktree's uncommitted edits with the move: commit-all + push its branch
  # ON the remote (over ssh, work passed in TB_* env like _tbeam_land) so the local worktree can
  # fast-forward to them below. The mirror of the SEND path's local _dev_worktree_beam_push.
  # Nonzero = the commit or push could not fully carry the work — land anyway (degraded, the
  # edits stay safe on $host; the reland path re-verifies what it needs) but say so loudly.
  if ! ssh "$target" "TB_WT=${(q)cwd} TB_HOST=${(q)$(hostname -s)} zsh -lic _dev_worktree_beam_push"; then
    echo "⚠ $host couldn't fully commit/push the worktree — the landing may miss its latest edits (they remain on $host at $cwd)" >&2
  fi

  # Transcript first: the collision reland below copies this sid's files into the new
  # slot's project dir, so they must be on local disk before the landing is decided.
  _tbeam_pull_transcript "$cwd" "$target" "$agent" "$sid" || return 1

  # Where does it land? Usually the recorded worktree (same path on every host); when THIS
  # machine's same-numbered slot is another session's — live, dirty, or diverged — reland
  # into a fresh slot instead of trampling it (_dev_beam_land_cwd, which also moves the
  # transcript to the new slot's project dir).
  local land; land=$(_dev_beam_land_cwd "$cwd" "$sid" "$host" "$agent") || return 1
  if [[ $land == "$cwd" ]]; then
    # Worktree mode: $cwd is the origin's per-session worktree (same root on every host).
    # Materialize it locally from its branch on origin if absent, rather than hard-failing.
    if [[ ! -d $cwd ]]; then
      local _pr _ps; _pr=$(_dev_repo_of_dir "$cwd"); _ps=${_pr#*$'\t'}; _pr=${_pr%%$'\t'*}
      [[ -n $_pr && -n $_ps ]] && _dev_worktree_enabled "$_pr" && _dev_worktree_create "$_pr" "$_ps" >/dev/null
    fi
    [[ -d $cwd ]] || { echo "dev: $cwd doesn't exist here — clone/sync the repo first." >&2; return 1; }
    _dev_worktree_beam_sync "$cwd"      # fast-forward to the edits the origin just pushed
  else
    cwd="$land"                          # relanded: resume in the fresh slot's worktree
  fi

  # If this exact id is already running in a local dev slot, reuse it (one owner).
  local existing s
  for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
    if [[ "$(tmux show-environment -t "=$s" CLAUDE_RESUME_ID 2>/dev/null | cut -d= -f2)" == "$sid" ]]; then
      existing="$s"; break
    fi
  done

  if [[ -n $fg ]]; then
    # Landed HERE: clear any stale "<host>: …" title a prior remote attach left set
    # (the OSC stops Terminal auto-titling, and tmux/claude below may not overwrite it).
    _term_title ""
    [[ -n $existing ]] && { echo "dev: already running locally in $existing — attaching."; tmux attach-session -t "=$existing:"; return; }
    echo "✓ Resuming ${sid[1,8]}… here"
    if [[ $agent == codex ]]; then ( cd "$cwd" && exec codex resume "$sid" ); else ( cd "$cwd" && exec claude -r "$sid" ); fi
    return
  fi

  local lrepo lslot session
  if [[ -n $existing ]]; then
    session="$existing"
  else
    read -r lrepo lslot < <(_dev_slot_for_cwd "$cwd")
    [[ -n $lrepo && -n $lslot ]] || { echo "dev: couldn't map $cwd to a dev slot" >&2; return 1; }
    session="dev-${lrepo}-${lslot}"
    _dev_resume_session "$session" "$cwd" "$sid" "$agent"
  fi
  echo "✓ Landed here as $session"
  if [[ -t 1 && -z $CLAUDE_CODE_SESSION_ID ]]; then
    # Title the local landing so the header reads "$session" (clearly local), NOT the
    # stale "<host>: …" a prior remote attach set — tmux (set-titles off) swallows OSC
    # from inside the pane, so this pre-attach write is the title that sticks.
    _term_title "$session"
    tmux attach-session -t "=$session:"
  else
    echo "  Attach: tmux attach -t $session"
  fi
}

# (tread removed — `t read` is reimplemented natively in bin/t.)

# _t_plan — the `t plan` verb: render the last plan a Claude session wrote. Resolves a session
# like the `t pop` family (repo+slot, a full dev-<repo>-<slot> name, or inside Claude the
# current session, --all to fzf-pick every project), then renders the last ~/.claude/plans/
# <slug>.md path referenced in its transcript — glow word-wraps for narrow mobile terminals,
# falling back to less. User-facing help lives in bin/t (`t plan -h`); the shim routes -h there.
_t_plan() {
  local sid
  if [[ "$1" == "--all" || "$1" == "-a" ]]; then
    local row
    row=$(_claude_sessions_fzf "") || return 1     # every project
    [[ -n $row ]] || return 1
    sid=${row%%$'\t'*}
  elif [[ -n "$1" ]]; then
    # repo/slot or full session name → tmux session → stashed CLAUDE_RESUME_ID
    # (stamped by _dev_new_session/_dev_resume_session and, for every other launch
    # path, by the claude-stamp-tmux SessionStart hook), falling back to the dir's
    # newest transcript only for pre-hook sessions. That fallback is ambiguous when
    # several slots share one repo dir (dev-api-1..5 all root at the same project,
    # so one ~/.claude/projects/<enc>/) — it returns whichever sibling wrote last,
    # not the slot you asked for. The hook is what makes this reliable now.
    # Mirrors tpop's resolution so `tplan api 1` lines up with `tpop api 1`.
    local session
    if [[ "$1" == dev-* ]]; then
      session="$1"
    else
      local repo="$1" slot="$2"
      # Repo-aware: a lone numeric arg is a SLOT of the repo $PWD is in
      # (`t plan 4` ≡ `t plan <cwd-repo> 4` — see _t_infer_repo).
      if [[ "$repo" == <-> && -z "$slot" ]]; then
        slot=$repo
        repo=$(_t_infer_repo "$slot") || { echo "Not inside a DEV_REPOS dir — name the repo (t plan <repo> $slot)." >&2; return 1; }
      fi
      if [[ -z "$slot" ]]; then                    # first existing slot for repo
        local n=1
        while (( n <= 20 )); do
          tmux has-session -t "=dev-${repo}-${n}:" 2>/dev/null && { slot=$n; break; }
          (( n++ ))
        done
      fi
      session="dev-${repo}-${slot}"
    fi
    if ! tmux has-session -t "=$session:" 2>/dev/null; then
      # Not live here — maybe beamed to / started on another host. Delegate the whole
      # `t plan` over there (shared _dev_session_remote_fallback): the plan .md lives in
      # that host's ~/.claude/plans (csync syncs transcripts, not plan files), so it
      # must be rendered on the slot's host.
      _dev_session_remote_fallback "$session" plan && return
      echo "No such session: $session" >&2; return 1
    fi
    if [[ $(_dev_agent_of_session "$session") == codex ]]; then
      echo "t plan: $session runs codex, which keeps no plan files (~/.claude/plans is Claude's)." >&2
      return 1
    fi
    sid=$(tmux show-environment -t "=$session" CLAUDE_RESUME_ID 2>/dev/null | cut -d= -f2)
    if [[ -z $sid ]]; then
      local dir; dir=$(tmux display-message -p -t "=$session:" '#{session_path}')
      local -a tx=( "$HOME/.claude/projects/${dir//[^A-Za-z0-9]/-}"/*.jsonl(Nom[1]) )
      sid=${${tx[1]:t}%.jsonl}
    fi
    [[ -n $sid ]] || { echo "Couldn't find a session id for $session." >&2; return 1; }
  elif [[ -n $CLAUDE_CODE_SESSION_ID ]]; then
    sid=$CLAUDE_CODE_SESSION_ID                     # current-session mode
  else
    local row
    row=$(_claude_sessions_fzf "$PWD") || return 1  # scoped to this dir
    [[ -n $row ]] || return 1
    sid=${row%%$'\t'*}
  fi

  local transcript=("$HOME"/.claude/projects/*/"$sid".jsonl(N))
  [[ -n $transcript ]] || { echo "No transcript found for session $sid" >&2; return 1; }

  # Last plan path referenced in the transcript — the plan as finally written.
  local plan
  plan=$(grep -ho "$HOME/.claude/plans/[^\"]*\.md" "$transcript[1]" 2>/dev/null | tail -1)
  [[ -n $plan && -f $plan ]] || { echo "This session has no saved plan." >&2; return 1; }

  if command -v glow &>/dev/null; then
    glow -p "$plan"
  else
    less "$plan"
  fi
}

# _dev_open_tab <cmd…> — run a command in a NEW tab of the local terminal app.
# The tab engine for multi-pick `t resume` (each extra revived slot gets its own
# tab running the ordinary `t open` attach). Terminal.app has no AppleScript
# verb for tabs, so the tab is a System Events Cmd+T — which needs Terminal
# granted Accessibility (System Settings → Privacy & Security) — and the script
# POLLS the front window's tab count before `do script in front window` (that
# phrasing targets the ACTIVE tab, which post-Cmd+T is the new one; a blind
# delay would race slow tab creation). iTerm2 has a first-class tab API, no
# Accessibility needed. Inside tmux the pane's $TERM_PROGRAM reads "tmux" — the
# real app is recovered from the server's launch env (tmux show-environment -g).
# rc 1 whenever a tab is impossible here — over ssh (the GUI is not where you
# are), an unrecognized terminal, osascript refused — so callers print attach
# hints instead.
_dev_open_tab() {
  local cmd="$*" tp=${TERM_PROGRAM:-}
  [[ -z ${SSH_CONNECTION:-} ]] || return 1
  if [[ -n ${TMUX:-} && ( -z $tp || $tp == tmux ) ]]; then
    tp=$(tmux show-environment -g TERM_PROGRAM 2>/dev/null); tp=${tp#TERM_PROGRAM=}
  fi
  cmd=${cmd//\\/\\\\}; cmd=${cmd//\"/\\\"}
  case $tp in
    Apple_Terminal)
      osascript >/dev/null 2>&1 <<EOS || return 1
tell application "Terminal"
  activate
  set tabCount to count tabs of front window
end tell
tell application "System Events" to keystroke "t" using command down
tell application "Terminal"
  repeat 40 times
    if (count tabs of front window) > tabCount then exit repeat
    delay 0.05
  end repeat
  do script "$cmd" in front window
end tell
EOS
      ;;
    iTerm.app)
      osascript >/dev/null 2>&1 <<EOS || return 1
tell application "iTerm2"
  tell current window to create tab with default profile
  tell current session of current window to write text "$cmd"
end tell
EOS
      ;;
    *) return 1 ;;
  esac
}

# _pr_state_stale <cached-state> <age-seconds> — is this cached PR state worth
# refetching? Prints nothing; returns 0 = refetch, 1 = the cache is good enough.
# <cached-state> is the raw cache value: a real state, '' when nothing has ever
# been cached, or '?' when the last lookup FAILED (offline, no auth, deleted PR).
# <age-seconds> is how long ago it was written, huge when there is no file.
# Factored out of the render loop because it is a POLICY, not a mechanism: the
# whole cost/staleness trade-off of the PR tag lives here and nowhere else.
_pr_state_stale() {
  local st=$1; local -i age=$2
  # MERGED/CLOSED are terminal — a merged PR never un-merges, so cache forever.
  [[ $st == MERGED || $st == CLOSED ]] && return 1
  # TODO(policy): '' (never asked) and 'OPEN' both want the 300s recheck below,
  # but '?' is different — it means we ASKED and could not tell. Offline, that
  # retries every 300s forever, forking a gh child per scan that is guaranteed to
  # fail. Decide whether a failed lookup should back off (and how far).
  (( age > 300 ))
}

# _pr_state_refresh <owner/repo#num> … — warm the pr/ state cache for these refs.
# ALWAYS called detached (`( _pr_state_refresh … & )`): the pickers read the cache
# and never block on the network, so a state lands on the NEXT invocation.
#
# Batched per REPO, not per PR, which is the entire point. Measured: `gh pr view`
# is ~0.41s for ONE pr, `gh pr list --state all --limit 100` is ~0.70s for a
# HUNDRED — so one call per repo already beats two individual lookups, and it
# warms every PR in the repo, including rows this scan never rendered. A ref older
# than the list window falls back to a single `gh pr view`, bounded so a scan that
# references many ancient PRs cannot fork-bomb gh.
_pr_state_refresh() {
  (( $# )) || return 0
  command -v gh >/dev/null 2>&1 || return 1
  local prdir="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/pr"
  mkdir -p "$prdir" 2>/dev/null || return 1
  # Every declaration is hoisted out of the loops below: an assignmentless
  # `local x` re-run on an already-local x does not redeclare, it PRINTS
  # `x=value` (the _ok=claw junk-output bug documented on _t_resume).
  local ref slug num st line f hd
  local -a parts
  local -A want got head
  local -i fallbacks
  for ref in "$@"; do
    slug=${ref%\#*}; num=${ref##*\#}
    [[ -n $slug && -n $num ]] || continue
    want[$slug]="${want[$slug]:-} $num"
  done
  for slug in ${(k)want}; do
    got=(); head=(); fallbacks=0
    for line in ${(f)"$(gh pr list --repo $slug --state all --limit 100 \
                          --json number,state,headRefOid --jq '.[]|"\(.number) \(.state) \(.headRefOid)"' 2>/dev/null)"}; do
      [[ -n $line ]] || continue
      parts=(${=line})
      got[$parts[1]]=$parts[2]; head[$parts[1]]=${parts[3]:-}
    done
    # Persist every state that ONE call returned, not just the refs asked about:
    # the entries are 6 bytes and terminal ones are cached forever, so warming the
    # whole repo now is what makes the NEXT new PR resolve on first sight instead
    # of reading "?" for a round trip. Already-terminal entries are skipped —
    # rewriting them would only churn an mtime nothing reads.
    for num in ${(k)got} ${=want[$slug]}; do
      f="$prdir/${slug//\//#}#$num"
      # A MERGED entry is terminal only once its .head sidecar exists — entries
      # cached before the sidecar was introduced get it filled in once.
      if [[ -f $f ]]; then
        st=$(<$f)
        [[ $st == CLOSED || ( $st == MERGED && -f $f.head ) ]] && continue
      fi
      st=${got[$num]:-}; hd=${head[$num]:-}
      # Older than the list window — one direct lookup, bounded so a scan that
      # references many ancient PRs cannot fork-bomb gh.
      if [[ -z $st ]] && (( fallbacks < 8 )); then
        (( fallbacks++ ))
        parts=(${=$(gh pr view "https://github.com/$slug/pull/$num" --json state,headRefOid \
                     --jq '"\(.state) \(.headRefOid)"' 2>/dev/null)})
        st=${parts[1]:-}; hd=${parts[2]:-}
      fi
      # The merged head sha, for _pr_state_tag's "work continued past the merge"
      # check. A sidecar rather than a second field, so every reader of the state
      # file (the bin/t and tfind Python twins) keeps reading a bare state.
      [[ $st == MERGED && -n $hd ]] &&
        print -rn -- "$hd" > "$f.head.$$.tmp" 2>/dev/null && mv -f "$f.head.$$.tmp" "$f.head" 2>/dev/null
      # '?' records a lookup that FAILED, so _pr_state_stale can tell "never
      # asked" from "asked and could not tell".
      print -rn -- "${st:-?}" > "$f.$$.tmp" 2>/dev/null && mv -f "$f.$$.tmp" "$f" 2>/dev/null
    done
    # Also rate-limit refreshes for a live slot whose last-mentioned PR is already
    # merged: later PRs can complete its work without appearing in its transcript.
    touch "$prdir/${slug//\//#}.checked" 2>/dev/null
  done
}

# _pr_state_tag <pr-url> [worktree] — the shared " · #N <state>" renderer behind the
# PR tag in both `t resume` and `t ls`. Sets $REPLY to the tag for a transcript's last PR URL
# (empty for an empty url), reading the pr/ cache ONLY — never a blocking gh call.
#
# It returns through REPLY rather than stdout for two reasons, and the first is a
# correctness one: a `$(_pr_state_tag …)` caller would run this in a SUBSHELL, so
# the $_PR_STALE appends below would mutate a copy and be thrown away — the batched
# refresh would then never fire and every unknown row would stay "?" forever.
# Second, it saves a fork per row, which is the cost `t ls` was just optimized to
# avoid paying per session.
#
# Refs whose cached state is missing or stale are appended to the caller's
# $_PR_STALE array rather than fetched here, so ONE batched _pr_state_refresh
# covers a whole scan; the caller calls _pr_state_flush when the scan is done.
# Callers must declare `local -a _PR_STALE` + `local -A _PR_SPAWNED` (dynamic
# scoping puts the caller's copies in reach) so two scans never share state.
#
# Unknown renders "?" rather than a bare "#N": a blank state is indistinguishable
# from "this session opened no PR", and the row you most want an answer about — the
# newest one — is ALWAYS the uncached one, since its PR was created after the last
# scan. Where gh cannot answer at all (the Linux node) it degrades to a bare "#N",
# because "?" on every row is noise, not information.
#
# With a [worktree] (a LIVE slot's, from `t ls`), a merged PR is checked against the
# work in that tree: "merged" alone reads as "this slot is done", which is wrong
# when the session kept iterating after the merge (the PR did not fix it, a follow-up
# is under way) — so uncommitted edits or commits not known to have landed render
# "merged, still in progress" instead. The tag stays until the session's next PR
# URL replaces it, since the last PR mention in the transcript is the one shown.
_pr_state_tag() {
  local pru=$1 wt=${2:-}
  REPLY=
  [[ -n $pru ]] || return 0
  local prdir="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/pr"
  local prp=${pru#github.com/}; prp=${prp/\/pull\//\/}
  local prkey=${prp//\//#} prnum=${pru##*/}
  local prcf="$prdir/$prkey" prraw= prst= prlab= prmt checked
  local -i wip=0 refresh=0
  if [[ -f $prcf ]]; then
    prraw=$(<$prcf)
    [[ $prraw == MERGED || $prraw == CLOSED || $prraw == OPEN ]] && prst=$prraw
  fi
  prlab=${prst:+${(L)prst}}
  [[ -z $prlab ]] && command -v gh >/dev/null 2>&1 && prlab='?'
  if [[ $prst == MERGED && -n $wt ]] && _pr_work_continued "$wt" "$prcf.head"; then
    prlab='merged, still in progress'
    wip=1
  fi
  if [[ -z ${_PR_SPAWNED[$prkey]:-} ]]; then
    prmt=$(zstat +mtime "$prcf" 2>/dev/null || echo 0)
    # a MERGED entry without its head sidecar predates it: fetch it once
    if _pr_state_stale "$prraw" $(( prmt ? EPOCHSECONDS - prmt : 999999999 )) ||
       { [[ $prraw == MERGED && ! -f $prcf.head ]] && command -v gh >/dev/null 2>&1; }; then
      refresh=1
    elif (( wip )); then
      checked=$(zstat +mtime "$prdir/${prkey%#*}.checked" 2>/dev/null || echo 0)
      _pr_state_stale OPEN $(( checked ? EPOCHSECONDS - checked : 999999999 )) && refresh=1
    fi
    if (( refresh )); then
      _PR_SPAWNED[$prkey]=1
      _PR_STALE+=("${prp%/*}#$prnum")
    fi
  fi
  REPLY=" · #$prnum${prlab:+ $prlab}"
}

# _pr_work_continued <worktree> <head-file> — has work gone on in <worktree> since
# its PR merged? 0 = yes. Check edits FIRST, even on a known merged commit. A clean
# tree can sit on a later merged PR, main, or a squash-merged commit whose tree
# appears in main's history. None is pending work just because its SHA differs
# from the last PR mentioned in the transcript. All evidence is local; the caller
# refreshes the repo's PR cache in the background when work still looks pending.
# Unknown/unreadable history falls back to the dirty check alone.
_pr_work_continued() {
  local wt=$1 hf=$2 head= cur dirty f main base tree trees
  [[ -e $wt/.git ]] || return 1
  dirty=$(git -C "$wt" status --porcelain 2>/dev/null) || return 1
  [[ -n $dirty ]] && return 0
  [[ -f $hf ]] && head=$(<$hf)
  [[ -n $head ]] || return 1
  cur=$(git -C "$wt" rev-parse HEAD 2>/dev/null) || return 1
  [[ $cur == $head ]] && return 1
  # The batched refresh already saves every merged head in this repo. A later PR
  # may have landed the slot's tip even though its transcript still names an old PR.
  for f in "${hf%#*}"\#*.head(N); do
    [[ -f ${f%.head} ]] || continue
    [[ $(<"${f%.head}") == MERGED && $(<"$f") == $cur ]] && return 1
  done
  main=$(git -C "$wt" rev-parse --verify refs/remotes/origin/main 2>/dev/null)
  if [[ -n $main ]]; then
    git -C "$wt" merge-base --is-ancestor "$cur" "$main" 2>/dev/null && return 1
    tree=$(git -C "$wt" rev-parse 'HEAD^{tree}' 2>/dev/null) || return 1
    base=$(git -C "$wt" merge-base "$cur" "$main" 2>/dev/null) || return 1
    # Squash merges change the commit ID. Match published snapshots too, including
    # older ones so an unrelated main update cannot make a finished slot look busy.
    trees=$(git -C "$wt" log --first-parent --format=%T "$base..$main" 2>/dev/null) || return 1
    [[ $'\n'$trees$'\n' == *$'\n'$tree$'\n'* ]] && return 1
  fi
  return 0
}

# _pr_state_flush — spawn ONE detached, per-repo-batched refresh for everything
# $_PR_STALE collected, then clear it. Two details are load-bearing, not tidy:
#   `&!` (background AND disown) rather than a plain `&`, because the shortest-lived
#   caller is the throwaway `zsh -lic` that bin/t runs _dev_session_rows in: it
#   calls this LAST and exits immediately, and an interactive zsh HUPs its running
#   jobs on exit, which would kill the refresh mid-`gh` and leave the state to be
#   refetched on every single run.
#   Stdio fully redirected, because that same stdout IS the machine-readable row
#   stream, and an ssh pipe under `t ls -r`: a stray byte from a child would slide
#   the table columns, and an inherited pipe would hold the connection open until
#   gh finished.
_pr_state_flush() {
  (( ${#_PR_STALE} )) || return 0
  _pr_state_refresh "${(@)_PR_STALE}" >/dev/null 2>&1 </dev/null &!
  _PR_STALE=()
}

# _t_resume_on_host <host> <sid> <wt> <agent> <attach|detach|fg> — `t resume --host`:
# revive a DEAD conversation on another machine instead of here. It is `t beam`'s send
# path minus the kill (the conversation is dead — nothing owns it here, and _t_resume
# already refused anything live by id): carry the recorded worktree's uncommitted edits
# to origin (_dev_worktree_beam_push — a no-op when the worktree is gone here, where the
# host rebuilds it from its branch), rsync the transcript over, then have the host land
# it through the shared _tbeam_land (worktree from origin, beam_sync, the slot-collision
# reland, a first-class dev slot). attach = ssh -t straight into it; fg = resume in the
# ssh foreground; detach = land and print the landed session name (last line).
