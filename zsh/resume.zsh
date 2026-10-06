_t_resume_on_host() {
  local host="$1" sid="$2" wt="$3" agent="$4" mode="$5"
  local target="${REMOTE_HOSTS[$host]:-$host}"
  command -v rsync >/dev/null 2>&1 || { echo "t resume --host: rsync not found" >&2; return 1; }
  if ! _dev_worktree_beam_push "$wt" "$host" >&2; then
    echo "⚠ couldn't fully commit/push $wt — $host may resume with stale code (the edits stay here)" >&2
  fi
  _tbeam_sync_transcript "$wt" "$target" "$agent" "$sid" >&2 || return 1
  local env="TB_CWD=${(q)wt} TB_SID=${(q)sid} TB_AGENT=${(q)agent}"
  case $mode in
    fg)     ssh -t "$target" "$env TB_MODE=fg zsh -lic _tbeam_land" ;;
    attach) ssh -t "$target" "$env TB_MODE=tmux TB_ATTACH=1 zsh -lic _tbeam_land" ;;
    *)      local out; out=$(ssh -o BatchMode=yes "$target" "$env TB_MODE=tmux zsh -lic _tbeam_land" 2>&1)
            local rc=$?
            print -r -- "${out##*$'\n'}"
            (( rc == 0 )) && [[ ${out##*$'\n'} == dev-* ]] ;;
  esac
}

# _t_resume — the `t resume` verb: revive a DEAD dev slot's last conversation.
# `t open` on a dead slot deliberately starts a FRESH claude (the worktree and its
# uncommitted work are reused, but not the chat); this is the counterpart that brings
# the chat back. Worktree-per-session is what makes it precise: each slot's worktree
# is its own ~/.claude/projects/<enc> dir, so "this slot's newest transcript" cannot
# be a sibling's (the ambiguity that plagues shared-tree repos — which are therefore
# refused here and pointed at the `t push -p` picker instead). Composes the existing
# engines: _dev_worktree_path (slot → recorded cwd), _dev_ensure_session_cwd (rebuild
# a reaped/synced-away worktree from its durable branch), _dev_resume_session (a
# first-class dev slot, CLAUDE_RESUME_ID stamped so pop/plan keep working). Candidates
# are EVERY conversation saved in a dead slot's worktree (newest first — an old one is
# still resumable), minus conversationless stubs (no real prompt → nothing to revive,
# and their untitled rows masked real ones). House picker convention: one candidate →
# use it, several → fzf, none → bail with a hint. The fzf picker is MULTI-SELECT
# (space toggles a ✓ on rows, tab works too; plain Enter is the usual single
# pick): every marked DEAD row
# revives into its own detached dev slot, the first revived (topmost = newest)
# attaches, the rest open in NEW terminal tabs (_dev_open_tab above; attach-hint
# fallback when tabs are impossible — ssh, unrecognized terminal, no
# Accessibility). A live mark is already open (noted, never
# resumed — one-live-owner), a second conversation marked for a slot an earlier mark
# just took is skipped (one tmux session per dev-<repo>-<slot> name), and --fg
# refuses a multi-pick (one terminal, one inline claude); outside any DEV_REPOS dir a bare
# `t resume` scans EVERY worktree repo (the
# `t ls` unscoped convention) with a repo column in the picker; -a/--all forces that
# widening from anywhere (the `t ls -a` flag). -r/--remote PULLS the latest
# transcripts from every $REMOTE_HOSTS host first (direct rsync — csync is periodic
# and needs a prompt on the far side, so "resume what just died on the other
# machine" cannot wait for it), then scans as usual. LIVE slots — local or on a
# $REMOTE_HOSTS host — are HIDDEN from the scan by default (a stderr count names
# them; `t open` is the verb for a running slot, and with a dozen live slots pinned
# to the top the dead rows this verb exists for scrolled off a phone screen);
# -l/--live shows them as labeled rows ("● active" / "● on <host>") whose pick
# ATTACHES in place instead of resuming (one-live-owner: a second `claude -r` on a
# live id diverges the transcript). An explicit `t resume <repo> <slot>` on a live
# slot always attaches — a direct ask, not a scan.
# -f/--fg resumes inline in THIS terminal (t pop's landing) instead of a slot.
# Dead rows carry the two shared picker signals (see _claude_session_rows and
# claude-stamp-tmux jobs 3-4): recency/date = max(transcript mtime, last-opened
# stamp) with ↻ marking opened-but-unwritten conversations, and a " · #N <state>"
# tag for the last PR URL in the transcript — merged/closed/open, or "?" while the
# state is not cached yet. State stays cache-only (never a blocking gh call), so a
# just-created PR reads "?" until the detached refresh lands it; it is rendered
# explicitly because a bare "#N" was indistinguishable from a session with no PR.
# User-facing help lives in bin/t (`t resume -h`); the t() shim routes -h there.
_t_resume() {
  setopt local_options null_glob bare_glob_qual
  local a no_tmux= all_flag= remote_flag= live_flag= days=30 _expect_days= on_host= _expect_host=; local -a pos
  for a in "$@"; do
    if [[ -n $_expect_days ]]; then days=$a; _expect_days=; continue; fi
    if [[ -n $_expect_host ]]; then on_host=$a; _expect_host=; continue; fi
    case "$a" in
      -f|--fg)     no_tmux=1 ;;
      -a|--all)    all_flag=1 ;;
      -r|--remote) remote_flag=1 ;;
      -l|--live)   live_flag=1 ;;
      --days)      _expect_days=1 ;;
      --days=*)    days=${a#--days=} ;;
      --host)      _expect_host=1 ;;
      --host=*)    on_host=${a#--host=} ;;
      -*)          echo "t resume: unknown flag: $a (t session resume -h for flags)" >&2; return 1 ;;
      *)           pos+=("$a") ;;
    esac
  done
  if [[ $days != all && $days != <-> ]]; then
    echo "t resume: --days takes a number of days or 'all' (got: ${days:-nothing})." >&2; return 1
  fi
  # --host <h>: revive the pick ON <h> instead of here — the dead conversation is
  # carried there by the `t beam` send machinery (_t_resume_on_host below).
  if [[ -n $_expect_host || ( -n ${(M)@:#--host*} && -z $on_host ) ]]; then
    echo "t resume: --host takes a host (one of: ${(k)REMOTE_HOSTS:-none configured})." >&2; return 1
  fi
  local repo="${pos[1]:-}" slot="${pos[2]:-}"
  # Repo-aware defaults (mirrors t plan/paste): lone numeric arg is a SLOT of the
  # cwd repo; bare `t resume` infers the repo too — and OUTSIDE any DEV_REPOS dir
  # it widens to EVERY worktree repo instead of erroring (the `t ls` unscoped
  # convention: no repo context → show everything; the picker grows a repo
  # column). -a/--all forces that widening even inside a repo dir (same flag as
  # `t ls -a`). A lone numeric slot still demands a repo — "slot 3" of no
  # particular repo is a guess, and other verbs refuse the same way.
  local all_mode=
  if [[ -n $all_flag ]]; then
    [[ -z $repo && -z $slot ]] || { echo "t resume: --all scans every worktree repo — drop '$repo${slot:+ $slot}'." >&2; return 1; }
    all_mode=1
  elif [[ "$repo" == <-> && -z "$slot" ]]; then
    slot=$repo
    repo=$(_t_infer_repo "$slot") || { echo "Not inside a DEV_REPOS dir — name the repo (t session resume <repo> $slot)." >&2; return 1; }
  elif [[ -z "$repo" ]]; then
    repo=$(_t_infer_repo) || all_mode=1
  fi

  # The scan targets: one named/inferred repo, or (all-repos mode) every
  # worktree-enabled repo deduped by canonical dir — `dot` and `dotfiles` key one
  # dir, so it is scanned once under its canonical alias (_dev_repo_of_dir's
  # basename-first rule), not once per alias with duplicate candidates.
  local -a repos
  if [[ -n $all_mode ]]; then
    local -A _seen; local _k _dirA _canon
    for _k in ${(ko)DEV_REPOS}; do
      _dirA=${DEV_REPOS[$_k]:A}
      [[ -n ${_seen[$_dirA]:-} ]] && continue
      _seen[$_dirA]=1
      _canon=$(_dev_repo_of_dir "${DEV_REPOS[$_k]}") || continue
      _canon=${_canon%%$'\t'*}
      _dev_worktree_enabled "$_canon" && repos+=("$_canon")
    done
    (( $#repos )) || { echo "No worktree-enabled repos configured (configured: ${(k)DEV_REPOS})." >&2; return 1; }
  else
    [[ -n ${DEV_REPOS[$repo]:-} ]] || { echo "Unknown repo: $repo (configured: ${(k)DEV_REPOS})" >&2; return 1; }
    if ! _dev_worktree_enabled "$repo"; then
      echo "t resume: $repo opts out of worktree-per-session, so its slots share one project dir and a slot's last conversation is ambiguous — pick one with \`t session push -p\` instead." >&2
      return 1
    fi
    repos=($repo)
  fi

  # Live dev sessions by ROOTED PATH (any alias — dev-dot-2 and dev-dotfiles-2 can
  # key one worktree), so a live slot is recognised however it was named.
  local -A live; local s p
  for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
    p=$(tmux display-message -p -t "=$s:" '#{session_path}' 2>/dev/null)
    [[ -n $p ]] && live[${p:A}]=$s
  done

  # -r/--remote: PULL the scan targets' transcripts from every $REMOTE_HOSTS host
  # FIRST (direct rsync --update per worktree project dir — union rule: newer
  # wins, nothing deleted; $HOME is identical across machines so the project-dir
  # encoding matches verbatim), then scan as usual. This is what "resume the
  # conversation that just died on the other machine" needs: csync's convergence
  # is periodic AND driven by a precmd hook, so a headless host pushes nothing to
  # iCloud until someone opens a shell THERE — the direct pull closes that
  # window. A pulled conversation is then an ordinary candidate (a dead session
  # has no home host). rc 23 = remote glob matched nothing (no sessions for that
  # repo there) — not an error.
  if [[ -n $remote_flag ]]; then
    (( ${#REMOTE_HOSTS} )) || { echo "t resume -r: no REMOTE_HOSTS configured (set them in ${T_LOCAL_RC})." >&2; return 1; }
    # Include filters, not a remote glob: the far side's login shell is zsh,
    # whose nomatch ABORTS on a glob with no hits (rc 1 — read as "unreachable"
    # for any repo with no sessions on that host). One rsync per host covers
    # every scan target; rc 23 = source dir absent there (no claude yet) — fine.
    local _h _tgt _enc _prc
    local -a _incs
    for repo in $repos; do
      _enc=${${:-${DEV_WORKTREE_ROOT}/${DEV_REPOS[$repo]:t}}//[^A-Za-z0-9]/-}
      _incs+=(--include="${_enc}-*/" --include="${_enc}-*/**")
    done
    for _h in ${(ko)REMOTE_HOSTS}; do
      _tgt=${REMOTE_HOSTS[$_h]}
      rsync -a --update --timeout=10 -e "ssh -o ConnectTimeout=3 -o BatchMode=yes" \
        "${(@)_incs}" --exclude='*' \
        "$_tgt:$HOME/.claude/projects/" "$HOME/.claude/projects/" 2>/dev/null
      _prc=$?
      # codex rollouts: the whole date tree (small, append-only files; union rule);
      # rc 23 = no ~/.codex/sessions there. A pulled rollout shows in the scan via
      # _codex_rollout_scan before codex has indexed it.
      if (( _prc == 0 || _prc == 23 )); then
        rsync -a --update --timeout=10 -e "ssh -o ConnectTimeout=3 -o BatchMode=yes" \
          --include='*/' --include='rollout-*.jsonl' --include='rollout-*.origin' --exclude='*' \
          "$_tgt:$HOME/.codex/sessions/" "${CODEX_HOME:-$HOME/.codex}/sessions/" 2>/dev/null
      fi
      if (( _prc == 0 || _prc == 23 )); then
        echo "⟳ pulled ${_h}'s latest transcripts" >&2
        # Cache the host's short hostname (once) so the origin column below can
        # translate a `<sid>.origin` stamp back to the friendly $REMOTE_HOSTS key.
        if [[ ! -s $HOME/.cache/t/hostnames/$_h ]]; then
          mkdir -p "$HOME/.cache/t/hostnames" 2>/dev/null
          ssh -o ConnectTimeout=3 -o BatchMode=yes "$_tgt" hostname -s 2>/dev/null > "$HOME/.cache/t/hostnames/$_h"
        fi
      else
        echo "t resume -r: $_h unreachable (rc=$_prc) — skipped; its newest transcripts may be missing here." >&2
      fi
    done
  fi

  # ONE live-slot scan (one-live-owner invariant): a synced transcript here does
  # NOT mean the slot is idle — a local tmux slot or another host may still be
  # driving `claude -r` on that session id, and a second `claude -r` would append
  # to the same .jsonl with no locking and diverge the conversation. _dev_rows_all
  # when $REMOTE_HOSTS exist (this machine + every host), else a local-only
  # _dev_session_rows; fg rows (`:`-labelled) dropped. Live slots become LABELED
  # PICKER ROWS below ("● active" / "● on <host>") whose pick ATTACHES in place
  # instead of resuming — the picker itself says where everything is, rather than
  # stderr notes nobody reads. The per-repo awk over the cached remote rows builds
  # slot→host/alias/title maps (matching the repo's dir OR its per-session-worktree
  # path, same rule as _dev_remote_resolve so a `dev-dot-2` on mini matches
  # `dev-dotfiles-2` here — both key one dir); the alias is captured so an attach
  # needs no second scan.
  local all_rows= remote_rows= live_all=
  if (( ${#REMOTE_HOSTS} )); then
    live_all=$(_dev_rows_all 2>/dev/null)
  else
    live_all=$(_dev_session_rows 2>/dev/null | awk -F'\t' '{print "local\t" $0}')
  fi
  all_rows=$(print -r -- "$live_all" | awk -F'\t' 'NF && $4 !~ /:/')
  # Live session IDS — every row, foreground ones included. The slot rules below
  # match by PATH, and that misses a conversation resumed into a DIFFERENT slot
  # than the one it was recorded in: a codex rollout names the worktree it started
  # in (its first line) while `codex resume` from slot 5 runs it in slot 5's tree,
  # so slot 13 read as dead and its thread was offered for a second resume — which
  # codex answers by hanging silently (mini ff-13, 2026-09-21: the thread had been
  # live in dev-ff-5 for 3.5 days). Keyed on the id, a candidate whose conversation
  # is running ANYWHERE becomes a live row pointing at its owner.
  # live_sid[sid] = loc \t alias \t slot \t summary \t agent, loc = here | <host>
  # | fg:<host> (a foreground owner: alias = its `t open` label, slot = -).
  local -A live_sid; local _lh _ls _lc _ln _lst _lctx _lsm _lag2
  while IFS=$'\t' read -r _lh _ls _lc _ln _lst _lctx _lsm _lag2; do
    [[ -n $_ls && $_ls != - && -n $_ln ]] || continue
    [[ $_lh == local ]] && _lh=here
    if [[ $_ln == *:* ]]; then
      live_sid[$_ls]="fg:${_lh}"$'\t'"$_ln"$'\t'-$'\t'"$_lsm"$'\t'"${_lag2:-claude}"
    else
      live_sid[$_ls]="$_lh"$'\t'"${_ln%-*}"$'\t'"${_ln##*-}"$'\t'"$_lsm"$'\t'"${_lag2:-claude}"
    fi
  done <<< "$live_all"
  remote_rows=$(print -r -- "$all_rows" | awk -F'\t' '$1 != "local"')
  # Local live titles, keyed by short session name (dev- prefix stripped).
  local -A local_sum local_agent; local _lsn _lsum _lag
  while IFS=$'\t' read -r _lsn _lsum _lag; do
    [[ -n $_lsn ]] && { local_sum[$_lsn]=$_lsum; local_agent[$_lsn]=${_lag:-claude}; }
  done < <(print -r -- "$all_rows" | awk -F'\t' '$1 == "local" {print $4 "\t" $7 "\t" $8}')

  # Candidates: dead slots whose worktree project dir holds a transcript —
  # EVERY conversation in the slot, newest first, not just the newest .jsonl
  # (an old conversation is still resumable; hiding it read as "resume lost my
  # session"). Conversationless stubs (an open-then-exit, a beam artifact — no
  # real user prompt, so _transcript_title prints nothing) are skipped: they
  # rendered as untitled rows and, being newest, often masked the real
  # conversation. A slot whose worktree is LIVE (by path, local or remote) is
  # not resumable — explicit ask attaches, scan skips. A slot whose tmux NAME
  # is taken by a session rooted elsewhere is unresumable AND wrong to attach
  # (see the collision branch below). NOTE: every `local` here is hoisted OUT
  # of the loops — an assignmentless `local x` re-run on an already-local x
  # does not redeclare, it PRINTS `x=value` (the `_ok=claw` junk-output bug).
  local -a cands slots tx
  local -a pending _mpaths _mrows _mf _PR_STALE
  local -A remote_live_host remote_live_alias remote_live_sum remote_live_agent remote_live_state
  local -A meta_title meta_pr live_seen
  local -a sidlive
  local _p _mr _mrest
  local n wt sid busy rhost _rdir _rbase _rhost _rn _ralias _rsum _rag _rst _ok _stale stale_path agent _ag
  local txf title when ep org orgf hf skipped=0 hidden_live=0
  local reopened opf opep REPLY
  local _rwtr=${DEV_WORKTREE_ROOT:-}
  # The two picker signals shared with _claude_session_rows/tfind (see the
  # claude-stamp-tmux notes): opened/ = the last-opened stamps (recency =
  # max(transcript, stamp), ↻ on stamp-newer rows); the PR tag is _pr_state_tag,
  # which owns the pr/ cache (cache-only reads — NEVER a blocking gh call) and
  # collects stale refs into _PR_STALE for the one batched _pr_state_flush below.
  local opdir="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/opened"
  local -A _PR_SPAWNED
  # Recency window (--days, default 30; 'all' disables) + this machine's short
  # hostname, to blank the origin column for locally-run conversations.
  local cutoff=0 selfhost=$(hostname -s 2>/dev/null)
  [[ $days != all ]] && cutoff=$(( EPOCHSECONDS - days * 86400 ))
  # origin → friendly $REMOTE_HOSTS key, via the hostname cache the -r pull
  # populates (~/.cache/t/hostnames/<alias> = that host's `hostname -s`).
  local -A host_alias; local _hcf
  for _hcf in "$HOME"/.cache/t/hostnames/*(N); do
    host_alias[$(<$_hcf)]=${_hcf:t}
  done
  for repo in $repos; do
    _rdir=${DEV_REPOS[$repo]}; _rbase=${_rdir:t}
    remote_live_host=(); remote_live_alias=(); remote_live_sum=(); remote_live_agent=(); remote_live_state=()
    if [[ -n $remote_rows ]]; then
      while IFS=$'\t' read -r _rhost _rn _ralias _rst _rsum _rag; do
        [[ -n $_rn ]] && { remote_live_host[$_rn]=$_rhost; remote_live_alias[$_rn]=$_ralias; remote_live_state[$_rn]=$_rst; remote_live_sum[$_rn]=$_rsum; remote_live_agent[$_rn]=${_rag:-claude}; }
      done < <(print -r -- "$remote_rows" | awk -F'\t' -v d="$(_dev_homerel "$_rdir")" -v wtr="$(_dev_homerel "$_rwtr")" -v b="$_rbase" '
        { c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c) }
        (c==d || (wtr != "" && b != "" && index(c, wtr "/" b "/") == 1)) {
          n = $4; sub(/^.*-/, "", n)
          r = $4; sub(/-[^-]+$/, "", r)
          print $1 "\t" n "\t" r "\t" $5 "\t" $7 "\t" $8
        }')
    fi
    # Which slots to look at. NOT a fixed 1..20 range (which silently capped the
    # scan — a busy repo runs well past 20, and every slot above the ceiling was
    # invisible BOTH live and dead, reading as "t resume drops my sessions"):
    # _dev_repo_slots discovers them from tmux + worktrees on disk + saved
    # project dirs, and the remote-live slots found just above are unioned in
    # (a slot live only on another host may have no local trace at all).
    if [[ -n $slot ]]; then
      slots=($slot)
    else
      slots=(${(f)"$(_dev_repo_slots "$repo")"} ${(k)remote_live_host})
      slots=(${(nou)slots:#})
    fi
    for n in $slots; do
      wt=$(_dev_worktree_path "$repo" "$n")
      if _dev_app_slot_reserved "$wt"; then
        if [[ -n $slot ]]; then
          print -u2 -- "t resume: $repo $n is reserved for the Codex desktop app; use t session open $repo $n --app"
          return 1
        fi
        live_seen[app/${repo}-$n]=1
        [[ -n $live_flag ]] || { (( hidden_live++ )); continue; }
        cands+=(9999999999$'\t'"$repo"$'\t'"$n"$'\t'-$'\t'"$wt"$'\t'"◉ desktop here"$'\t'"(Codex desktop workspace)"$'\t'app:here$'\t'-$'\t'-$'\t'codex)
        continue
      fi
      busy=${live[${wt:A}]:-}
      if [[ -n $busy ]]; then
        if [[ -n $slot ]]; then
          echo "Slot $n is live ($busy) — attaching (resume only revives dead slots)."
          _t_dev "$repo" "$n"
          return
        fi
        # Scan: a live local slot is HIDDEN by default (counted for the hint
        # below — the list is "what can I revive", and live slots are `t open`'s
        # business); -l/--live shows it as a labeled row (pick → attach). Sort
        # key (field 1, stripped after the global sort below): a live session is
        # "now", so the max sentinel pins it above every dead transcript.
        live_seen[here/${busy#dev-}]=1
        [[ -n $live_flag ]] || { (( hidden_live++ )); continue; }
        cands+=(9999999999$'\t'"$repo"$'\t'"$n"$'\t'-$'\t'"$wt"$'\t'"● active"$'\t'"${local_sum[${busy#dev-}]:-(live session)}"$'\t'here$'\t'-$'\t'-$'\t'"${local_agent[${busy#dev-}]:-$(_dev_agent_of_session "$busy")}")
        continue
      fi
      # Remote-live: same treatment as local live (see the scan note above).
      rhost=${remote_live_host[$n]:-}
      if [[ -n $rhost ]]; then
        if [[ ${remote_live_state[$n]} == app ]]; then
          if [[ -n $slot ]]; then
            print -u2 -- "t resume: $repo $n is reserved for the Codex desktop app on $rhost; close and release it there before resuming"
            return 1
          fi
          live_seen[app/$rhost/${remote_live_alias[$n]}-$n]=1
          [[ -n $live_flag ]] || { (( hidden_live++ )); continue; }
          cands+=(9999999999$'\t'"$repo"$'\t'"$n"$'\t'-$'\t'"$wt"$'\t'"◉ desktop on $rhost"$'\t'"${remote_live_sum[$n]:-(Codex desktop workspace)}"$'\t'"app:$rhost"$'\t'"${remote_live_alias[$n]}"$'\t'-$'\t'codex)
          continue
        fi
        if [[ -n $slot ]]; then
          echo "Slot $n is live on $rhost — attaching (resume only revives dead slots)."
          _dev_remote_attach "$rhost"$'\t'"${remote_live_alias[$n]}"$'\t'"$n" ""
          return
        fi
        live_seen[$rhost/${remote_live_alias[$n]}-$n]=1
        [[ -n $live_flag ]] || { (( hidden_live++ )); continue; }
        cands+=(9999999999$'\t'"$repo"$'\t'"$n"$'\t'-$'\t'"$wt"$'\t'"● on $rhost"$'\t'"${remote_live_sum[$n]:-(live session)}"$'\t'"$rhost"$'\t'"${remote_live_alias[$n]}"$'\t'-$'\t'"${remote_live_agent[$n]:-claude}")
        continue
      fi
      # Name-only collision: a dev-<alias>-${n} tmux session (any alias keying
      # this repo's dir — see _t_dev / _t_pop) exists but is rooted elsewhere
      # (not this slot's worktree — an old alias, an opt-out shared tree, or a
      # session opened straight in the canonical repo dir, which `t ls` shows
      # and this scan matched by PATH so it missed). It is still a LIVE session
      # occupying the slot's tmux name, so it gets the same treatment as one
      # rooted in the worktree: a labeled row that ATTACHES on pick (_t_dev
      # canonicalizes sibling aliases, so it lands on this exact session).
      # It was previously skipped in scan mode and refused on explicit ask —
      # which made a live slot invisible in the very list that is supposed to
      # show what is running. Resuming a DEAD conversation into that slot is
      # still impossible (`tmux new-session -s dev-${repo}-${n}` would collide
      # on the name), hence the live row rather than the slot's transcripts.
      _stale=
      for _ok in ${(k)DEV_REPOS}; do
        [[ ${DEV_REPOS[$_ok]} != $_rdir ]] && continue
        tmux has-session -t "=dev-${_ok}-${n}:" 2>/dev/null && { _stale="dev-${_ok}-${n}"; break; }
      done
      if [[ -n $_stale ]]; then
        stale_path=$(tmux display-message -p -t "=$_stale:" '#{session_path}' 2>/dev/null)
        if [[ -n $slot ]]; then
          echo "Slot $n is live ($_stale${stale_path:+ in $stale_path}) — attaching (resume only revives dead slots)."
          _t_dev "$repo" "$n"
          return
        fi
        live_seen[here/${_stale#dev-}]=1
        [[ -n $live_flag ]] || { (( hidden_live++ )); continue; }
        cands+=(9999999999$'\t'"$repo"$'\t'"$n"$'\t'-$'\t'"${stale_path:-$wt}"$'\t'"● active"$'\t'"${local_sum[${_stale#dev-}]:-(live session)}"$'\t'here$'\t'-$'\t'-$'\t'"${local_agent[${_stale#dev-}]:-$(_dev_agent_of_session "$_stale")}")
        continue
      fi
      # every conversation either agent recorded in this worktree: claude's project
      # dir by mtime, codex's threads from its sqlite index (rollouts are date-keyed)
      tx=( "$HOME/.claude/projects/${wt//[^A-Za-z0-9]/-}"/*.jsonl(Nom)
           ${(f)"$(_dev_agent_transcripts_for_cwd codex "$wt")"} )
      for txf in "${(@)tx}"; do
        # Live by ID (see live_sid above): never a second resume — a live row
        # aimed at the owner, collected apart so the dedup below can drop it when
        # the owner's own slot already produced its row. Checked before --days:
        # a running conversation is current whatever its file's mtime says.
        sid=$(_dev_transcript_sid "$txf")
        if [[ -n ${live_sid[$sid]:-} ]]; then
          sidlive+=("$repo"$'\t'"$sid"$'\t'"$wt"$'\t'"${live_sid[$sid]}")
          continue
        fi
        ep=$(zstat +mtime "$txf" 2>/dev/null || echo 0)   # sort key + --days gate
        # Effective recency = max(transcript mtime, opened stamp): resuming
        # writes nothing to the .jsonl, so without the stamp a just-reopened
        # conversation sorts — and --days-gates — as days old. Stamp newer →
        # the row is dated by the stamp and marked ↻.
        reopened=; opf="$opdir/$(_dev_transcript_sid "$txf")"
        if [[ -f $opf ]]; then
          opep=$(zstat +mtime "$opf" 2>/dev/null || echo 0)
          (( opep > ep )) && { ep=$opep; reopened=1; }
        fi
        if (( cutoff && ep < cutoff )); then
          (( skipped++ )); continue        # outside the --days window
        fi
        # Nothing is READ from the transcript here: the title and the PR URL both
        # need the file's contents, and doing that inline meant a python3 fork
        # plus a whole-file `grep -ao` per candidate — 135MB read twice for ff's
        # 43 in-window conversations. Collect the survivors instead and read them
        # all in ONE batched, incrementally-cached pass below. Every field is
        # non-empty (reopened is 1/0, never blank) so the `ps:\t:` split back
        # cannot collapse a column.
        pending+=("$ep"$'\t'"$repo"$'\t'"$n"$'\t'"$wt"$'\t'"$txf"$'\t'"${reopened:-0}")
      done
    done
  done

  # Conversations live under ANOTHER slot's session (or a foreground one). One row
  # per owner — skipped when the owner's own slot row was already counted/emitted.
  # Hidden like any live row unless -l, or unless a slot was named: `t resume ff 13`
  # must say where 13's conversation went rather than "nothing to resume".
  local _sl _sloc _salias _sslot _ssum _sag _skey _slabel
  local -a _slf
  for _sl in "${(@)sidlive}"; do
    _slf=("${(@ps:\t:)_sl}")
    _sloc=$_slf[4]; _salias=$_slf[5]; _sslot=$_slf[6]; _ssum=$_slf[7]; _sag=${_slf[8]:-claude}
    if [[ $_sloc == fg:* ]]; then _skey="$_sloc/$_salias"; else _skey="$_sloc/$_salias-$_sslot"; fi
    [[ -n ${live_seen[$_skey]:-} ]] && continue
    live_seen[$_skey]=1
    if [[ -z $live_flag && -z $slot ]]; then (( hidden_live++ )); continue; fi
    case $_sloc in
      here)  _slabel="● in $_salias-$_sslot" ;;
      fg:*)  _slabel="● fg ${${_sloc#fg:}/here/here}" ;;
      *)     _slabel="● $_salias-$_sslot on $_sloc" ;;
    esac
    cands+=(9999999999$'\t'"$_slf[1]"$'\t'"${${_sslot:#-}:-$slot}"$'\t'"$_slf[2]"$'\t'"$_slf[3]"$'\t'"$_slabel"$'\t'"${_ssum:-(live session)}"$'\t'"$_sloc"$'\t'"$_salias"$'\t'-$'\t'"$_sag")
  done

  # ONE batched metadata read for every surviving candidate (title + last PR URL),
  # warm-cached per transcript and incremental for the ones still being appended to.
  if (( $#pending )); then
    for _p in "${(@)pending}"; do _mpaths+=("${${(@ps:\t:)_p}[5]}"); done
    _mrows=("${(@f)$(_transcript_meta_batch "${(@)_mpaths}")}")
    for _mr in "${(@)_mrows}"; do
      [[ -n $_mr ]] || continue
      # Peeled with parameter expansion, NOT a split: title and pr are both
      # allowed to be empty and tab is an IFS-whitespace char, so `read`/`(ps)`
      # would collapse the blank column and slide the fields (the same trap the
      # `-` sentinels in _dev_session_rows exist for).
      _mrest=${_mr#*$'\t'}
      meta_title[${_mr%%$'\t'*}]=${_mrest%%$'\t'*}
      meta_pr[${_mr%%$'\t'*}]=${_mrest#*$'\t'}
    done
  fi
  for _p in "${(@)pending}"; do
    _mf=("${(@ps:\t:)_p}")
    ep=$_mf[1]; repo=$_mf[2]; n=$_mf[3]; wt=$_mf[4]; txf=$_mf[5]
    [[ $_mf[6] == 1 ]] && reopened=1 || reopened=
    title=${meta_title[$txf]:-}
    [[ -n $title ]] || continue        # conversationless stub — nothing to resume
    # ep already reflects the reopened stamp (ep=opep above), so one
    # strftime covers both branches; ↻ marks the stamp-dated rows.
    when=; (( ep )) && when=$(strftime '%b %d %H:%M' "$ep" 2>/dev/null)
    [[ -n $reopened && -n $when ]] && when+=" ↻"
    # PR-in-session tag: the LAST github.com …/pull/N URL in the transcript
    # (a `gh pr create` lands its URL in the tool output) names the
    # session's PR; append " · #N <state>" to the title. State is read from
    # the pr/ cache ONLY — never a blocking gh call — with the freshness policy in
    # _pr_state_stale and the (detached, per-repo batched) refetch fired once for
    # the whole scan below, so a new state shows on the next invocation.
    _pr_state_tag "${meta_pr[$txf]:-}"; title+=$REPLY
    # Origin: which machine the conversation LAST RAN on — the <sid>.origin
    # stamp claude-stamp-tmux writes next to the transcript (syncs with it).
    # Blank for this machine / unstamped (pre-feature) transcripts; a raw
    # hostname is translated to its $REMOTE_HOSTS key when the cache knows it.
    org=; orgf="${txf%.jsonl}.origin"
    if [[ -f $orgf ]]; then
      org=$(<$orgf)
      if [[ $org == $selfhost ]]; then org=
      elif [[ -n ${host_alias[$org]:-} ]]; then org=${host_alias[$org]}
      fi
    fi
    # field 4 is the real sid (the trailing uuid of a rollout filename); the agent
    # (field 11 here, 10 once the sort key is stripped) gets its own display column
    # AND picks the binary a pick spawns
    _ag=$(_dev_transcript_agent "$txf")
    cands+=("$ep"$'\t'"$repo"$'\t'"$n"$'\t'"$(_dev_transcript_sid "$txf")"$'\t'"$wt"$'\t'"$when"$'\t'"$title"$'\t'-$'\t'-$'\t'"${org[1,10]}"$'\t'"$_ag")
  done
  # ONE detached refresh for the whole scan, instead of a `gh pr view` child per
  # stale row: _pr_state_refresh batches the refs by repo, so a scan that turned up
  # six unknown PRs in one repo costs a single gh call rather than six. Still
  # strictly fire-and-forget — the render above already used the cache and is done.
  _pr_state_flush

  (( skipped )) && echo "(${skipped} older conversation(s) outside the last ${days}d hidden — t session resume --days all shows them)" >&2
  (( hidden_live )) && echo "(${hidden_live} live slot(s) hidden — t session resume --live lists them; t session open attaches one)" >&2

  # ONE global newest-first order by session time (the leading epoch field),
  # never grouped by repo — the loop above emits repo-by-repo, so without this
  # all-repos mode read as blocks per repo. Live rows carry the max sentinel
  # and pin to the top. (On) = descending numeric on the leading field, which
  # is then stripped so the positional fields below stay 1-9.
  cands=("${(@On)cands}")
  cands=("${(@)cands#*$'\t'}")

  if (( ! $#cands )); then
    if [[ -n $slot ]]; then
      echo "No saved conversation for $repo slot $slot (nothing recorded in its worktree)." >&2
      echo "Fresh session: t session open $repo $slot · full picker: t session push -p" >&2
    elif [[ -n $all_mode ]]; then
      echo "Nothing to resume — no dead slot in any worktree repo has a saved conversation." >&2
      echo "Fresh session: t session open <repo> · full picker: t session push -p" >&2
    else
      echo "Nothing to resume for $repo — no dead slot has a saved conversation." >&2
      echo "Fresh session: t session open $repo · full picker: t session push -p" >&2
    fi
    return 1
  fi

  # Picker rows: repo(1) slot(2) sid(3) wt(4) when(5) title(6) loc(7) alias(8)
  # origin(9) agent(10) — loc/alias are `-` for a dead (resumable) row, the `here`
  # sentinel (displayed "● active") for a live local slot, or the host + remote
  # alias for a slot live on a $REMOTE_HOSTS
  # host (pick → attach in place, never a second owner); origin is the machine a
  # dead conversation LAST RAN on (`-`/empty = here or unstamped — live rows name
  # their host in the ● label instead); agent is the row's claude/codex — what a
  # dead pick spawns, and what a live slot is running (from the live scan, else
  # the session's own stamp). EVERY row carries all ten, sentinelled, so the
  # positional reads below never slide. One ALIGNED display column (11) is
  # appended here — fzf renders raw \t fields at literal tab stops (nothing
  # lines up), so both fzf and the no-fzf listing show the same pre-padded
  # gh-style row: [repo]  slot  agent  [origin]  date|●-where  title. The agent
  # column is `<icon> <name>` on every row — a ⬡ in the date cell was too easy to
  # miss ("need to more clearly indicate which is claude, codex"), and the icon
  # is the same one `t ls` shows — under the full legend (_dev_agent_legend: every
  # supported tool, always) on the fzf header and the listing's footer. fzf
  # renders it as --with-nth=-1 — the LAST field, the same rule as the listing's
  # `${c##*$'\t'}` — never a fixed index: when the agent field landed as column
  # 10, a hard-coded --with-nth=10 showed every dead row as the word `claude`
  # (and matched queries against nothing else) — "t resume shows no session
  # info", 2026-09-14. The
  # origin column only appears when some row has one (a single-machine setup
  # never sees it); all-repos mode adds the repo column. Both pad to the widest
  # value in this candidate set.
  local pick c fprompt; local -a f
  local rw=0 ow=0 aw=0 i=1
  if [[ -n $all_mode ]]; then
    fprompt="resume (${days}d)> "
    for c in "${(@)cands}"; do f=("${(@ps:\t:)c}"); (( ${#f[1]} > rw )) && rw=${#f[1]}; done
  else
    fprompt="resume $repo (${days}d)> "
  fi
  for c in "${(@)cands}"; do
    f=("${(@ps:\t:)c}")
    [[ ${f[9]:-} != - && -n ${f[9]:-} ]] && (( ${#f[9]} > ow )) && ow=${#f[9]}
    (( ${#f[10]} + 2 > aw )) && aw=$(( ${#f[10]} + 2 ))   # `<icon> <name>`
  done
  local legend; legend=$(_dev_agent_legend)
  for c in "${(@)cands}"; do
    f=("${(@ps:\t:)c}")
    org=${f[9]:-}; [[ $org == - ]] && org=
    agent="${_DEV_AGENT_GLYPH[${f[10]:-claude}]:-?} ${f[10]:-claude}"
    if [[ -n $all_mode ]]; then
      if (( ow )); then
        cands[$i]+=$'\t'"$(printf '%-*s  %2s  %-*s  %-*s  %-14s  %s' "$rw" "$f[1]" "$f[2]" "$aw" "$agent" "$ow" "$org" "$f[5]" "$f[6]")"
      else
        cands[$i]+=$'\t'"$(printf '%-*s  %2s  %-*s  %-14s  %s' "$rw" "$f[1]" "$f[2]" "$aw" "$agent" "$f[5]" "$f[6]")"
      fi
    else
      if (( ow )); then
        cands[$i]+=$'\t'"$(printf '%2s  %-*s  %-*s  %-14s  %s' "$f[2]" "$aw" "$agent" "$ow" "$org" "$f[5]" "$f[6]")"
      else
        cands[$i]+=$'\t'"$(printf '%2s  %-*s  %-14s  %s' "$f[2]" "$aw" "$agent" "$f[5]" "$f[6]")"
      fi
    fi
    (( i++ ))
  done
  # Sole candidate auto-picks — but only when it is DEAD (loc == -, resumed in
  # place here) or we have a TTY. A sole LIVE row without a TTY would hit
  # _t_dev / _dev_remote_attach, both of which need a terminal to attach; fall
  # through to the listing branch so the message matches the multi-candidate
  # no-fzf case instead of failing hard from inside the attach helper.
  if (( $#cands == 1 )) && f=("${(@ps:\t:)cands[1]}") && [[ $f[7] == - || -t 1 ]]; then
    pick=${cands[1]}
  elif [[ -t 0 && -t 1 ]] && command -v fzf >/dev/null; then
    # Space toggles a ✓ mark (bound to toggle+down — the same action as fzf's
    # default tab binding, so both work and marking a batch is spacebar-taps
    # down the list). The cost: a literal space no longer types into the filter
    # query — acceptable because fzf's fuzzy match crosses word gaps ("fixbug"
    # still matches "fix bug").
    pick=$(print -rl -- "${(@)cands}" | _t_fzf --multi --marker='✓' --bind 'space:toggle+down' \
      --delimiter=$'\t' --with-nth=-1 --no-hscroll \
      --header="${legend}   ·   space marks ✓ — every mark revives, first attaches" --prompt="$fprompt") || return 1
    [[ -n $pick ]] || return 1
  elif [[ -n $slot ]] && { pick=; for c in "${(@)cands}"; do
         f=("${(@ps:\t:)c}"); [[ $f[7] == - ]] && { pick=$c; break; }; done; [[ -n $pick ]]; }; then
    # Explicit slot but no TTY/fzf to pick with: the newest DEAD conversation IS
    # the documented contract ("revive the slot's last conversation") — take it
    # (never a live row: attaching needs the terminal we lack), and list the rest
    # so a specific pick is one fzf-equipped call away.
    echo "Slot $slot has $#cands saved conversations — resuming the newest (run from a terminal to pick):" >&2
    for c in "${(@)cands}"; do echo "  ${c##*$'\t'}" >&2; done
    echo "  $legend" >&2
  else
    if [[ -n $all_mode ]]; then
      echo "Several resumable conversations — name one (t session resume <repo> <slot>):" >&2
    else
      echo "Several resumable conversations for $repo — name one (t session resume $repo <slot>):" >&2
    fi
    for c in "${(@)cands}"; do echo "  ${c##*$'\t'}" >&2; done
    echo "  $legend" >&2
    return 1
  fi
  # Multi-pick (only the fzf branch can produce one — $pick then holds one row
  # per line, in LIST order, so the first row is the newest / a pinned live one).
  # Revive every marked dead row detached, then attach the FIRST revived slot —
  # a resume ends inside a session, same as a single pick; the rest open in
  # their own terminal tabs (attach-hint fallback). Live marks are already open
  # (one-live-owner: never a second `claude -r`) and a second conversation
  # marked for a slot an earlier mark just took can't have the tmux name —
  # both become hints, not errors, so one stray mark never aborts the batch.
  local -a picks; picks=("${(@f)pick}")
  if (( $#picks > 1 )); then
    if [[ -n $no_tmux ]]; then
      echo "t resume --fg resumes ONE conversation inline in this terminal — mark a single row (or drop --fg)." >&2
      return 1
    fi
    local -A mp_taken
    local mp_first_repo= mp_first_slot= mp_first_session= mp_cwd mp_session mp_loc mp_pair mp_agent
    local -a mp_rest mp_unopened
    for pick in "${(@)picks}"; do
      f=("${(@ps:\t:)pick}")
      repo=$f[1]; slot=$f[2]; sid=$f[3]; wt=$f[4]; mp_loc="${f[7]:-}"; mp_agent="${f[10]:-claude}"
      if [[ $mp_loc == fg:* ]]; then
        echo "· $repo: that conversation is live as foreground ${f[8]} — attach with: t session open ${f[8]}"
        continue
      elif [[ $mp_loc == here ]]; then
        [[ -n ${f[8]:-} && $f[8] != - ]] && repo=$f[8]
        echo "· $repo $slot is already live here — attach with: t session open $repo $slot"
        continue
      elif [[ -n $mp_loc && $mp_loc != - ]]; then
        echo "· $repo $slot is live on $mp_loc — attach with: t session open $repo $slot"
        continue
      fi
      if [[ -n ${mp_taken[$repo/$slot]:-} ]]; then
        echo "· ${sid:0:8}: $repo slot $slot already revived by an earlier mark — skipped (t session resume $repo $slot swaps it)." >&2
        continue
      fi
      if [[ -n $on_host ]]; then
        # every mark lands DETACHED on the host; the first is attached below
        mp_taken[$repo/$slot]=1
        echo "Resuming ${sid:0:8} ($repo $slot) on $on_host"
        mp_session=$(_t_resume_on_host "$on_host" "$sid" "$wt" "$mp_agent" detach) || {
          echo "· $repo $slot: landing on $on_host failed — ${mp_session:-no output}" >&2; continue; }
        if [[ -z $mp_first_repo ]]; then
          mp_first_repo=$repo; mp_first_slot=$slot; mp_first_session=$mp_session
        else
          mp_rest+=("${${mp_session#dev-}%-*} ${mp_session##*-}")
        fi
        continue
      fi
      if ! mp_cwd=$(_dev_ensure_session_cwd "$wt"); then
        echo "· $repo $slot: couldn't rebuild its worktree ($wt) — skipped." >&2
        continue
      fi
      mp_taken[$repo/$slot]=1
      mp_session="dev-${repo}-${slot}"
      echo "Resuming ${sid:0:8} in $mp_session ($mp_cwd)"
      _dev_resume_session "$mp_session" "$mp_cwd" "$sid" "$mp_agent"
      if [[ -z $mp_first_repo ]]; then
        mp_first_repo=$repo; mp_first_slot=$slot
      else
        mp_rest+=("$repo $slot")
      fi
    done
    if [[ -z $mp_first_repo ]]; then
      echo "Nothing revived — every marked row was already live or unrecoverable." >&2
      return 1
    fi
    if [[ -n $on_host ]]; then
      # Remote landings: the rest are one `t open` away (it auto-finds the host);
      # the first attaches in place over ssh when there is a terminal to do it in.
      (( $#mp_rest )) && echo "Also landed on $on_host — attach with: ${(j: · :)${mp_rest/#/t session open }}"
      if [[ -t 0 && -t 1 ]]; then
        ssh -t "${REMOTE_HOSTS[$on_host]:-$on_host}" "zsh -lic ${(q):-tmux attach -t $mp_first_session}"
      else
        echo "Attach with: t session open ${${mp_first_session#dev-}%-*} ${mp_first_session##*-}"
      fi
      return
    fi
    # Every revived slot beyond the first opens in its OWN terminal tab, each
    # running the ordinary `t open` attach (_dev_open_tab); the first attaches
    # HERE as usual, so 4 marks end as 4 visible sessions. Tabs impossible —
    # ssh, unrecognized terminal, osascript refused — falls back to attach
    # hints, with a pointer at the two fixable causes when the GUI is local.
    # Inside tmux / no TTY the first cannot attach here either, so it gets a
    # tab too.
    for mp_pair in "${(@)mp_rest}"; do
      _dev_open_tab "t open $mp_pair" || mp_unopened+=("t open $mp_pair")
    done
    if (( $#mp_unopened )); then
      echo "Also revived — attach with: ${(j: · :)mp_unopened}"
      [[ -z ${SSH_CONNECTION:-} ]] \
        && echo "(tabs need a local Terminal.app/iTerm2 GUI; Terminal.app also needs Accessibility — System Settings → Privacy & Security)" >&2
    fi
    if [[ -z $TMUX && -t 0 && -t 1 ]]; then
      tmux attach-session -t "=dev-${mp_first_repo}-${mp_first_slot}:"
    else
      _dev_open_tab "t open $mp_first_repo $mp_first_slot" \
        || echo "Attach with: t session open $mp_first_repo $mp_first_slot"
    fi
    return
  fi
  pick=$picks[1]
  f=("${(@ps:\t:)pick}")
  repo=$f[1]; slot=$f[2]; sid=$f[3]; wt=$f[4]; agent="${f[10]:-claude}"
  local loc="${f[7]:-}" lalias="${f[8]:-}"

  # A LIVE row was picked: attach in place (local or on its host) — resuming it
  # here would mint a second owner of the session id (see the scan note above).
  if [[ $loc == fg:* ]]; then
    echo "That conversation is live as foreground $lalias — attaching."
    _dev_open_fg "$lalias"
    return
  elif [[ $loc == app:* ]]; then
    print -u2 -- "t resume: slot $slot is reserved for the Codex desktop app on ${loc#app:}; close and release it there before resuming"
    return 1
  elif [[ $loc == here ]]; then
    [[ -n $lalias && $lalias != - ]] && repo=$lalias   # owner under another alias/slot
    echo "Slot $slot is live locally — attaching."
    _t_dev "$repo" "$slot"
    return
  elif [[ -n $loc && $loc != - ]]; then
    _dev_remote_attach "$loc"$'\t'"$lalias"$'\t'"$slot" ""
    return
  fi

  # --host: land it THERE (see _t_resume_on_host) — the local worktree is not
  # rebuilt, the host materializes its own from the branch.
  if [[ -n $on_host ]]; then
    local hmode=detach
    if [[ -n $no_tmux ]]; then hmode=fg
    elif [[ -t 0 && -t 1 ]]; then hmode=attach
    fi
    echo "Resuming ${sid:0:8} ($repo $slot) on $on_host"
    # attach/fg own the terminal through ssh -t — never capture their stdout
    [[ $hmode == detach ]] || { _t_resume_on_host "$on_host" "$sid" "$wt" "$agent" "$hmode"; return; }
    local landed; landed=$(_t_resume_on_host "$on_host" "$sid" "$wt" "$agent" detach) || {
      echo "t resume --host: landing on $on_host failed: ${landed:-no output}" >&2
      return 1
    }
    echo "Landed in $landed on $on_host — attach with: t session open ${${landed#dev-}%-*} ${landed##*-}"
    return 0
  fi

  # The recorded cwd is the transcript's lookup key: rebuild the worktree at the
  # IDENTICAL path when it was reaped/never existed here (branch + transcript are
  # durable; the dir is not).
  local cwd
  cwd=$(_dev_ensure_session_cwd "$wt") || {
    echo "Couldn't rebuild the worktree for $repo slot $slot ($wt) — is its dev/${${DEV_REPOS[$repo]}:t}-${slot} branch gone?" >&2
    return 1
  }

  if [[ -n $no_tmux ]]; then
    if [[ -n $TMUX && -n $CLAUDE_CODE_SESSION_ID ]]; then
      echo "Run t session resume --fg from a plain shell — it resumes the session in the foreground." >&2
      return 1
    fi
    cd "$cwd" || return 1
    if [[ $agent == codex ]]; then codex resume "$sid"; else claude -r "$sid"; fi
    return
  fi

  local session="dev-${repo}-${slot}"
  echo "Resuming ${sid:0:8} in $session ($cwd)"
  _dev_resume_session "$session" "$cwd" "$sid" "$agent"
  if [[ -z $TMUX && -t 0 && -t 1 ]]; then
    tmux attach-session -t "=$session:"
  else
    echo "Attach with: t session open $repo $slot"
  fi
}

# _t_find — the `t find` verb: find the Claude session working on something you describe.
# Semantic search, not grep: a keyword pass over titles + your prompts gathers candidates
# (padded with recent sessions so divergent phrasing still gets a shot), Sonnet ranks the
# genuinely-relevant ones (falling back to keyword order when the claude CLI is unreachable),
# and your fzf pick foreground-resumes (the same landing as `t pop`). Searches every project.
# User-facing help lives in bin/t (`t find -h`); the t() shim routes -h there.
_t_find() {
  local keyword=
  [[ "$1" == "-k" || "$1" == "--keyword" ]] && { keyword=1; shift; }
  if [[ -n $TMUX && -n $CLAUDE_CODE_SESSION_ID ]]; then
    echo "Run tfind from a plain shell — it resumes a session in the foreground." >&2
    return 1
  fi
  local row
  # No query → browse newest-first (matches `t find -h`); Sonnet has nothing to rank
  # without a query, so route through the keyword/browse picker even without -k.
  if [[ -n $keyword || -z "$*" ]]; then
    row=$(_claude_sessions_fzf "" "$*") || return 1       # offline keyword rank / browse
  else
    row=$(_claude_sessions_semantic "$*") || return 1     # Sonnet-reranked
  fi
  [[ -n $row ]] || return 1
  local sid cwd
  sid=${row%%$'\t'*}
  cwd=${${row#*$'\t'}%%$'\t'*}
  # Resume-through-sync: a picked session may be a per-session worktree absent here (created
  # on another machine, or reaped after merge). Rebuild it from its branch rather than failing.
  # _dev_ensure_session_cwd prints nothing on failure, so capture into a temp and only overwrite
  # $cwd on success — else the error message below would have lost the session path (matches the
  # same fix in _t_push).
  local resolved
  resolved=$(_dev_ensure_session_cwd "$cwd") \
    || { echo "Session's directory no longer exists and could not be rebuilt: $cwd" >&2; return 1; }
  cwd=$resolved
  echo "Resuming claude -r ${sid[1,8]}… in $cwd"
  cd "$cwd" && claude -r "$sid"
}

# _claude_sessions_semantic <query> — fzf-pick a session by what it's ABOUT.
# Echoes the chosen "<sid>\t<cwd>\t<display>" row (same contract as
# _claude_sessions_fzf, so tfind handles either). Pipeline: a keyword pass builds
# a recall-oriented candidate pool, Sonnet (via `claude -p`, run as a subprocess
# so it bypasses the zsh claude wrapper) reranks them with reasons, and the
# ranked rows feed fzf. Any failure — no CLI, timeout, unparseable reply — falls
# back to keyword order so the picker always populates.
_claude_sessions_semantic() {
  command -v fzf     >/dev/null 2>&1 || { echo "fzf not installed (brew install fzf)" >&2; return 1; }
  command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; return 1; }
  local projects="$HOME/.claude/projects"
  [[ -d $projects ]] || { echo "No Claude sessions at $projects" >&2; return 1; }
  local query="$1"
  echo "↻ Asking Sonnet which session matches \"$query\"…  (-k to skip)" >&2

  python3 - "$projects" "$query" <<'PY' | _t_fzf --delimiter=$'\t' --with-nth=3 --no-hscroll \
        --prompt="resume claude (sonnet: $query) > " --height=60% --reverse
import json, os, sys, glob, datetime, subprocess, re, time
root, query = sys.argv[1], sys.argv[2]
STOP = {'of','the','a','an','to','on','in','for','and','is','it','with','at'}
qterms = [t for t in query.lower().split() if t not in STOP]
# Effective recency = max(transcript mtime, last-opened stamp) — same signal as
# _claude_session_rows, so the recent-session padding + display reflect resumes too.
cache_root = os.path.join(os.environ.get('XDG_CACHE_HOME')
                          or os.path.expanduser('~/.cache'), 'claude-sessions')
opened_dir = os.path.join(cache_root, 'opened')
def _opened(sid):
    try: return os.path.getmtime(os.path.join(opened_dir, sid))
    except OSError: return 0
# PR indicator — the compact twin of _claude_session_rows' (see its comments for
# the policy: cache-only reads, detached gh refresh, terminal states forever).
pr_dir = os.path.join(cache_root, 'pr')
pr_re = re.compile(r'github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)')
stale = []
def pr_state(ref):
    st, fresh = '', False
    try:
        fp = os.path.join(pr_dir, '#'.join(ref))
        st = open(fp).read().strip()
        fresh = time.time() - os.path.getmtime(fp) < 300
    except OSError: pass
    if st not in ('MERGED', 'CLOSED') and not fresh:
        stale.append(ref)
    return st if st in ('MERGED', 'CLOSED', 'OPEN') else ''
def pr_refresh():
    if stale:
        try: os.makedirs(pr_dir, exist_ok=True)
        except OSError: return
    for owner, repo, num in stale[:8]:
        fp = os.path.join(pr_dir, f'{owner}#{repo}#{num}')
        cmd = (f'st=$(gh pr view "https://github.com/{owner}/{repo}/pull/{num}"'
               f' --json state --jq .state 2>/dev/null) || st="?"; '
               f'printf %s "$st" > "{fp}.$$.tmp" && mv "{fp}.$$.tmp" "{fp}"')
        try:
            subprocess.Popen(['/bin/sh', '-c', cmd], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            break

# ── Scan: title + your first few prompts per session (the "about" signal). ──
sessions = []
for f in glob.glob(os.path.join(root, '*', '*.jsonl')):
    sid = os.path.basename(f)[:-6]
    cwd = title = ctitle = pr = None
    prompts = []
    try:
        for line in open(f, errors='ignore'):
            if cwd is None and '"cwd"' in line:
                try: cwd = json.loads(line).get('cwd')
                except ValueError: pass
            if '/pull/' in line:                        # last PR URL wins
                m = pr_re.findall(line)
                if m: pr = m[-1]
            if '"custom-title"' in line:
                try:
                    t = json.loads(line).get('customTitle')
                    if t: ctitle = t
                except ValueError: pass
            if '"ai-title"' in line:
                try:
                    t = json.loads(line).get('aiTitle')
                    if t: title = t
                except ValueError: pass
            if len(prompts) < 6 and '"type":"user"' in line:
                try:
                    c = json.loads(line).get('message', {}).get('content')
                    txt = c if isinstance(c, str) else (
                        ' '.join(x.get('text', '') for x in c if isinstance(x, dict))
                        if isinstance(c, list) else '')
                    txt = txt.strip()
                    if txt and not txt.startswith('<'): prompts.append(txt)
                except ValueError: pass
    except OSError:
        continue
    head = ctitle or title or (prompts[0] if prompts else '') or '(no message)'
    mtime = os.path.getmtime(f)
    opened = _opened(sid)
    prs = ''
    if pr:
        st = pr_state(pr)
        prs = f" · #{pr[2]}{' ' + st.lower() if st else ''}"
    sessions.append({'sid': sid, 'cwd': cwd or '?', 'mtime': max(mtime, opened),
                     'reopened': opened > mtime, 'head': head, 'prompts': prompts,
                     'pr': prs})
pr_refresh()

# ── Retrieve: keyword pass for recall. Pad thin matches with recent sessions
# so a query whose words diverge from the transcript still reaches the model. ──
def kw(s):
    head = s['head'].lower(); body = ' '.join(s['prompts']).lower()
    return sum(body.count(t) + 4 * head.count(t) for t in qterms)
for s in sessions: s['kw'] = kw(s)
cands = sorted((s for s in sessions if s['kw'] > 0),
               key=lambda s: (s['kw'], s['mtime']), reverse=True)[:30]
if len(cands) < 8:
    have = {s['sid'] for s in cands}
    for s in sorted(sessions, key=lambda s: s['mtime'], reverse=True):
        if s['sid'] not in have: cands.append(s)
        if len(cands) >= 20: break

def emit(rows):                                       # rows: (session, reason)
    for s, why in rows:
        when = datetime.datetime.fromtimestamp(s['mtime']).strftime('%m-%d %H:%M')
        short = os.path.basename(s['cwd']) if s['cwd'] != '?' else '?'
        mark = '↻ ' if s['reopened'] else '  '
        disp = f"{when} {mark}{short:<16}  {' '.join(s['head'].split())[:60]}{s['pr']}"
        if why: disp += f"   ⟵ {why}"
        print(f"{s['sid']}\t{s['cwd']}\t{disp}")

if not cands:
    sys.exit(0)

# ── Rerank: hand Sonnet the candidates' context + the query, get a ranking. ──
def digest(i, s):
    when = datetime.datetime.fromtimestamp(s['mtime']).strftime('%Y-%m-%d')
    ps = ' / '.join(p[:200] for p in s['prompts'][:4])
    return f"[{i}] ({when}, {os.path.basename(s['cwd'])}) {s['head'][:120]}\n    {ps[:600]}"

prompt = (
    f'I am looking for a past coding session — the one working on:\n"{query}"\n\n'
    f'Candidate sessions, each with its title and my opening prompts:\n\n'
    + '\n'.join(digest(i, s) for i, s in enumerate(cands))
    + '\n\nReturn ONLY a JSON array (no prose, no code fence) of the genuinely '
      'relevant sessions, best match first, at most 8, each {"i": <index>, '
      '"why": "<reason in 8 words or fewer>"}. If none fit, return [].')

order = None
try:
    p = subprocess.run(['claude', '-p', '--model', 'sonnet', '--output-format', 'json'],
                       input=prompt, capture_output=True, text=True, timeout=60)
    res = json.loads(p.stdout).get('result', '')
    m = re.search(r'\[.*\]', res, re.S)               # tolerate stray prose/fences
    if m: order = json.loads(m.group(0))
except Exception:
    order = None

if order:                                             # Sonnet's ranking, with reasons
    rows = []
    for o in order:
        try: i = int(o['i'])
        except (KeyError, ValueError, TypeError): continue
        if 0 <= i < len(cands):
            rows.append((cands[i], str(o.get('why', '')).strip()))
    if rows:
        emit(rows); sys.exit(0)
emit([(s, '') for s in cands])                        # fallback: keyword order
PY
}

# _claude_session_rows [cwd] [query] — scan saved Claude transcripts, printing
# one "<session-id>\t<cwd>\t<display>" row per session to stdout (no fzf — see
# _claude_sessions_fzf for the picker). Split out so the rows can be generated on
# a *remote* host over ssh and fzf'd locally (tbeam --from): fzf can't be driven
# interactively through a captured ssh pipe, but a row dump travels fine.
# Newest-first by transcript mtime; the JSONL is parsed once in
# python for each file's real cwd (the `cwd` field, not the lossy folder name),
# its title — preferring a /rename `customTitle`, then the generated `aiTitle`,
# then the first human message. With a <cwd> arg, only sessions
# whose cwd is that dir or below are shown; omit it to list every project.
# With a <query> arg, every session is scored by how well the query terms match
# its title + your prompts; non-matches are dropped and the list is ranked by
# relevance, then recency (this is what `tfind` drives). Returns nonzero on no
# pick / no fzf. Rows carry two extra signals: ↻ marks a session opened more
# recently than written (the claude-stamp-tmux opened/ stamp), and " · #N <state>"
# tags a session whose transcript mentions a PR (cache-only state; see below).
_claude_session_rows() {
  # Diagnostics go to stderr: this function's stdout is captured by the caller's
  # $(...), so a stdout error would be swallowed silently instead of shown.
  command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; return 1; }
  local projects="$HOME/.claude/projects"
  [[ -d $projects ]] || { echo "No Claude sessions at $projects" >&2; return 1; }
  local filter="${1:-}" query="${2:-}"
  # Worktree-aware scoping: a per-session worktree's recorded cwd lives under
  # DEV_WORKTREE_ROOT, NOT under the repo dir, so a raw-$PWD prefix match would hide every
  # worktree session of the repo you are standing in — including one synced from another
  # machine, where only the branch + transcript travel (the worktree dir never does). So
  # resolve the filter to its canonical repo dir (works whether $PWD is the repo or one of
  # its worktrees) and hand python both that dir and DEV_WORKTREE_ROOT so it also matches
  # the repo's worktrees (mirrors _dev_dir_in_scope). Non-DEV dirs keep the raw prefix.
  if [[ -n $filter ]]; then
    local _r; _r=$(_dev_repo_of_dir "$filter" 2>/dev/null)
    [[ -n $_r ]] && filter="${DEV_REPOS[${_r%%$'\t'*}]}"
  fi

  DEV_WORKTREE_ROOT="$DEV_WORKTREE_ROOT" python3 - "$projects" "$filter" "$query" <<'PY'
import json, os, sys, glob, datetime, re, subprocess, time
root = sys.argv[1]
filt = sys.argv[2] if len(sys.argv) > 2 else ''
query = sys.argv[3] if len(sys.argv) > 3 else ''
# DEV_WORKTREE_ROOT lets the scope test treat a per-session worktree
# ($DEV_WORKTREE_ROOT/<repo-basename>/<slot>) as belonging to its repo — the twin of the
# zsh _dev_dir_in_scope, so the repo-scoped picker shows worktree (incl. synced) sessions.
wt_root = os.environ.get('DEV_WORKTREE_ROOT', '')
# Last-opened stamps (claude-stamp-tmux job 3): resuming appends nothing to the
# transcript, so recency = max(transcript mtime, opened-stamp mtime) — a session
# reopened five minutes ago sorts (and dates) as five minutes old even though its
# last message is days old. Stamp newer than the transcript → the row is marked ↻
# (recently opened, nothing new written yet).
cache_root = os.path.join(os.environ.get('XDG_CACHE_HOME')
                          or os.path.expanduser('~/.cache'), 'claude-sessions')
opened_dir = os.path.join(cache_root, 'opened')
def _opened(sid):
    try: return os.path.getmtime(os.path.join(opened_dir, sid))
    except OSError: return 0
# PR-in-session indicator: the LAST github.com …/pull/N URL in the transcript (a
# `gh pr create` lands its URL in the tool output; last mention ≈ the PR the
# session ended up on) tags the row " · #N <state>". State comes ONLY from the
# pr/ cache — the picker never blocks on the network. Uncached/stale refs are
# refreshed by DETACHED `gh pr view` children spawned after the scan, so state
# appears on the NEXT invocation. MERGED/CLOSED are terminal (cached forever);
# OPEN and lookup failures ('?') recheck after 5 min. Sibling of opened/ — the
# pid-registry prune globs plain files only, so subdirs are never swept.
pr_dir = os.path.join(cache_root, 'pr')
pr_re = re.compile(r'github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)')
stale = []
def pr_state(ref):
    st, fresh = '', False
    try:
        fp = os.path.join(pr_dir, '#'.join(ref))
        st = open(fp).read().strip()
        fresh = time.time() - os.path.getmtime(fp) < 300
    except OSError: pass
    if st not in ('MERGED', 'CLOSED') and not fresh:
        stale.append(ref)
    return st if st in ('MERGED', 'CLOSED', 'OPEN') else ''
def pr_refresh():                     # fire-and-forget; bounded so a first scan
    if stale:
        try: os.makedirs(pr_dir, exist_ok=True)
        except OSError: return
    for owner, repo, num in stale[:8]:  # over many PRs can't fork-bomb gh
        fp = os.path.join(pr_dir, f'{owner}#{repo}#{num}')
        cmd = (f'st=$(gh pr view "https://github.com/{owner}/{repo}/pull/{num}"'
               f' --json state --jq .state 2>/dev/null) || st="?"; '
               f'printf %s "$st" > "{fp}.$$.tmp" && mv "{fp}.$$.tmp" "{fp}"')
        try:
            subprocess.Popen(['/bin/sh', '-c', cmd], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            break
def _in_scope(cwd):
    if not filt:
        return True
    cwd = cwd or ''
    if cwd == filt or cwd.startswith(filt + os.sep):
        return True
    if wt_root and cwd.startswith(os.path.join(wt_root, os.path.basename(filt.rstrip('/'))) + os.sep):
        return True
    return False
# Query terms, minus a few stopwords so "redesign of portfolio tab" matches on
# the words that carry meaning, not "of". Short tokens like "ui"/"db" survive.
STOP = {'of','the','a','an','to','on','in','for','and','is','it','with','at'}
qterms = [t for t in query.lower().split() if t not in STOP]
# Whole-file scan, but only JSON-parse the lines we need (cheap substring gate
# first) so grabbing the *latest* aiTitle stays fast over hundreds of sessions.
rows = []
for f in glob.glob(os.path.join(root, '*', '*.jsonl')):
    sid = os.path.basename(f)[:-6]
    cwd = msg = title = ctitle = pr = None
    umsgs = []                                          # all your prompts (search mode)
    try:
        for line in open(f, errors='ignore'):
            if cwd is None and '"cwd"' in line:
                try: cwd = json.loads(line).get('cwd')
                except ValueError: pass
            if '/pull/' in line:                        # last PR URL wins
                m = pr_re.findall(line)
                if m: pr = m[-1]
            if '"custom-title"' in line:                # set by /rename — wins
                try:
                    t = json.loads(line).get('customTitle')
                    if t: ctitle = t                    # keep the most recent
                except ValueError: pass
            if '"ai-title"' in line:
                try:
                    t = json.loads(line).get('aiTitle')
                    if t: title = t                     # keep the most recent
                except ValueError: pass
            # First user message is enough for the display title; in search mode
            # keep parsing to collect every prompt to match the query against.
            if (msg is None or qterms) and '"type":"user"' in line:
                try:
                    c = json.loads(line).get('message', {}).get('content')
                    txt = c if isinstance(c, str) else (
                        ' '.join(x.get('text', '') for x in c if isinstance(x, dict))
                        if isinstance(c, list) else '')
                    txt = txt.strip()
                    if txt and not txt.startswith('<'):
                        if msg is None: msg = txt
                        if qterms: umsgs.append(txt.lower())
                except ValueError: pass
    except OSError:
        continue
    if not _in_scope(cwd):
        continue
    # ── Relevance scoring (search mode) ──────────────────────────────────────
    # The tunable heart of `tfind`: how much each match counts. A term in the
    # session's headline (its title, or opening prompt if untitled) is weighted
    # 4x a term buried mid-conversation — a session *about* X beats one that
    # merely mentions X in passing. Drop sessions with no match at all.
    score = 0
    if qterms:
        hay  = ' '.join(umsgs)
        head = (ctitle or title or msg or '').lower()
        score = sum(hay.count(t) + 4 * head.count(t) for t in qterms)
        if score == 0:
            continue
    # ─────────────────────────────────────────────────────────────────────────
    mtime = os.path.getmtime(f)
    opened = _opened(sid)
    mark = '↻ ' if opened > mtime else '  '
    mtime = max(mtime, opened)
    short = os.path.basename(cwd) if cwd else '?'
    title = ' '.join((ctitle or title or msg or '(no message)').split())[:80]
    prs = ''
    if pr:
        st = pr_state(pr)
        prs = f" · #{pr[2]}{' ' + st.lower() if st else ''}"
    rows.append((score, mtime, sid, cwd or '?', short, title, mark, prs))
# Primary key = relevance (0 for every row when not searching, so it collapses
# to pure newest-first); tiebreak = recency.
rows.sort(reverse=True)
for score, mtime, sid, cwd, short, title, mark, prs in rows:
    when = datetime.datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')
    print(f"{sid}\t{cwd}\t{when} {mark}{short:<18}  {title}{prs}")
pr_refresh()
PY
}

# _claude_sessions_fzf [cwd] [query] — fzf-pick a saved Claude transcript, echoing
# the chosen "<session-id>\t<cwd>\t<display>" row (fzf shows only the display
# column). Thin picker over _claude_session_rows; all the scan/scoring logic lives
# there. Returns nonzero on no pick / no fzf.
_claude_sessions_fzf() {
  command -v fzf >/dev/null 2>&1 || { echo "fzf not installed (brew install fzf)" >&2; return 1; }
  local filter="${1:-}" query="${2:-}"
  local fzf_prompt='resume claude (all) > '   # not `prompt`: that local IS the shell's PS1
  [[ -n $filter ]] && fzf_prompt="resume claude (${filter:t}) > "
  [[ -n $query  ]] && fzf_prompt="resume claude (search: $query) > "
  _claude_session_rows "$filter" "$query" | _t_fzf --delimiter=$'\t' --with-nth=3 --no-hscroll \
        --prompt="$fzf_prompt" --height=60% --reverse
}

# Return success only for the visible, final Codex turn error. A quoted example
# or unrelated old message must not turn an ordinary restart into a daemon bypass.
_t_invalid_cwd_screen() {
  emulate -L zsh
  local line
  for line in ${(f)1}; do
    [[ $line =~ '^[[:space:]]*(■[[:space:]]*)?Failed to start turn: turn/start failed in TUI: turn/start failed: invalid cwd: No such file or directory \(os error 2\) \(code -32600\)[[:space:]]*$' ]] && return 0
  done
  return 1
}

_t_pid_is_zombie() {
  emulate -L zsh
  local state
  state=$(ps -o stat= -p "$1" 2>/dev/null) || return 1
  [[ $state =~ '^[[:space:]]*Z' ]]
}

# Explicit recovery of a stuck client. Keep the pane as a worktree reservation,
# capture unsent visible text before signalling, and never force-respawn a live
# pane. A per-slot lock prevents two simultaneous restarts from racing.
_t_restart_client_only() {
  local cmdline
  cmdline=$(ps -ww -o args= -p "$1" 2>/dev/null) || {
    print -u2 -- 't restart: cannot verify the client process; nothing was stopped'; return 1
  }
  local -a words; read -r -A words <<< "$cmdline"
  if (( ${words[(Ie)app-server]} )); then
    print -u2 -- 't restart: refusing to stop a shared app-server; nothing was stopped'; return 1
  fi
}

_t_restart_slot() {
  emulate -L zsh
  local session="$1" dir="$2" sid="$3" agent="$4" mode="$5" expected_pid="${6:-}" expected_dead="${7:-}"
  local pane cpid up attempt snapshot old_remain actual_dir launch screen invalid_cwd=0
  local cache="${XDG_CACHE_HOME:-$HOME/.cache}/t/restart" lock
  # Do not reuse a process snapshot captured by another command in this shell.
  local _DEV_PS_AT=0
  [[ $agent == codex || $agent == claude ]] || return 1
  [[ -d $dir ]] || { print -u2 -- 't restart: worktree is missing'; return 1; }
  tmux has-session -t "=$session:" 2>/dev/null || return 1
  actual_dir=$(tmux display-message -p -t "=$session:" '#{session_path}')
  [[ $actual_dir == $dir && $(_dev_agent_of_session "$session") == $agent &&
     $(_dev_session_sid "$session" "$dir") == $sid ]] || {
    print -u2 -- 't restart: the slot changed; inspect it and try again'; return 1
  }
  _dev_app_slot_reserved "$dir" && {
    print -u2 -- 't restart: the desktop app owns this worktree; close and release it first'; return 1
  }
  pane=$(tmux display-message -p -t "=$session:" '#{pane_id}') || return 1
  [[ $(tmux list-panes -s -t "=$session" -F '#{pane_id}') == $pane ]] || {
    print -u2 -- 't restart: multiple panes in this slot; keep only the agent pane before restarting'; return 1
  }
  cpid=$(_dev_session_claude_pid "$session")
  if [[ -n $cpid ]]; then _t_restart_client_only "$cpid" || return 1; fi
  [[ -z $expected_pid || $cpid == $expected_pid ]] || {
    print -u2 -- 't restart: the agent changed since the recovery offer; nothing was stopped'; return 1
  }
  if [[ $mode == restart-dead ]]; then
    [[ -z $cpid && -n $sid && -n $expected_dead &&
       $(tmux display-message -p -t "$pane" '#{pane_dead}') == 1 &&
       $(tmux display-message -p -t "$pane" '#{pane_id} #{pane_pid} #{pane_dead_time} #{pane_dead}') == $expected_dead ]] || {
      print -u2 -- 't restart: the exited pane changed; nothing was stopped'; return 1
    }
  fi
  if [[ $mode == restart-invalid-cwd ]]; then
    [[ $agent == codex && -n $sid && -n $expected_pid ]] || return 1
  fi
  if [[ $mode == restart-no-daemon ]]; then
    [[ $agent == codex && -n $expected_pid ]] || return 1
    # This bypass is invocation-local. Never restart the shared server, which
    # may own other sessions, or change the user's daemon configuration.
    [[ -n $sid ]] || _dev_agent_at_welcome "$agent" "$session" "$dir" || return 1
    command codex --help 2>/dev/null | command grep -q -- '--no-daemon' || {
      print -u2 -- 't restart: this Codex version does not support --no-daemon'; return 1
    }
  fi
  [[ $cpid == <-> || ( -z $cpid && $(tmux display-message -p -t "$pane" '#{pane_dead}') == 1 ) ]] || {
    print -u2 -- 't restart: no live agent or exited pane to restart; inspect with t session open'; return 1
  }
  up=$PPID
  while [[ -n $up && $up != 0 && $up != 1 ]]; do
    [[ $up == $cpid ]] && {
      print -u2 -- 't restart: run from a separate terminal, outside the agent being restarted'; return 1
    }
    up=$(ps -o ppid= -p "$up" 2>/dev/null); up=${up//[[:space:]]/}
  done
  if [[ $mode == dry-run ]]; then
    print -r -- "Would save pane text, stop $agent (pid $cpid), and resume $sid in $dir"
    return 0
  fi
  ( umask 077; mkdir -p "$cache" ) || return 1
  lock="$cache/$session.lock"
  mkdir "$lock" 2>/dev/null || {
    print -u2 -r -- "t restart: recovery already locked ($lock); if interrupted, inspect the slot before removing that empty lock directory"
    return 1
  }
  {
    snapshot=$(mktemp "$cache/$session.XXXXXXXX") || return 1
    tmux capture-pane -p -J -S - -t "$pane" >| "$snapshot" || return 1
    print -r -- "Saved pane text (including visible draft): $snapshot"
    # Capture can take time: revalidate immediately before changing the pane.
    _DEV_PS_AT=0
    [[ $(_dev_session_claude_pid "$session") == $cpid &&
       $(_dev_session_sid "$session" "$dir") == $sid &&
       $(_dev_agent_of_session "$session") == $agent &&
       $(tmux display-message -p -t "=$session:" '#{session_path}') == $dir &&
       $(tmux list-panes -s -t "=$session" -F '#{pane_id}') == $pane ]] || {
      print -u2 -- 't restart: the slot changed; nothing was stopped'; return 1
    }
    if [[ $agent == codex && ( $mode == restart || $mode == restart-invalid-cwd ) ]]; then
      screen=$(tmux capture-pane -p -J -t "$pane") || return 1
      _t_invalid_cwd_screen "$screen" && invalid_cwd=1
      if [[ $mode == restart-invalid-cwd && $invalid_cwd != 1 ]]; then
        print -u2 -- 't restart: the invalid-cwd failure cleared; nothing was stopped'; return 1
      fi
    fi
    if (( invalid_cwd )); then
      # The pane's original cwd inode can be stale even though its worktree path
      # exists again. An explicit cwd also avoids a stale shared daemon cwd.
      [[ -n $sid && -d $dir ]] || {
        print -u2 -- 't restart: the thread or worktree is missing; nothing was stopped'; return 1
      }
      command codex resume --help 2>/dev/null | command grep -q -- '--no-daemon' || {
        print -u2 -- 't restart: this Codex version does not support --no-daemon'; return 1
      }
      command codex resume --help 2>/dev/null | command grep -q -- '--cd' || {
        print -u2 -- 't restart: this Codex version does not support --cd'; return 1
      }
      launch="$(_dev_agent_resume_cmd "$agent" "$sid") --cd ${(q)dir} --no-daemon"
    elif [[ $mode == restart-no-daemon ]]; then
      screen=$(tmux capture-pane -p -J -t "$pane") || return 1
      [[ $screen == *'Cannot use the background server'* &&
         $screen == *'Run without daemon this time'* &&
         ( ( $screen == *'Experimental feature request failed'* &&
             $screen == *'Restart cannot resolve this compatibility check.'* &&
             $screen == *'2. Cancel'* ) ||
           $screen == *'background server is not running'* ||
           $screen == *'background server socket is stale or unreachable'* ) ]] || {
        print -u2 -- 't restart: the startup failure cleared; nothing was stopped'; return 1
      }
      if [[ -n $sid ]]; then
        launch="$(_dev_agent_resume_cmd "$agent" "$sid") --no-daemon"
      else
        launch="$(_dev_agent_new_cmd "$agent") --no-daemon"
      fi
    else
      launch=$(_dev_agent_resume_cmd "$agent" "$sid")
      if [[ $agent == codex ]]; then
        # Reconnecting through the shared daemon can disturb other windows.
        # Every recovery must be invocation-local, including exited panes.
        command codex resume --help 2>/dev/null | command grep -q -- '--no-daemon' || {
          print -u2 -- 't restart: this Codex version does not support --no-daemon; nothing was stopped'; return 1
        }
        launch+=' --no-daemon'
      fi
    fi
    if (( invalid_cwd )); then
      # Capability checks and capture can race with the client moving on.
      _DEV_PS_AT=0
      [[ -d $dir && $(_dev_session_claude_pid "$session") == $cpid &&
         $(_dev_session_sid "$session" "$dir") == $sid &&
         $(_dev_agent_of_session "$session") == $agent &&
         $(tmux display-message -p -t "=$session:" '#{session_path}') == $dir &&
         $(tmux list-panes -s -t "=$session" -F '#{pane_id}') == $pane ]] || {
        print -u2 -- 't restart: the slot changed; nothing was stopped'; return 1
      }
      screen=$(tmux capture-pane -p -J -t "$pane") || return 1
      _t_invalid_cwd_screen "$screen" || {
        print -u2 -- 't restart: the invalid-cwd failure cleared; nothing was stopped'; return 1
      }
    fi
    if [[ $mode == restart-dead ]]; then
      _DEV_PS_AT=0
      [[ -z $(_dev_session_claude_pid "$session") &&
         $(tmux display-message -p -t "$pane" '#{pane_id} #{pane_pid} #{pane_dead_time} #{pane_dead}') == $expected_dead ]] || {
        print -u2 -- 't restart: the exited pane changed; nothing was stopped'; return 1
      }
    fi
    if [[ -n $cpid ]]; then _t_restart_client_only "$cpid" || return 1; fi
    old_remain=$(tmux show-options -A -p -v -t "$pane" remain-on-exit) || return 1
    tmux set-environment -t "=$session" CLAUDE_RESUME_ID "$sid" || return 1
    tmux set-environment -t "=$session" DEV_AGENT "$agent" || return 1
    tmux set-option -p -t "$pane" remain-on-exit on || return 1
    if [[ -n $cpid ]]; then kill -TERM "$cpid" || return 1; fi
    for attempt in {1..200}; do
      if [[ $(tmux display-message -p -t "$pane" '#{pane_dead}') == 1 ]] &&
         { [[ -z $cpid ]] || ! kill -0 "$cpid" 2>/dev/null ||
           _t_pid_is_zombie "$cpid"; }; then
        # On Linux an exited pane's child can remain a zombie until reaped;
        # kill -0 still succeeds, but a verified Z state cannot run an agent.
        # An unknown or unreadable process state keeps the pane reserved.
        # No -k: tmux must refuse if anything is still running in this pane.
        tmux respawn-pane -t "$pane" -c "$dir" "zsh -lic ${(q)launch}" || return 1
        tmux set-option -p -t "$pane" remain-on-exit "$old_remain"
        return $?
      fi
      sleep 0.05
    done
    print -u2 -- 't restart: the old client or its pane has not exited; no second client was started. Inspect with t session open.'
    return 1
  } always {
    rmdir "$lock" 2>/dev/null
  }
}

# Independent proof that this live Codex PID owns the requested thread. A tmux
# CLAUDE_RESUME_ID stamp is intentionally insufficient: we write it before launch.
_t_app_pull_ui_ready() {
  local session="$1" dir="$2" sid="$3" pid="$4" codex_home="${5:-${CODEX_HOME:-$HOME/.codex}}" db args pane_text
  db="$codex_home/state_5.sqlite"; [[ -r $db ]] || return 1
  args=$(ps -ww -o args= -p "$pid" 2>/dev/null) || return 1
  pane_text=$(tmux capture-pane -p -t "=$session:" 2>/dev/null) || return 1
  LC_ALL=C python3 - "$db" "$dir" "$sid" "$args" "$pane_text" <<'PY' 2>/dev/null
import json, os, re, sqlite3, sys

try:
    db, cwd, sid, args, pane = sys.argv[1:]
    # The process walker has already identified this PID as Codex. Its own argv,
    # rather than a prewritten tmux stamp, binds the live process to the thread.
    prefix, sep, tail = args.partition(' resume ')
    if (not sep or not re.fullmatch(r'codex(?:-(?:aarch64|x86_64)-[\w.-]+)?',
                                     os.path.basename(prefix)) or
            tail != f'{sid} --cd {cwd} --no-daemon'):
        sys.exit(1)
    # The default empty composer shows that the interactive TUI is drawn; exact
    # resume argv and rollout metadata supply the thread identity. An error
    # screen mentioning Codex lacks this anchored input prompt.
    if not any(re.fullmatch(r'\s*[›»]\s+Ask Codex to do anything\s*', line)
               for line in pane.splitlines()[-12:]):
        sys.exit(1)
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=0.5)
    rows = c.execute('select rollout_path from threads where id=? and cwd=? '
                     'and archived=0 and trim(first_user_message)<>\'\' '
                     'and instr(source, \'"subagent"\')=0', (sid, cwd)).fetchall()
    if len(rows) != 1:
        sys.exit(1)
    with open(rows[0][0]) as f:
        meta = json.loads(f.readline())
    payload = meta.get('payload', {})
    if (meta.get('type') != 'session_meta' or payload.get('id') != sid or
            payload.get('cwd') != cwd or payload.get('source') not in ('cli', 'vscode') or
            payload.get('thread_source') == 'subagent'):
        sys.exit(1)
except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
    sys.exit(1)
PY
}

_t_app_pull_lock_owned() {
  local pid="$1" sid="$2" codex_home="$3"
  [[ $pid == <-> && -n $sid && -n $codex_home ]] || return 1
  LC_ALL=C python3 - "$pid" "$sid" "$codex_home" <<'PY' 2>/dev/null
import errno, fcntl, os, stat, subprocess, sys

try:
    pid, sid, home = sys.argv[1:]
    path = os.path.join(home, 'thread-writer-locks', sid + '.lock')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            sys.exit(1)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EACCES):
                raise
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            sys.exit(1)  # No process currently owns the writer lock.
    finally:
        os.close(fd)
    listed = subprocess.run(['lsof', '-a', '-p', pid, '-Fn', '--', path],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=2, check=False)
    if listed.returncode or not any(
            line.startswith('n') and os.path.realpath(line[1:]) == os.path.realpath(path)
            for line in listed.stdout.splitlines()):
        sys.exit(1)
except (OSError, ValueError, subprocess.TimeoutExpired):
    sys.exit(1)
PY
}

_t_app_pull_ready() {
  local session="$1" dir="$2" sid="$3" pid="$4" codex_home="${5:-${CODEX_HOME:-$HOME/.codex}}"
  local reg line reg_sid reg_cwd started
  [[ $pid == <-> ]] || return 1
  _t_app_pull_lock_owned "$pid" "$sid" "$codex_home" || return 1
  reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/$pid"
  if [[ -r $reg ]]; then
    line="$(<"$reg")"
    reg_sid=${line%%$'\t'*}; reg_cwd=${line#*$'\t'}
    if [[ $reg_sid == $sid && $reg_cwd == $dir ]]; then
      started=$(LC_ALL=C ps -o lstart= -p "$pid" 2>/dev/null)
      if [[ -n $started ]] && LC_ALL=C python3 - "$reg" "$started" <<'PY' 2>/dev/null; then
import datetime, os, sys
try:
    born = datetime.datetime.strptime(sys.argv[2].strip(), '%a %b %d %H:%M:%S %Y').timestamp()
    sys.exit(0 if os.stat(sys.argv[1]).st_mtime >= born else 1)
except (OSError, ValueError):
    sys.exit(1)
PY
        return 0
      fi
    fi
  fi
  [[ $(_codex_pane_sid "$session" "$dir" "$pid" 2>/dev/null) == $sid ]] ||
    _t_app_pull_ui_ready "$session" "$dir" "$sid" "$pid" "$codex_home"
}

# Read-only ownership probe used before retrying a handoff interrupted after
# CLI launch. Output is a PID only when this exact session owns the thread.
_t_app_pull_owner() {
  emulate -L zsh
  local session="$1" dir="$2" sid="$3" codex_home="${4:-${CODEX_HOME:-$HOME/.codex}}"
  local actual pid
  tmux has-session -t "=$session:" 2>/dev/null || return 1
  actual=$(tmux display-message -p -t "=$session:" '#{session_path}' 2>/dev/null) || return 1
  [[ -n $actual && ${actual:A} == ${dir:A} && $(_dev_agent_of_session "$session") == codex ]] || return 1
  local _DEV_PS_AT=0
  pid=$(_dev_session_claude_pid "$session")
  [[ -n $pid ]] && _t_app_pull_ready "$session" "$dir" "$sid" "$pid" "$codex_home" || return 1
  print -r -- "$pid"
}

# Return a desktop-reserved Codex worktree to its original tmux slot. The caller
# serializes handoffs and has verified that this exact thread was released.
# expected_sid is '-' for a desktop-only row with no prior tmux session.
_t_app_pull_slot() {
  emulate -L zsh
  local session="$1" dir="$2" sid="$3" expected_sid="$4" codex_home="${5:-${CODEX_HOME:-$HOME/.codex}}"
  local marker actual actual_sid pane panes owner owner_dir pid attempt found=0 launch
  local -a owners
  [[ $session == dev-* && -n $dir && -d $dir && -n $sid && $sid != - && -n $expected_sid ]] || {
    print -u2 -- 't app pull: invalid slot, worktree, or thread'; return 1
  }
  [[ $sid =~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' &&
     ( $expected_sid == - || $expected_sid =~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' ) ]] || {
    print -u2 -- 't app pull: invalid thread identifier'; return 1
  }
  marker=$(_dev_app_slot_marker "$dir") && [[ -f $marker ]] || {
    print -u2 -- 't app pull: this worktree has no desktop reservation'; return 1
  }
  command -v codex >/dev/null 2>&1 || { print -u2 -- 't app pull: codex is unavailable'; return 1; }
  [[ $codex_home == /* && -d $codex_home ]] || {
    print -u2 -- 't app pull: the selected Codex home is unavailable'; return 1
  }
  launch="export CODEX_HOME=${(q)codex_home}; exec codex resume ${(q)sid} --cd ${(q)dir} --no-daemon"
  local fg_sid fg_rest fg_cwd
  while IFS=$'\t' read -r fg_sid fg_rest; do
    fg_cwd=${fg_rest%%$'\t'*}
    if [[ $fg_sid == $sid || ( -n $fg_cwd && ${fg_cwd:A} == ${dir:A} ) ]]; then
      print -u2 -- 't app pull: a foreground CLI already owns this thread or worktree'
      return 1
    fi
  done < <(_dev_fg_rows 2>/dev/null)
  # Check every alias, including sessions whose names differ from the selected
  # slot. A second owner of the same cwd must never be silently ignored.
  # tmux 3.7c replaces literal tabs in -F with `_`; a printable separator
  # preserves the complete path in the final read field, including any `|`.
  while IFS='|' read -r owner owner_dir; do
    [[ -n $owner && -n $owner_dir ]] || continue
    [[ ${owner_dir:A} == ${dir:A} ]] && owners+=("$owner")
    if [[ $owner != $session ]]; then
      local _DEV_PS_AT=0
      pid=$(_dev_session_claude_pid "$owner")
      if [[ -n $pid && $(_dev_session_sid "$owner" "$owner_dir") == $sid ]]; then
        print -u2 -- 't app pull: this thread is already live in another tmux session'
        return 1
      fi
    fi
  done < <(tmux list-sessions -F '#{session_name}|#{session_path}' 2>/dev/null)
  (( $#owners <= 1 )) || { print -u2 -- 't app pull: multiple tmux sessions own this worktree'; return 1; }
  if (( $#owners )); then
    [[ $owners[1] == $session ]] || { print -u2 -- 't app pull: another tmux session owns this worktree'; return 1; }
    tmux has-session -t "=$session:" 2>/dev/null || return 1
    actual=$(tmux display-message -p -t "=$session:" '#{session_path}') || return 1
    [[ ${actual:A} == ${dir:A} ]] || { print -u2 -- 't app pull: slot directory changed'; return 1; }
    actual_sid=$(_dev_session_sid "$session" "$dir")
    [[ $(_dev_agent_of_session "$session") == codex &&
       ( $actual_sid == $expected_sid || ( $expected_sid == - && -z $actual_sid ) ) ]] || {
      print -u2 -- 't app pull: slot agent or thread changed'; return 1
    }
    local _DEV_PS_AT=0
    pid=$(_dev_session_claude_pid "$session")
    if [[ -n $pid ]]; then
      if _t_app_pull_ready "$session" "$dir" "$sid" "$pid" "$codex_home"; then
        rm -- "$marker" || return 1
        tmux set-option -t "=$session:" window-size latest 2>/dev/null
        _dev_recovery_watch "$session"
        return 0
      fi
      print -u2 -- 't app pull: an unverified CLI already owns this slot'
      return 1
    fi
    panes=$(tmux list-panes -s -t "=$session" -F '#{pane_id}') || return 1
    [[ -n $panes && $panes != *$'\n'* ]] || {
      print -u2 -- 't app pull: slot has multiple panes'; return 1
    }
    pane=${panes%%$'\n'*}
    command codex resume --help 2>/dev/null | command grep -q -- '--no-daemon' || {
      print -u2 -- 't app pull: this Codex version does not support --no-daemon; upgrade Codex and retry'; return 1
    }
    tmux set-environment -t "=$session" CLAUDE_RESUME_ID "$sid" || return 1
    tmux set-environment -t "=$session" DEV_AGENT codex || return 1
    if [[ $(tmux display-message -p -t "$pane" '#{pane_dead}') == 1 ]]; then
      tmux set-option -p -t "$pane" remain-on-exit on || return 1
      tmux respawn-pane -t "$pane" -c "$dir" "zsh -lic ${(q)launch}" || return 1
    else
      # Keep the idle shell intact. A new window gives the CLI an independent
      # process tree, without injecting keystrokes into an interactive shell.
      launch="tmux set-option -p -t \"\$TMUX_PANE\" remain-on-exit on; $launch"
      tmux new-window -t "=$session:" -c "$dir" "zsh -lic ${(q)launch}" || return 1
    fi
  else
    tmux has-session -t "=$session:" 2>/dev/null && {
      print -u2 -- 't app pull: slot name is occupied elsewhere'; return 1
    }
    command codex resume --help 2>/dev/null | command grep -q -- '--no-daemon' || {
      print -u2 -- 't app pull: this Codex version does not support --no-daemon; upgrade Codex and retry'; return 1
    }
    # The pane enables remain-on-exit before exec, including a fast Codex
    # startup failure. No default shell or personal tmux configuration is used.
    launch="tmux set-option -p -t \"\$TMUX_PANE\" remain-on-exit on; $launch"
    tmux new-session -d -s "$session" -c "$dir" \
      -e "CLAUDE_RESUME_ID=$sid" -e 'DEV_AGENT=codex' \
      "zsh -lic ${(q)launch}" || return 1
  fi
  # A stamp alone is not success. Wait for a real Codex process in this exact
  # slot and for the thread resolver to agree before releasing the reservation.
  # Process scans can be slow on a busy host; include their time in the bound.
  zmodload zsh/datetime
  local -F deadline=$(( EPOCHREALTIME + 5 ))
  for attempt in {1..100}; do
    (( EPOCHREALTIME < deadline )) || break
    local _DEV_PS_AT=0
    pid=$(_dev_session_claude_pid "$session")
    if [[ -n $pid && $(_dev_agent_of_session "$session") == codex &&
          $(tmux display-message -p -t "=$session:" '#{session_path}' 2>/dev/null) == $dir ]] &&
       _t_app_pull_ready "$session" "$dir" "$sid" "$pid" "$codex_home"; then
      found=1; break
    fi
    sleep 0.05
  done
  (( found )) || {
    local short=${session#dev-}
    print -u2 -- "t app pull: CLI thread could not be verified; worktree remains reserved. Inspect startup with tmux attach-session -t ${(q)session}, then retry t open ${short%-*} ${short##*-} --cli"
    return 1
  }
  # A process observed for one snapshot may be a client that failed instantly.
  sleep 0.1
  local _DEV_PS_AT=0
  [[ $(_dev_session_claude_pid "$session") == $pid ]] &&
    _t_app_pull_ready "$session" "$dir" "$sid" "$pid" "$codex_home" || {
    print -u2 -- 't app pull: Codex exited during startup; the worktree remains reserved'
    return 1
  }
  tmux set-option -t "=$session:" window-size latest 2>/dev/null
  rm -- "$marker" || { print -u2 -- 't app pull: CLI started but reservation could not be released'; return 1; }
  _dev_recovery_watch "$session"
  return 0
}

# Release one desktop reservation after the selected conversation has been
# unloaded. The Python caller resolves the exact saved thread and serializes
# handoffs; recheck it here immediately before changing tmux or the marker.
# Archive in the owning desktop app is required while its private backend holds
# the writer lock. This helper never signals the app or edits its history.
_t_app_close_verify() {
  local dir="$1" sid="$2" codex_home="$3" rollout="$4" archived="$5"
  python3 - "$T_HOME/libexec/t_app_handoff.py" "$dir" "$sid" "$codex_home" "$rollout" "$archived" <<'PY'
import os, runpy, sqlite3, subprocess, sys
from pathlib import Path

helper, cwd, sid, home, rollout, archived = sys.argv[1:]
def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)
try:
    mod = runpy.run_path(helper)
    if not mod['_safe_archive_hint'](cwd, run):
        raise ValueError('this is not a reserved t worktree safe to close')
    if sid == '-':
        mod['assert_workspace_released'](home, cwd,
                                         retry='Retry t kill after Codex releases this worktree.')
    else:
        db = Path(home) / 'state_5.sqlite'
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=0.5) as conn:
            rows = conn.execute('select cwd,rollout_path,archived from threads where id=?',
                                (sid,)).fetchall()
        if (len(rows) != 1 or not isinstance(rows[0][0], str)
                or not isinstance(rows[0][1], str)
                or os.path.realpath(rows[0][0]) != os.path.realpath(cwd)
                or os.path.realpath(rows[0][1]) != os.path.realpath(rollout)
                or rows[0][2] != int(archived)):
            raise ValueError('the selected saved conversation changed during close')
        mod['assert_released'](None, sid, cwd, run, codex_home=home,
                               rollout=rollout, archived=archived == '1',
                               retry='Retry t kill after the conversation is released.')
    mod['assert_desktop_view_released'](
        None, run, archived=archived == '1', empty=sid == '-',
        retry='Retry t kill after closing or archiving the desktop view.')
except (OSError, ValueError, sqlite3.Error) as exc:
    message = str(exc)
    if ('selected Codex conversation is still loaded' in message
            or 'no verifiable writer lock contract' in message):
        message = ('the selected chat is still loaded. Finish its turn and Archive it '
                   'in Codex, then retry t kill; the worktree remains reserved')
    print('t kill: ' + message, file=sys.stderr)
    sys.exit(1)
PY
}

_t_app_close_slot() {
  emulate -L zsh
  local session="$1" dir="$2" sid="$3" codex_home="$4" rollout="$5" archived="$6"
  local alias_key slot expected marker actual stamp owner owner_dir pid pids comm
  [[ $session == dev-*-<-> && -d $dir && $dir == /*
     && $codex_home == /* &&
     ( ( $sid == - && $rollout == - && $archived == 0 ) ||
       ( $sid =~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
         && $rollout == /* && $archived == (0|1) ) ) ]] || {
    print -u2 -- 't kill: invalid desktop slot or saved conversation'; return 1
  }
  alias_key=${${session#dev-}%-*}; slot=${session##*-}
  [[ -n ${DEV_REPOS[$alias_key]:-} && -n $DEV_WORKTREE_ROOT ]] || {
    print -u2 -- 't kill: the desktop slot is not registered here'; return 1
  }
  expected=$(_dev_worktree_path "$alias_key" "$slot")
  [[ ${expected:A} == ${dir:A} ]] || {
    print -u2 -- 't kill: the desktop worktree changed'; return 1
  }
  marker=$(_dev_app_slot_marker "$dir") || return 1
  [[ -f $marker && ! -L $marker && $(<"$marker") == codex-app ]] || {
    print -u2 -- 't kill: the desktop reservation changed'; return 1
  }
  # A live CLI may have taken this path under any registered alias, not just
  # the selected session name. A second parked session also makes ownership
  # ambiguous, so leave both untouched.
  while IFS='|' read -r owner owner_dir; do
    [[ -n $owner && $owner_dir == /* && ${owner_dir:A} == ${dir:A} ]] || continue
    if _dev_session_has_claude "$owner"; then
      print -u2 -- 't kill: a CLI is still running in this worktree'; return 1
    fi
    if [[ $owner != $session ]]; then
      print -u2 -- 't kill: another tmux session reserves this worktree'; return 1
    fi
  done < <(tmux list-sessions -F '#{session_name}|#{session_path}' 2>/dev/null)
  if tmux has-session -t "=$session:" 2>/dev/null; then
    actual=$(tmux display-message -p -t "=$session:" '#{session_path}') || return 1
    [[ ${actual:A} == ${dir:A} ]] || {
      print -u2 -- 't kill: the parked slot changed directory'; return 1
    }
    stamp=$(tmux show-environment -t "=$session" CLAUDE_RESUME_ID 2>/dev/null)
    [[ $stamp == "CLAUDE_RESUME_ID=$sid" || ( $sid == - && -z $stamp ) ]] || {
      print -u2 -- 't kill: the parked slot names another conversation'; return 1
    }
  fi
  # An independent foreground CLI can use the same worktree without tmux.
  pids=$(_dev_cwd_pids "$dir") || {
    print -u2 -- 't kill: could not inspect worktree processes'; return 1
  }
  for pid in ${(f)pids}; do
    comm=$(ps -o comm= -p "$pid" 2>/dev/null) || {
      print -u2 -- 't kill: a worktree process could not be identified'; return 1
    }
    if _dev_agent_is_proc "$comm" && ! _dev_agent_is_service "$pid"; then
      print -u2 -- 't kill: a foreground CLI is still running in this worktree'; return 1
    fi
  done
  _t_app_close_verify "$dir" "$sid" "$codex_home" "$rollout" "$archived" || return 1
  if tmux has-session -t "=$session:" 2>/dev/null; then
    tmux kill-session -t "=$session:" || {
      print -u2 -- 't kill: could not close the parked tmux slot'; return 1
    }
  fi
  _dev_stop_rooted "$dir" || {
    print -u2 -- 't kill: could not stop leftover worktree processes'; return 1
  }
  # Recheck after tmux/process cleanup: another writer may have claimed this
  # thread in the meantime. The marker is the final mutation.
  _t_app_close_verify "$dir" "$sid" "$codex_home" "$rollout" "$archived" || return 1
  [[ -f $marker && ! -L $marker && $(<"$marker") == codex-app ]] || {
    print -u2 -- 't kill: desktop reservation changed during close'; return 1
  }
  rm -- "$marker" || return 1
  if [[ $sid == - ]]; then
    print -r -- "Closed empty desktop slot $session."
  else
    print -r -- "Closed desktop slot $session; saved conversation $sid remains available."
  fi
}

# _dev_resume_session <session> <dir> <session-id> — sibling of _dev_new_session:
# create a detached, logged tmux session in <dir>, but RESUME an existing Claude
# conversation (claude -r) rather than starting fresh on $DEV_BRANCH. Same name
# + log path convention so dev/tread/tpaste treat it like any dev session.
_dev_resume_session() {
  local session="$1" dir="$2" sid="$3" agent="${4:-claude}"
  _dev_auto_trust "$dir"
  local logfile="$HOME/.tmux-logs/${session}.log"
  mkdir -p "$HOME/.tmux-logs"
  # No fixed geometry / window-size latest: fit the active client so attaching from
  # a phone doesn't pan a too-wide window (see _dev_new_session for the full why).
  tmux new-session -d -s "$session" -c "$dir"
  tmux set-option -t "=$session:" window-size latest 2>/dev/null
  tmux pipe-pane -t "=$session:" -o "cat >> $logfile"
  # Record the resumed id on the session so `tpop` can pull the exact same
  # conversation back to the foreground (it also falls back to the dir's newest
  # transcript, but this is the precise signal when we know it).
  tmux set-environment -t "=$session" CLAUDE_RESUME_ID "$sid"
  tmux set-environment -t "=$session" DEV_AGENT "$agent"
  # `; exit` so quitting Claude tears the session down rather than leaving an idle
  # shell (see _dev_new_session for the full rationale).
  tmux send-keys -t "=$session:" "$(_dev_agent_resume_cmd "$agent" "$sid"); exit" Enter
  _dev_recovery_watch "$session"
}

# _dev_slot_for_cwd <cwd> — map a transcript's working dir to a dev session slot.
# Echo "<repo> <slot>" (space-separated) on success, or nothing on failure.
# This is the glue that makes a resumed session "supported by dev": pick the
# DEV_REPOS key whose path matches <cwd>, then choose a free slot for it.
#
# Mapping rules:
#   • Match: exact path, or a worktree/subdir of a repo (cwd under "$path/").
#     Longest matching path wins, so a nested repo beats its parent.
#   • No match: fall back to a key derived from the dir's basename so ANY repo
#     is resumable. dev/tread only validate DEV_REPOS keys, so tpush prints a
#     raw `tmux attach` hint for these derived keys.
#   • Slot: first dev-<repo>-<n> with no running session (mirrors `dev`).
_dev_slot_for_cwd() {
  local cwd="$1" match slot=
  # Resolve the DEV_REPOS alias (and a worktree's own slot, if any) via the shared
  # resolver — handles exact dirs, subdirs, AND per-session worktrees uniformly.
  local r; r=$(_dev_repo_of_dir "$cwd")
  if [[ -n $r ]]; then
    match=${r%%$'\t'*}; slot=${r#*$'\t'}
  else
    # No DEV_REPOS match → derive a key from the dir's basename so ANY session is
    # resumable (e.g. ~/code/dotfiles → "dotfiles"). Such a session still appears
    # in `dev list`, but dev/tread validate against DEV_REPOS and won't know
    # the key — so tpush prints a raw `tmux attach` hint for it instead.
    match="${cwd:t}"                       # :t = basename
    match="${match//[^A-Za-z0-9_-]/-}"     # sanitise for a tmux session name
  fi
  [[ -n "$match" ]] || return 1

  # A worktree path names its own slot: that's the ONLY slot whose cwd is this dir
  # (one worktree per slot). Free → use it; taken → FAIL rather than fall into the
  # generic scan, which would return a different slot number while cwd stays pinned
  # to this worktree — pairing the new slot with another slot's checkout/branch and
  # colliding with the live owner already in it. Callers handle the "couldn't map".
  # "Taken" is judged by session_path, not by name alone: an unrelated dev-<repo>-<n>
  # session rooted elsewhere (a stale shared-tree slot from before worktree-per-session,
  # or a same-named session in another checkout) does NOT own this worktree, so the
  # slot is still ours to claim.
  if [[ -n $slot ]]; then
    local existing_path
    existing_path=$(tmux display-message -p -t "=dev-${match}-${slot}:" '#{session_path}' 2>/dev/null)
    [[ -n $existing_path && ${existing_path:A} == ${cwd:A} ]] && return 1
    print -r -- "$match $slot"; return 0
  fi
  # Else next free slot: first dev-<repo>-<n> with no running session (mirrors `dev`).
  local n=1
  while (( n <= 20 )); do
    tmux has-session -t "=dev-${match}-${n}:" 2>/dev/null || { print -r -- "$match $n"; return 0; }
    (( n++ ))
  done
  return 1
}

# claude — thin wrapper around the real `claude` CLI that makes tpush's
# "background this conversation" flow seamless. Before launching, it arms a
# one-shot sentinel file (path passed to Claude via CLAUDE_TPUSH_ATTACH). When
# tpush — run as /tpush from inside the session — writes an instruction there,
# the wrapper acts on it the moment you leave Claude (/exit or Ctrl-D):
#   • SPAWN<TAB>session<TAB>cwd<TAB>sid — resume $sid into a fresh detached tmux
#     session, THEN attach. The spawn is deferred to here ON PURPOSE: doing it
#     from inside the live session (as tpush used to) means two claude processes
#     own $sid at once, and with no transcript lock they diverge — the
#     backgrounded copy looks frozen. By the time this runs the foreground has
#     fully exited, so $sid has exactly one owner.
#   • ATTACH<TAB>session — the conversation was already backgrounded; just attach.
# No sentinel written → behaves exactly like plain `claude`. The wrapper has to
# own this: tpush runs in Claude's Bash subprocess, which has no TTY to attach
# and can't exit (let alone outlive) its own parent.
claude() { _dev_agent_wrap claude "$@" }
# codex — the same wrapper for Codex CLI, so `t push` from inside a codex session
# hands off the same way (its shell tool inherits the sentinel env like claude's).
codex()  { _dev_agent_wrap codex "$@" }
# _dev_agent_wrap <agent> [args…] — the shared body: arm the sentinel, run the real
# binary, act on the instruction it left. SPAWN's payload carries a trailing agent
# field (session, cwd, sid, agent); a payload without it — an older `t push` — is
# claude, so the two never disagree about who resumes what.
_dev_agent_wrap() {
  local agent="$1"; shift
  _dev_auto_trust "$PWD"
  local sentinel="${TMPDIR:-/tmp}/claude-tpush-attach.$$"
  rm -f "$sentinel"
  CLAUDE_TPUSH_ATTACH="$sentinel" command "$agent" "$@"
  local rc=$?
  if [[ -s "$sentinel" ]]; then
    local payload="$(<"$sentinel")"
    rm -f "$sentinel"
    local verb="${payload%%$'\t'*}" rest="${payload#*$'\t'}" target cwd sid sagent
    case "$verb" in
      SPAWN)
        local -a f=("${(@ps:\t:)rest}")
        target=$f[1]; cwd=$f[2]; sid=$f[3]; sagent=${f[4]:-claude}
        _dev_agent_valid "$sagent" || sagent=claude
        tmux has-session -t "=$target:" 2>/dev/null || _dev_resume_session "$target" "$cwd" "$sid" "$sagent"
        ;;
      ATTACH) target="$rest" ;;
      *)      target="$payload" ;;   # legacy: whole line is a bare session name
    esac
    if [[ -n "$target" ]] && tmux has-session -t "=$target:" 2>/dev/null; then
      echo "Attaching to backgrounded $target…"
      exec tmux attach -t "=$target:"
    fi
  fi
  return $rc
}

# _tpush_claude_pid — walk up from this shell to the controlling `claude` process
# and echo its PID (empty on miss). tpush runs inside Claude's Bash-tool shell,
# whose ancestry is …→ claude → login zsh → tmux; the nearest ancestor named
# `claude` is the live foreground session. tpush signals it to exit so the user
# doesn't have to type /exit — quitting hands control back to the claude() wrapper,
# whose post-exit block resumes this session into tmux. Capped walk; stops at init.
_tpush_claude_pid() { _dev_agent_pid_above }   # agent-agnostic now; the name stays for its callers

# _t_push — the `t push` verb: push a Claude session into a detached background tmux slot.
# Inside Claude (CLAUDE_CODE_SESSION_ID set) it grabs THIS session + $PWD; from a plain shell
# it fzf-picks one (scoped to $PWD, -a for every project, -p to force the picker). Resumes via
# `claude -r` into a dev-named slot; refuses to nest when already in tmux. Inverse of `t pop`.
# User-facing help lives in bin/t (`t push -h`); the t() shim routes -h there, so the -h case
# in the loop below is gone (it could never be reached).
_t_push() {
  local pick= all= a
  for a in "$@"; do
    case "$a" in
      -p|--pick) pick=1 ;;
      -a|--all)  pick=1; all=1 ;;   # --all implies the picker, unfiltered
    esac
  done

  if [[ -n $TMUX && -z $pick ]]; then
    echo "Already inside tmux ($(tmux display-message -p '#S')). Nothing to do." >&2
    return 1
  fi

  local sid cwd agent=claude self_sid
  self_sid=$(_dev_self_sid)
  if [[ -n $self_sid && -z $pick ]]; then
    sid=$self_sid; cwd=$PWD                        # current-session mode (claude or codex)
    agent=$(_dev_self_agent); agent=${agent:-claude}
  else
    local row filter="$PWD"
    [[ -n $all ]] && filter=""                     # --all: every project
    row=$(_claude_sessions_fzf "$filter") || return 1
    [[ -n $row ]] || return 1
    sid=${row%%$'\t'*}
    cwd=${${row#*$'\t'}%%$'\t'*}
  fi

  # Resume-through-sync: a session picked from a synced transcript may be a per-session
  # worktree that does not exist here (it was created on another machine, or reaped after
  # merge). Rebuild it from its branch rather than hard-failing; only a truly unrecoverable
  # dir (non-DEV, or worktree-opt-out repo) errors out.
  local resolved
  resolved=$(_dev_ensure_session_cwd "$cwd") \
    || { echo "Session's directory no longer exists and could not be rebuilt: $cwd" >&2; return 1; }
  cwd=$resolved

  # Already backgrounded? If this exact conversation is running under any slot,
  # reuse it rather than spawning a duplicate. _dev_resume_session stashes
  # CLAUDE_RESUME_ID on each session, so we match on that.
  local repo slot session existing s
  for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
    if [[ "$(tmux show-environment -t "=$s" CLAUDE_RESUME_ID 2>/dev/null | cut -d= -f2)" == "$sid" ]]; then
      existing="$s"; break
    fi
  done

  if [[ -n $existing ]]; then
    session="$existing"
    local rest=${existing#dev-}; slot=${rest##*-}; repo=${rest%-*}
  else
    read -r repo slot < <(_dev_slot_for_cwd "$cwd")
    [[ -n $repo && -n $slot ]] || { echo "Couldn't map $cwd to a dev slot."; return 1; }
    session="dev-${repo}-${slot}"
  fi

  # dev/tread only understand DEV_REPOS keys; for a derived key, point at raw tmux.
  local attach_hint
  if [[ -n "${DEV_REPOS[$repo]}" ]]; then
    attach_hint="Attach: dev $repo $slot    Read log: tread $repo $slot"
  else
    attach_hint="Attach: tmux attach -t $session"
  fi

  # Defer the resume spawn when we're inside Claude and the claude() wrapper is
  # present to do it post-exit. Spawning `claude -r $sid` now — while THIS
  # foreground Claude is still alive on $sid — puts two processes on one
  # transcript with no lock, and they diverge (the backgrounded copy freezes).
  # The wrapper spawns once we've exited and $sid is free; see claude() above.
  local defer=
  [[ -n $self_sid && -n $CLAUDE_TPUSH_ATTACH && -z $existing ]] && defer=1

  if [[ -n $existing ]]; then
    echo "This conversation is already backgrounded in $session."
  elif tmux has-session -t "=$session:" 2>/dev/null; then
    # _dev_slot_for_cwd picks a free slot, so this only trips on a race.
    echo "$session already exists for another session — ${attach_hint#Attach: }"
    return 1
  elif [[ -n $defer ]]; then
    echo "Will resume ${sid[1,8]}… into detached $session ($cwd) on exit."
  else
    _dev_resume_session "$session" "$cwd" "$sid" "$agent"
    echo "Resumed ${sid[1,8]}… in detached $session ($cwd)"
  fi

  # Land you in the session. Three cases:
  if [[ -z $self_sid ]]; then
    # Plain-shell picker mode: we own a real terminal and the picked session
    # isn't live, so spawn (above) + attach straight in. No overlap to worry about.
    echo "$attach_hint"
    tmux attach-session -t "=$session:"
  elif [[ -n $CLAUDE_TPUSH_ATTACH ]]; then
    # Inside Claude via the claude() wrapper: can't attach (or safely spawn) from
    # this Bash subprocess, so hand the wrapper the intent. ATTACH for an already
    # running copy; SPAWN (session+cwd+sid) so it resumes once we've exited.
    if [[ -n $existing ]]; then
      print -r -- "ATTACH"$'\t'"$session" > "$CLAUDE_TPUSH_ATTACH"
    else
      # trailing agent field: the wrapper defaults a missing one to claude
      print -r -- "SPAWN"$'\t'"$session"$'\t'"$cwd"$'\t'"$sid"$'\t'"$agent" > "$CLAUDE_TPUSH_ATTACH"
    fi
    # Auto-exit: signal the controlling `claude` to quit so you don't have to type
    # /exit. The sentinel above is already written and closed (the `>` redirection
    # flushes on completion), so the wrapper's post-exit block will read it and
    # resume this session into tmux. SIGTERM exits Claude cleanly (~2s, terminal
    # restored on the wrapper's tmux attach); the transcript is appended live so
    # only the in-flight tpush turn may be lost (cosmetic). Killing BEFORE any
    # spawn preserves the one-live-owner invariant. If the process can't be found,
    # fall back to the manual hint rather than leaving you stuck.
    local cpid; cpid=$(_tpush_claude_pid)
    if [[ -n $cpid ]]; then
      echo "Backgrounding into $session… (exiting this foreground copy now)"
      kill -TERM "$cpid"
    else
      echo "→ Type /exit (or Ctrl-D) and you'll drop into $session automatically."
    fi
  else
    # Inside Claude without the wrapper (older shell): can't defer, so spawn now
    # and warn — exit immediately, two live copies of one session diverge.
    [[ -z $existing ]] && _dev_resume_session "$session" "$cwd" "$sid" "$agent"
    echo "$attach_hint"
    echo "(Exit this foreground Claude NOW — two live copies of one session diverge.)"
  fi
}

# _t_pop — the `t pop` verb: pull a tmux'd Claude session back to the foreground. Targets the
# dev session for the current dir when bare, `t pop api 3` (dev-api-3), or a full dev-<repo>-
# <slot> name. Kills the tmux session and resumes its conversation here with `claude -r` (the
# inverse of `t push`); run from a plain shell, not inside the session you are popping.
# User-facing help lives in bin/t (`t pop -h`); the t() shim routes -h there.
_t_pop() {
  local session
  if [[ "$1" == dev-* ]]; then
    session="$1"
  elif [[ -n "$1" ]]; then
    local repo="$1" slot="$2"
    # Repo-aware: a lone numeric arg is a SLOT of the repo $PWD is in
    # (`t pop 4` ≡ `t pop <cwd-repo> 4` — see _t_infer_repo).
    if [[ "$repo" == <-> && -z "$slot" ]]; then
      slot=$repo
      repo=$(_t_infer_repo "$slot") || { echo "Not inside a DEV_REPOS dir — name the repo (t session pop <repo> $slot)."; return 1; }
    fi
    # Same-dir sibling aliases (see _dev_kill): a `dev-dot-2` answers `t pop
    # dotfiles 2` when `dot` and `dotfiles` both key ~/code/dotfiles. Without this
    # the lookup misses the local slot and _dev_session_remote_fallback can pop
    # it on another host while the local copy keeps running.
    local -a _palias=( "$repo" )
    if [[ -n ${DEV_REPOS[$repo]:-} ]]; then
      local _pdir=${DEV_REPOS[$repo]} _pk
      for _pk in ${(k)DEV_REPOS}; do
        [[ $_pk == $repo || ${DEV_REPOS[$_pk]} != $_pdir ]] && continue
        _palias+=( $_pk )
      done
    fi
    if [[ -z "$slot" ]]; then                      # first existing slot for repo
      local n=1 a
      while (( n <= 20 )); do
        for a in $_palias; do
          tmux has-session -t "=dev-${a}-${n}:" 2>/dev/null && { repo=$a; slot=$n; break 2; }
        done
        (( n++ ))
      done
    elif (( ${#_palias} > 1 )) && ! tmux has-session -t "=dev-${repo}-${slot}:" 2>/dev/null; then
      local a
      for a in $_palias; do
        [[ $a == $repo ]] && continue
        tmux has-session -t "=dev-${a}-${slot}:" 2>/dev/null && { repo=$a; break; }
      done
    fi
    session="dev-${repo}-${slot}"
  else
    # no args — find the dev session rooted in this repo (its dir or below; the
    # repo dir from _dev_cwd_repo_dir so a subdir works too, else $PWD itself)
    local s d scope; scope=$(_dev_cwd_repo_dir); scope=${scope:-$PWD}
    for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
      d=$(tmux display-message -p -t "=$s:" '#{session_path}')
      [[ $d == $scope || $d == $scope/* ]] && { session="$s"; break; }
    done
    if [[ -z $session ]]; then
      # Nothing live HERE — a slot of this repo may be live on another host. Infer the
      # repo from $PWD and let _dev_remote_delegate resolve/pop it over there (empty
      # slot → it picks the repo's remote slot, fzf-picks if several). Same remote
      # detection the explicit `t pop <repo> <slot>` path gets, now for bare `t pop`.
      local repo; repo=$(_t_infer_repo) && _dev_remote_delegate "$repo" "" pop && return
      echo "No dev session for $scope (here or on any remote host). Pass a repo/slot or session name."; return 1
    fi
  fi

  if ! tmux has-session -t "=$session:" 2>/dev/null; then
    # Not live locally — the slot may be on a remote host. Delegate the pop to its host
    # over ssh -t (shared _dev_session_remote_fallback): it un-tmuxes THERE and you drive
    # it through the ssh TTY — the same semantics as a local pop (closing ssh ends the
    # foreground claude, exactly like a no-tmux local pop). Falls through to the local
    # error only when the slot is live nowhere.
    _dev_session_remote_fallback "$session" pop && return
    echo "No such session: $session"; return 1
  fi
  if [[ -n $TMUX && "$(tmux display-message -p '#S')" == "$session" ]]; then
    echo "You're inside $session right now — run tpop from a different terminal."; return 1
  fi

  # Resume id: the precise CLAUDE_RESUME_ID stamped on the session (by
  # _dev_new_session/_dev_resume_session, or the claude-stamp-tmux SessionStart
  # hook for any other launch path), else the dir's newest transcript. The
  # newest-fallback is only for pre-hook sessions and is ambiguous when several
  # slots share one repo dir (it returns whichever sibling wrote last); the hook
  # is what makes targeting a specific slot reliable.
  local dir sid agent
  dir=$(tmux display-message -p -t "=$session:" '#{session_path}')
  agent=$(_dev_agent_of_session "$session")
  sid=$(tmux show-environment -t "=$session" CLAUDE_RESUME_ID 2>/dev/null | cut -d= -f2)
  [[ -n $sid ]] || sid=$(_dev_agent_newest_sid "$agent" "$dir")   # newest conversation in the dir, per agent
  [[ -n $sid ]] || { echo "Couldn't find a session id for $session ($dir)."; return 1; }

  echo "Popping $session → foreground ($(_dev_agent_resume_cmd "$agent" "${sid[1,8]}…") in $dir)"
  # Capture the live claude PID inside the session so we can wait for it to fully
  # exit before resuming. Claude Code takes NO lock on a session's transcript: if
  # the old (tmux) process and the new (foreground) one are both live on $sid they
  # each append to one .jsonl with no coordination, the conversation diverges, and
  # the copy you aren't driving looks frozen. kill-session only sends SIGHUP, so
  # the old claude needs a beat to trap it, flush, and exit — racing it here was
  # the "popped session stops updating" bug.
  local pane_pid cpid
  pane_pid=$(tmux list-panes -t "=$session:" -F '#{pane_pid}' 2>/dev/null | head -1)
  [[ -n $pane_pid ]] && cpid=$(pgrep -P "$pane_pid" 2>/dev/null | head -1)   # claude = pane shell's child
  tmux kill-session -t "=$session:"
  if [[ -n $cpid ]]; then
    local n=0
    while kill -0 "$cpid" 2>/dev/null && (( n++ < 100 )); do sleep 0.05; done   # wait ≤5s for it to die
    kill -0 "$cpid" 2>/dev/null && \
      echo "warning: $session's $agent ($cpid) didn't exit; resuming anyway — transcript may interleave." >&2
  fi
  cd "$dir" || return 1
  if [[ $agent == codex ]]; then codex resume "$sid"; else claude -r "$sid"; fi
}

# _tbeam_sync_transcript <cwd> <host> — copy a session's transcript dir to <host>
# before it's resumed there. The conversation lives in
# ~/.claude/projects/<cwd-with-non-alnum-as-dashes>/ (/ and . alike → -, so a
# worktree path under .worktrees encodes to --worktrees), and `claude -r <sid>` on the
# far side can only resume what's already on its disk — so the bytes must land
# first. csync/iCloud is the background convergence path; this is the immediate,
# deterministic push for "beam it *now*".
#
# Conflict policy (the one real knob): rsync --update, no --delete. Newer mtime
# wins per file, nothing is removed. The machine you're beaming FROM holds the
# live, freshest copy, so it wins — but if the host somehow had a newer copy
# (you'd worked there more recently) it's preserved rather than clobbered.
_tbeam_sync_transcript() {
  local cwd="$1" host="$2" agent="${3:-claude}" sid="${4:-}"
  if [[ $agent == codex ]]; then
    # One rollout (+ its .origin), sent with its path RELATIVE to ~/.codex (rsync -R
    # and the `/./` anchor) so the far side keeps the YYYY/MM/DD layout codex scans;
    # codex indexes it there on the first resume (verified on 0.154).
    local tx; tx=$(_dev_agent_transcript codex "$sid") || { echo "tbeam: no rollout for ${sid[1,8]}…" >&2; return 1; }
    local croot="${CODEX_HOME:-$HOME/.codex}" rel=${tx#*/.codex/}
    [[ $rel != $tx ]] || rel=${tx#$croot/}
    local -a files=( "$croot/./$rel" )
    [[ -f ${tx%.jsonl}.origin ]] && files+=( "$croot/./${rel%.jsonl}.origin" )
    rsync -azR --update -e ssh "${(@)files}" "$host:.codex/"
    return
  fi
  local enc="${cwd//[^A-Za-z0-9]/-}"             # /a/b → -a-b, Claude's dir scheme (/ AND . → -)
  local src="$HOME/.claude/projects/$enc/"
  [[ -d $src ]] || { echo "tbeam: no transcript dir for $cwd ($src)" >&2; return 1; }
  rsync -az --update --exclude='.DS_Store' -e ssh "$src" "$host:.claude/projects/$enc/"
}

# _tbeam_pull_transcript <cwd> <host> — the mirror of _tbeam_sync_transcript: pull
# a session's transcript dir FROM <host> down to here before it's resumed locally
# (tbeam --from). Same conflict policy — rsync --update, no --delete — only the
# direction flips, so the freshest copy of each file survives whichever way the
# beam flows.
_tbeam_pull_transcript() {
  local cwd="$1" host="$2" agent="${3:-claude}" sid="${4:-}"
  if [[ $agent == codex ]]; then
    # Ask the host where the rollout lives, then mirror its directory locally.
    # Pull named files without -R: macOS rsync 2.6.9 sends an extra .codex parent
    # for /./ paths, which modern receivers correctly reject as unrequested.
    local rtx; rtx=$(ssh -o BatchMode=yes "$host" "zsh -lic '_dev_agent_transcript codex ${(q)sid}'" 2>/dev/null | tail -1)
    [[ $rtx == */rollout-*.jsonl ]] || { echo "tbeam: $host has no rollout for ${sid[1,8]}…" >&2; return 1; }
    local rel=${rtx#*/.codex/}
    # Only Codex session paths are allowed to choose a local destination. This
    # also keeps remote shell arguments plain and excludes traversal segments.
    if [[ ! $rel =~ '^sessions/([A-Za-z0-9_-]+/)*rollout-[A-Za-z0-9._:+-]+\.jsonl$' ]]; then
      echo "tbeam: $host returned an unsupported rollout path" >&2
      return 1
    fi
    local dst="${CODEX_HOME:-$HOME/.codex}/${rel:h}/"
    mkdir -p "$dst" || return 1
    rsync -az --update -e ssh "$host:.codex/$rel" "$host:.codex/${rel%.jsonl}.origin" \
      "$dst" 2>/dev/null \
      || rsync -az --update -e ssh "$host:.codex/$rel" "$dst"
    return
  fi
  local enc="${cwd//[^A-Za-z0-9]/-}"                # /a/b → -a-b, Claude's dir scheme (/ AND . → -)
  local dst="$HOME/.claude/projects/$enc/"
  mkdir -p "$dst"
  rsync -az --update --exclude='.DS_Store' -e ssh "$host:.claude/projects/$enc/" "$dst"
}

# _tbeam_transcript_cwd <transcript.jsonl> — print the working dir a session ran
# in, read from the first `"cwd"` line of its transcript (the real path, not the
# lossy dash-encoded folder name). Used when `tbeam -s <id>` resolves an explicit
# id locally and needs that session's cwd to verify it on the host and cd there.
_tbeam_transcript_cwd() {
  python3 - "$1" <<'PY'
import json, sys
for line in open(sys.argv[1], errors='ignore'):
    if '"cwd"' in line:
        try:
            c = json.loads(line).get('cwd')
            if c: print(c); break
        except ValueError: pass
PY
}

# _tbeam_kill_owner — stop the dev-<repo>-<slot> tmux session on THIS machine that
# owns the session id in $TB_SID, if one is live. This is what turns tbeam from a
# copy into a MOVE: once a session has landed on the far side, its origin-side
# owner is killed so exactly one live claude owns the id — two live owners of one
# transcript have no lock and diverge (the same invariant tpush/tpop protect).
# Shared dotfiles code so the caller can run it on the *remote* origin over ssh;
# the id rides in $TB_SID (env, not args) to dodge nested ssh-quoting, exactly
# like _tbeam_land. kill-session only SIGHUPs claude, but the transcript is
# appended live and already synced, so nothing is lost. Echoes the killed session
# name as its last line (caller reports it); silent, returns nonzero, if the id
# isn't live in any local dev slot.
#
# Slot id resolution must match _dev_session_rows: use _dev_session_sid (registry-
# first, with the stamp validated against the slot's repo) rather than the raw
# CLAUDE_RESUME_ID stamp. Otherwise a stale stamp (e.g. cross-repo pane reuse) on
# the still-live origin slot would cause this to silently fail to find the owner
# while _dev_pull, which already resolved the authoritative id via _dev_session_rows,
# resumes locally — leaving two live owners on the same transcript.
_tbeam_kill_owner() {
  local sid="$TB_SID" s slot_sid
  [[ -n $sid ]] || return 1
  for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
    slot_sid=$(_dev_session_sid "$s")
    if [[ "$slot_sid" == "$sid" ]]; then
      tmux kill-session -t "=$s:" 2>/dev/null && { print -r -- "$s"; return 0; }
    fi
  done
  return 1
}

# _dev_codex_rollout_integrity [sid] — a paginated Codex thread's SQLite history
# is projected from its durable rollout. If the file has since shrunk below the
# projection checkpoint (for example, a stale cross-host sync replaced it), a beam
# would move only the stale file and silently lose completed turns. Read both DBs
# without modifying them; older installs without these tables retain legacy beam.
_dev_codex_rollout_integrity() {
  local sid=${1:-${TB_SID:-}}
  [[ -n $sid ]] || { print -u2 -- 'tbeam: missing Codex thread id for rollout check'; return 1; }
  python3 - "${CODEX_HOME:-$HOME/.codex}" "$sid" <<'PY'
import sqlite3
import sys
from pathlib import Path

home, sid = Path(sys.argv[1]), sys.argv[2]


def read_one(db_path, query):
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.5) as db:
            return db.execute(query, (sid,)).fetchone()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if 'no such table' in message or (
            db_path.name == 'state_5.sqlite' and 'no such column: history_mode' in message
        ):
            return None
        raise


try:
    thread = read_one(home / 'state_5.sqlite',
                      'select rollout_path, history_mode from threads where id=?')
    if thread is None or thread[1] != 'paginated':
        sys.exit(0)
    checkpoint = read_one(home / 'thread_history_1.sqlite',
                          'select next_rollout_byte_offset from thread_history_projection_state where thread_id=?')
    if checkpoint is None:
        sys.exit(0)
    offset = checkpoint[0]
    if not isinstance(offset, int) or offset < 0:
        raise ValueError('invalid SQLite rollout checkpoint')
    if not thread[0]:
        raise ValueError('missing paginated rollout path')
    rollout = Path(thread[0])
    if not rollout.is_absolute():
        rollout = home / rollout
    size = rollout.stat().st_size
    if size < offset:
        print(f'tbeam: Codex rollout is {size} bytes, behind its SQLite checkpoint at {offset}; '
              'keep the source here and restore the matching rollout before beaming', file=sys.stderr)
        sys.exit(1)
except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
    print(f'tbeam: could not verify paginated Codex rollout: {exc}', file=sys.stderr)
    sys.exit(1)
PY
}

# _tbeam_land — runs ON the destination host. It's defined in the shared dotfiles
# (so it exists on every machine); the laptop invokes it over ssh with the work
# passed in the environment: TB_CWD, TB_SID, TB_MODE (tmux|fg), TB_ATTACH.
# tmux mode reuses the host's own _dev_resume_session, so what lands is a
# first-class dev-<repo>-<slot> that dev/tread/tpop already understand. fg mode
# just resumes the conversation in this ssh session's foreground.
_tbeam_land() {
  # Where does it land? Usually TB_CWD — the origin's per-session worktree path, same root
  # on every host — but when THIS host's same-numbered slot is another session's (live,
  # dirty, or diverged: the collision case), _dev_beam_land_cwd relands into a fresh slot,
  # relocating the already-synced transcript with it (the sender ran _tbeam_sync_transcript
  # before invoking us, so the sid's files are on disk either way).
  local agent=${TB_AGENT:-claude}; _dev_agent_valid "$agent" || agent=claude
  local land; land=$(_dev_beam_land_cwd "$TB_CWD" "$TB_SID" "" "$agent") || return 1
  if [[ $land == "$TB_CWD" ]]; then
    # Worktree mode: if TB_CWD does not exist here yet, materialize the slot's worktree
    # from its branch on origin (else fresh off main) before landing. Uncommitted edits
    # ride along too: the origin commit-all + pushed before the move, and the
    # _dev_worktree_beam_sync below fast-forwards this worktree to them (no-op for a
    # freshly created one; the real work is when it pre-existed and was reused stale).
    if [[ ! -d $TB_CWD ]]; then
      local _br _bs; _br=$(_dev_repo_of_dir "$TB_CWD"); _bs=${_br#*$'\t'}; _br=${_br%%$'\t'*}
      [[ -n $_br && -n $_bs ]] && _dev_worktree_enabled "$_br" && _dev_worktree_create "$_br" "$_bs" >/dev/null
    fi
    _dev_worktree_beam_sync "$TB_CWD"
  fi
  cd "$land" 2>/dev/null || { echo "tbeam: $land not found on ${HOST:-this host}" >&2; return 1; }
  if [[ "$TB_MODE" == fg ]]; then                # owns this ssh TTY; dies with it
    if [[ $agent == codex ]]; then exec codex resume "$TB_SID"; else exec claude -r "$TB_SID"; fi
  fi
  local repo slot session
  read -r repo slot < <(_dev_slot_for_cwd "$land")
  [[ -n $repo && -n $slot ]] || { echo "tbeam: couldn't map $land to a dev slot" >&2; return 1; }
  session="dev-${repo}-${slot}"
  _dev_resume_session "$session" "$land" "$TB_SID" "$agent"
  if [[ -n $TB_ATTACH ]]; then
    exec tmux attach -t "=$session:"              # drop the ssh caller straight in
  fi
  print -r -- "$session"                        # last line: caller reads it for the hint
}

# _t_beam — the `t beam` verb: move a Claude session between machines. Like `t push`, but across
# machines — and a MOVE, not a copy: after the session lands on the far side the origin's live
# owner is stopped so exactly one claude owns the id (the invariant `t push`/`t pop` protect).
# Both directions live here: by default it SENDS THIS conversation (or an fzf pick) to a host
# (--host, default $TBEAM_HOST), lands it in a detached dev slot, and ssh's you in; `--from
# <host>` (or `--here` to auto-find the host) RECEIVES — pulls a session living on another host
# down into a local slot. To merely ATTACH a remote slot without moving it, use `t open <repo>
# <slot>`. The repo must exist at the same ~/code path on both; the transcript is rsync'd before
# it resumes. User-facing help lives in bin/t (`t beam -h`); the t() shim routes -h there.
_t_beam() {
  # while/shift (not for-in) so -s/--session can consume the following token as
  # its value; the `=`-joined forms (-s=… / --session=…) work too.
  local fg= detach= pick= all= host= sid_arg= from_host= here=
  local -a pos=()
  while (( $# )); do
    case "$1" in
      -f|--fg)              fg=1 ;;
      -d|--detach)          detach=1 ;;
      -p|--pick)            pick=1 ;;
      -a|--all)             pick=1; all=1 ;;
      -s|--session|--id)    shift; sid_arg="$1" ;;
      -s=*|--session=*|--id=*) sid_arg="${1#*=}" ;;
      --from)               shift; from_host="$1" ;;
      --from=*)             from_host="${1#*=}" ;;
      --here)               here=1 ;;
      -*)                   echo "tbeam: unknown flag $1" >&2; return 1 ;;
      *)                    pos+=("$1") ;;
    esac
    shift
  done

  # RECEIVE (auto-host): --here is "bring it back" without naming the host — it
  # auto-detects which $REMOTE_HOSTS box the slot is live on (the same probe `t open`
  # uses for remote attach, _dev_remote_resolve) and then runs the --from pull. The
  # inverse of a send when you do not want to remember where it went. Repo/slot are
  # optional and just SCOPE the probe: bare `t beam --here` fzf-picks among EVERY live
  # remote session; `t beam <repo> [slot]` (or a lone slot in a repo dir) narrows it;
  # one match → no prompt. Resolves to a concrete host+repo+slot, then shares the
  # --from machinery below by setting from_host (so the MOVE + one-live-owner hold).
  if [[ -n $here && -z $from_host ]]; then
    command -v rsync >/dev/null 2>&1 || { echo "tbeam: rsync not found" >&2; return 1; }
    local repo_arg=${pos[1]} slot_arg=
    [[ ${pos[2]} == <-> ]] && slot_arg=${pos[2]}
    # Repo-aware (mirrors --from): a lone numeric positional is a SLOT of the cwd repo.
    # Bare `--here` (no positionals) stays empty so _dev_remote_resolve probes ALL remotes —
    # do NOT infer from $PWD here, or standing in any DEV_REPOS dir would silently narrow
    # the picker to that repo and hide every other remote session.
    if [[ $repo_arg == <-> && -z $slot_arg ]]; then slot_arg=$repo_arg; repo_arg=$(_t_infer_repo); fi
    # _dev_remote_resolve returns "<host>\t<repo>\t<slot>" (fzf-picks if several live).
    local res; res=$(_dev_remote_resolve "$repo_arg" "$slot_arg") || return 1
    from_host=${res%%$'\t'*}
    repo_arg=${${res#*$'\t'}%%$'\t'*}
    slot_arg=${res##*$'\t'}
    [[ -n $CLAUDE_CODE_SESSION_ID ]] && fg=
    local target="${REMOTE_HOSTS[$from_host]:-$from_host}"
    _dev_pull "$from_host" "$target" "$repo_arg" "$slot_arg" "$fg"
    return
  fi

  # RECEIVE: --from <host> pulls a session FROM that host onto THIS machine — the exact
  # mirror of the default send, and a MOVE for the same one-live-owner reason (_dev_pull
  # stops the origin copy on <host> once the transcript lands here). Positionals are
  # [repo [slot]] only (the host is named by --from, never a positional); the repo is
  # required (it names which remote slot to pull). -f resumes inline here instead of in
  # a detached dev slot — but inside Claude there is no TTY to attach, so force a slot.
  if [[ -n $from_host ]]; then
    command -v rsync >/dev/null 2>&1 || { echo "tbeam: rsync not found" >&2; return 1; }
    local repo_arg=${pos[1]} slot_arg=
    [[ ${pos[2]} == <-> ]] && slot_arg=${pos[2]}
    # Repo-aware: bare/numeric positionals mean the repo $PWD is in (`t beam 4
    # --from mini` pulls ITS slot 4 here). Alias-only inference (no slot passed to
    # _t_infer_repo): the slot lives on the REMOTE host, so a same-numbered local
    # session's name would be a coincidence, not evidence.
    if [[ $repo_arg == <-> && -z $slot_arg ]]; then slot_arg=$repo_arg; repo_arg=$(_t_infer_repo); fi
    [[ -z $repo_arg ]] && repo_arg=$(_t_infer_repo)
    [[ -n $repo_arg ]] || { echo "tbeam: --from needs a <repo> to pull (e.g. t session move dot 1 --from $from_host)" >&2; return 1; }
    [[ -n $CLAUDE_CODE_SESSION_ID ]] && fg=
    local target="${REMOTE_HOSTS[$from_host]:-$from_host}"
    _dev_pull "$from_host" "$target" "$repo_arg" "$slot_arg" "$fg"
    return
  fi

  # Positional grammar: [repo [slot]] [host], matching the dev/tplan/tpop family so
  # `tbeam api 1` lines up with `tpop api 1`. The first positional is a <repo> only
  # when it's a DEV_REPOS key — that's what disambiguates it from a bare host
  # (`tbeam mini` still means host 'mini'). An optional numeric slot follows, then
  # an optional explicit host. (This is the SEND path; the OTHER way — pull a session
  # FROM a host onto this machine — is `--from <host>`, handled just above.)
  local repo_arg= slot_arg=
  if [[ -n ${pos[1]} && -n ${DEV_REPOS[${pos[1]}]} ]]; then
    repo_arg=${pos[1]}
    if [[ ${pos[2]} == <-> ]]; then    # numeric → slot, then optional host
      slot_arg=${pos[2]}; host=${pos[3]}
    elif [[ -z $sid_arg && ${pos[2]} == *:* ]]; then
      # `t beam <repo> <repo>:<id>` — the second token is a FOREGROUND label from
      # `t ls` (redundant repo prefix); beam that session by id. pos[3] is the host.
      # Clear repo_arg so the -s id path resolves it (not the dev-slot path, which
      # is tried first whenever repo_arg is set).
      repo_arg=; sid_arg=${pos[2]##*:}; host=${pos[3]}
    else                               # no slot → second positional is the host
      host=${pos[2]}
    fi
  elif [[ -z $sid_arg && ${pos[1]} == *:* ]]; then
    # A bare FOREGROUND label — the `<repo>:<id>` shown by `t ls` for a claude run
    # directly in a terminal (e.g. ff:727a2a8c). It has no dev slot, so route it
    # through the -s id path below (the repo prefix is stripped); pos[2] is the host.
    # A colon is unambiguous here (hosts/repos/slots never contain one); a bare id
    # with no colon collides with a host name, so use `-s <id>` for that.
    sid_arg=${pos[1]##*:}; host=${pos[2]}
  elif [[ ${pos[1]} == <-> ]]; then
    # Repo-aware: a lone numeric first positional is a SLOT of the repo $PWD is in
    # (`t beam 4` ≡ `t beam <cwd-repo> 4`, optional host after — see _t_infer_repo).
    slot_arg=${pos[1]}
    repo_arg=$(_t_infer_repo "$slot_arg") || { echo "tbeam: not inside a DEV_REPOS dir — name the repo (t session move <repo> ${pos[1]})" >&2; return 1; }
    host=${pos[2]}
  else
    host=${pos[1]}
  fi
  host="${host:-${TBEAM_HOST:-}}"
  if [[ -z "$host" ]]; then
    echo "tbeam: no host given and TBEAM_HOST is unset (set it in ${T_LOCAL_RC})" >&2
    return 1
  fi
  command -v rsync >/dev/null 2>&1 || { echo "tbeam: rsync not found" >&2; return 1; }

  # Resolve the session id + its working dir (mirrors tpush). Four ways:
  #   • <repo> [slot] — a dev slot, resolved like tplan/tpop: tmux session →
  #     its stamped CLAUDE_RESUME_ID (newest-transcript fallback for pre-hook
  #     sessions). The origin is a local dev slot, so this is a MOVE that
  #     kill-sessions it once landed (the self_move=… block below).
  #   • -s <id>  — an explicit id (full, or a unique prefix like the 8 chars the
  #     picker/tbeam show): resolve it locally to its transcript + recorded cwd,
  #     skipping both the picker and current-session mode. Lets you re-beam a
  #     known id without picking. A `<repo>:<id>` FOREGROUND label from `t ls`
  #     passed as a positional (e.g. `t beam ff:727a2a8c`) feeds this same path
  #     (the repo prefix is stripped above); its origin is a foreground claude, so
  #     the kill-block below stops it by pid instead of kill-session.
  #   • inside Claude (no -p) — THIS conversation + $PWD.
  #   • otherwise — fzf-pick (scoped to $PWD; -a for every repo).
  # self_move = "the origin is THIS foreground claude" (true current-session
  # move): only then do we SIGTERM ourselves to complete the move. For any other
  # origin (a dev slot) we kill-session it instead — see the two blocks below.
  local sid cwd self_move= agent=claude self_sid
  self_sid=$(_dev_self_sid)
  if [[ -n $repo_arg ]]; then
    local slot=$slot_arg
    if [[ -z $slot ]]; then                         # first existing slot for repo
      local n=1
      while (( n <= 20 )); do
        tmux has-session -t "=dev-${repo_arg}-${n}:" 2>/dev/null && { slot=$n; break; }
        (( n++ ))
      done
    fi
    local session="dev-${repo_arg}-${slot}"
    tmux has-session -t "=$session:" 2>/dev/null || { echo "tbeam: no such session: $session" >&2; return 1; }
    # Authoritative id (registry-first, stamp validated against the slot's repo) —
    # must match _tbeam_kill_owner's resolution, or a stale cross-repo stamp on the
    # origin slot would beam transcript X while the slot is actually running Y,
    # leaving the live slot un-killed and two owners on Y after the remote resumes.
    local dir; dir=$(tmux display-message -p -t "=$session:" '#{session_path}' 2>/dev/null)
    agent=$(_dev_agent_of_session "$session")
    sid=$(_dev_session_sid "$session" "$dir")
    [[ -n $sid ]] || sid=$(_dev_agent_newest_sid "$agent" "$dir")   # pre-hook fallback: newest conversation in the dir
    [[ -n $sid ]] || { echo "tbeam: couldn't find a session id for $session" >&2; return 1; }
    # The session's REAL dir is its tmux session_path ($dir) — the per-session worktree for
    # a worktree repo, the dev clone for an opt-out one. Use it (not the canonical DEV_REPOS
    # dir) so the transcript rsync + TB_CWD + the worktree push below all target where the
    # session actually runs; fall back to the repo dir only if session_path is unreadable.
    cwd=${dir:-${DEV_REPOS[$repo_arg]}}
  elif [[ -n $sid_arg ]]; then
    setopt local_options null_glob
    local -a tx=( "$HOME/.claude/projects"/*/"$sid_arg"*.jsonl )
    local crow
    if (( ${#tx} == 0 )) && crow=$(_codex_thread_lookup "$sid_arg") && [[ -n $crow ]]; then
      # a codex thread id (exact — codex ids are not prefix-matched here): its cwd is
      # the index's, which follows the last resume
      agent=codex; sid=$sid_arg
      cwd=${${crow#*$'\t'}#*$'\t'}; cwd=${cwd%%$'\t'*}
    else
      (( ${#tx} ))      || { echo "tbeam: no local session matching '$sid_arg'" >&2; return 1; }
      (( ${#tx} == 1 )) || { echo "tbeam: '$sid_arg' matches ${#tx} sessions — use a longer prefix" >&2; return 1; }
      sid=${${tx[1]:t}%.jsonl}
      cwd=$(_tbeam_transcript_cwd "$tx[1]")
    fi
    [[ -n $cwd ]] || { echo "tbeam: couldn't read the working dir for $sid" >&2; return 1; }
  elif [[ -n $self_sid && -z $pick ]]; then
    sid=$self_sid; cwd=$PWD                         # current-session mode (claude or codex)
    agent=$(_dev_self_agent); agent=${agent:-claude}
  else
    local row filter="$PWD"
    [[ -n $all ]] && filter=""                      # --all: every project
    row=$(_claude_sessions_fzf "$filter") || return 1
    [[ -n $row ]] || return 1
    sid=${row%%$'\t'*}
    cwd=${${row#*$'\t'}%%$'\t'*}
  fi
  if _dev_app_slot_reserved "$cwd"; then
    print -u2 -- "tbeam: $cwd is reserved for the Codex desktop app; close it and release the reservation before moving its conversation"
    return 1
  fi
  [[ $agent != codex ]] || _dev_codex_rollout_integrity "$sid" || return 1
  [[ $sid == "$self_sid" && -n $self_sid ]] && self_move=1
  [[ -n $self_sid ]] && detach=1                    # no TTY in an agent's tool subprocess to ssh -t into
  # Resume-through-sync for the picker is handled on the FAR side: _tbeam_land materializes
  # a missing per-session worktree from its branch before resuming. Don't rebuild it here —
  # the send path only needs the cwd as a path string (for the transcript rsync + TB_CWD),
  # and creating a local worktree would leave a stray checkout behind on a move. For the
  # same reason, only require the cwd to pre-exist on the host for NON-worktree paths;
  # worktree paths (under $DEV_WORKTREE_ROOT) are materialized by _tbeam_land.
  if [[ -z $DEV_WORKTREE_ROOT || $cwd != $DEV_WORKTREE_ROOT/*/* ]]; then
    if ! ssh "$host" "test -d ${(q)cwd}" 2>/dev/null; then
      echo "tbeam: $cwd doesn't exist on $host — clone/sync the repo there first." >&2
      return 1
    fi
  fi

  echo "⟳ Beaming ${sid[1,8]}… ($cwd) → $host"
  # It's a MOVE, not a copy: stop the origin BEFORE snapshotting code + transcript,
  # so a still-live claude can't keep editing files in the worktree (the push below
  # would miss those edits) or appending to the transcript (the sync below would
  # miss the tail) between the snapshot and the move — leaving the destination to
  # resume with stale code despite a newer transcript. Two cases for "the origin":
  #   • a LOCAL dev slot (picker mode, or an -s id that's live in a slot) — kill it
  #     now, before the blocking ssh -t branches below take over the terminal. We
  #     aren't attached to it, so this is safe. (self_move excludes the case where
  #     that slot is the very claude we're running inside.)
  #   • THIS foreground claude (self_move — a current-session move) — can't
  #     kill-session it; instead we SIGTERM it at the very end (see the detached
  #     branch), mirroring tpush's auto-exit. No race here either: claude is
  #     blocked executing this very command, so it can't edit files mid-beam.
  if [[ -z $self_move ]]; then
    local killed; killed=$(TB_SID=$sid _tbeam_kill_owner)
    if [[ $killed == dev-* ]]; then
      echo "✂ Stopped the local copy ($killed) — moved to $host"
    else
      # No dev slot owned the id — a FOREGROUND claude (the `<repo>:<id>` rows in
      # `t ls`) might. Stop it too, so beaming a foreground session is a real MOVE
      # and not two live owners: claude takes no transcript lock, so two resumers of
      # one id diverge (the invariant tpush/tpop/_dev_adopt_fg protect). SIGTERM the
      # pid via the registry, then wait for it to exit before $host takes over.
      local fgpid; fgpid=$(_dev_pid_for_sid "$sid")
      if [[ -n $fgpid ]]; then
        kill -TERM "$fgpid" 2>/dev/null
        local n=0
        while kill -0 "$fgpid" 2>/dev/null && (( n++ < 100 )); do sleep 0.05; done
        if kill -0 "$fgpid" 2>/dev/null; then
          # Owner ignored SIGTERM (or is wedged) — warn rather than claim success,
          # mirroring _dev_adopt_fg: the remote is about to take over, so two live
          # claudes may briefly share the id and the transcript can interleave.
          echo "warning: local foreground copy (pid $fgpid) didn't exit; $host resuming anyway — transcript may interleave." >&2
        else
          echo "✂ Stopped the local foreground copy (pid $fgpid) — moved to $host"
        fi
      fi
    fi
  fi
  if ! _dev_worktree_beam_push "$cwd" "$host"; then   # carry uncommitted worktree edits ahead of the move
    echo "⚠ couldn't fully commit/push $cwd — $host may resume with stale code (the edits stay here)" >&2
  fi
  _tbeam_sync_transcript "$cwd" "$host" "$agent" "$sid" || return 1

  # Foreground mode: resume straight in the ssh session (needs a real terminal).
  if [[ -n $fg ]]; then
    [[ -z $detach ]] || { echo "tbeam: -f needs a terminal; can't combine with -d / inside Claude." >&2; return 1; }
    ssh -t "$host" "TB_CWD=${(q)cwd} TB_SID=${(q)sid} TB_AGENT=${(q)agent} TB_MODE=fg zsh -lic _tbeam_land"
    return
  fi

  # tmux mode + auto-attach: -t lets _tbeam_land exec us into the landed session.
  if [[ -z $detach ]]; then
    ssh -t "$host" "TB_CWD=${(q)cwd} TB_SID=${(q)sid} TB_AGENT=${(q)agent} TB_MODE=tmux TB_ATTACH=1 zsh -lic _tbeam_land"
    return
  fi

  # tmux mode, detached: capture the landed session name, print an attach hint.
  local session
  session=$(ssh "$host" "TB_CWD=${(q)cwd} TB_SID=${(q)sid} TB_AGENT=${(q)agent} TB_MODE=tmux zsh -lic _tbeam_land" | tail -1)
  [[ -n $session ]] || { echo "tbeam: landing on $host failed." >&2; return 1; }
  echo "✓ Running on $host as $session"
  local rest=${session#dev-} repo slot
  slot=${rest##*-}; repo=${rest%-*}
  if [[ -n "${DEV_REPOS[$repo]}" ]]; then
    echo "  Attach: t session open $repo $slot    (or pull it back: t session move $repo $slot --from $host)"
  else
    echo "  Attach: ssh $host -t \"zsh -lic 'tmux attach -t $session'\""
  fi

  # current-session move (self_move): the origin is THIS foreground claude, so the
  # kill-block up top skipped it. Now that the session is live on $host, exit here
  # so the id keeps one owner — mirrors tpush's auto-exit: SIGTERM the controlling
  # claude (it flushes and quits in ~2s; transcript is appended live, so only the
  # in-flight turn is lost). The hints above are already flushed. If the process
  # can't be found, fall back to asking for a manual /exit. (An explicit -s id that
  # isn't our own session is NOT self_move, so we never kill the wrong claude.)
  if [[ -n $self_move ]]; then
    local cpid; cpid=$(_tpush_claude_pid)
    if [[ -n $cpid ]]; then
      echo "Moved to $host — exiting this copy so it has one owner."
      kill -TERM "$cpid"
    else
      echo "→ This session now lives on $host. Type /exit here so two copies don't diverge."
    fi
  fi
}

# (on removed — `t on <host> [cmd…]` is reimplemented natively in bin/t, with the
# same two-layer quoting and `zsh -lic` remote contract. The per-host shorthand
