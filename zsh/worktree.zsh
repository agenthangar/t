# _dev_worktree_enabled <repo> — true unless this repo (or the global default) opts out.
_dev_worktree_enabled() {
  local v=${DEV_WORKTREE[$1]:-}
  [[ -n $v ]] || v=${DEV_WORKTREE_DEFAULT:-1}
  [[ $v != 0 && $v != no && $v != off && $v != false ]]
}
# Folder labels are presentation; slot IDs and branch names stay numeric.
_dev_worktree_slot() {
  local base="$1" leaf="$2"
  [[ $leaf == "$base-"* ]] && leaf=${leaf#"$base-"}
  [[ $leaf == <-> ]] || return 1
  print -r -- "$leaf"
}
_dev_worktree_paths() {
  local base=${DEV_REPOS[$1]:t}
  [[ -n $base && $2 == <-> ]] || return 1
  print -rl -- "$DEV_WORKTREE_ROOT/$base/$2" "$DEV_WORKTREE_ROOT/$base/$base-$2"
}
# Reuse existing paths, including legacy numeric directories. Never move them.
_dev_worktree_path() {
  local -a candidates present
  candidates=("${(@f)$(_dev_worktree_paths "$1" "$2")}")
  (( $#candidates == 2 )) || return 1
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -e $candidate || -L $candidate ]] && present+=("$candidate")
  done
  if (( $#present > 1 )); then
    print -u2 -- "t: both worktree layouts exist for $1 $2; inspect ${candidates[1]} and ${candidates[2]} before continuing (no worktrees moved)"
    return 1
  fi
  print -r -- "${present[1]:-${candidates[2]}}"
}
# _dev_worktree_branch <repo> <slot> — the slot's dedicated branch.
_dev_worktree_branch() { print -r -- "dev/${DEV_REPOS[$1]:t}-$2" }

# Resolve fetched origin refs only; never branch from an arbitrary local checkout.
# Clones record custom defaults in origin/HEAD. Older checkouts may lack it.
_dev_origin_base() {
  local repodir="$1" ref
  ref=$(git -C "$repodir" symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null)
  if [[ $ref == refs/remotes/origin/* && $ref != refs/remotes/origin/HEAD ]] &&
     git -C "$repodir" rev-parse --verify -q "${ref}^{commit}" >/dev/null 2>&1; then
    print -r -- "${ref#refs/remotes/}"; return 0
  fi
  for ref in origin/main origin/master; do
    if git -C "$repodir" rev-parse --verify -q "refs/remotes/${ref}^{commit}" >/dev/null 2>&1; then
      print -r -- "$ref"; return 0
    fi
  done
  return 1
}

# Read-only transfer negotiation. The wire cwd is immutable: hosts with different
# roots must attach at the source or use history's verified fresh-workspace resume.
_dev_worktree_accept_cwd() {
  local cwd="$1" row repo slot resolved
  row=$(_dev_repo_of_dir "$cwd") || {
    print -u2 -- "t: destination cannot map $cwd; align registered repository/worktree roots, or attach on the source host"; return 1
  }
  repo=${row%%$'\t'*}; slot=${row#*$'\t'}
  [[ -n $slot ]] || {
    print -u2 -- 't: destination does not map this path to a worktree slot; align configured roots or attach on the source host'; return 1
  }
  local -a candidates; candidates=("${(@f)$(_dev_worktree_paths "$repo" "$slot")}")
  [[ $cwd == $candidates[1] || $cwd == $candidates[2] ]] || {
    print -u2 -- 't: transfer requires the worktree root, not a subdirectory'; return 1
  }
  resolved=$(_dev_worktree_path "$repo" "$slot") || return 1
  [[ $resolved == "$cwd" || ( ! -e $resolved && ! -L $resolved ) ]] || {
    print -u2 -- 't: destination slot uses another folder layout; use t session resume or an unused slot'; return 1
  }
  _dev_worktree_enabled "$repo"
}

_dev_worktree_remote_accept() {
  local target="$1" cwd="$2" snippet
  # Require this capability even for numeric paths: an old receiver cannot
  # detect that a named sibling on its disk owns the same internal slot.
  snippet='(( ${+functions[_dev_worktree_accept_cwd]} )) && _dev_worktree_accept_cwd '
  snippet+=${(q)cwd}
  ssh "$target" "zsh -lic ${(q)snippet}" || {
    print -u2 -- "t: $target could not accept the exact worktree path; update t on both hosts and verify their configured roots before retrying (source was not stopped)"
    return 1
  }
}

# Retired integrations may have left a reservation in private Git metadata.
# Do not freshen or sweep that workspace. Exact saved CLI resume still verifies
# the conversation's writer locks; no new reservations are created.
_dev_legacy_workspace_reserved() {
  local gitdir
  gitdir=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
  [[ -f "$gitdir/t-app-slot" ]]
}

# A t issue launch reserves its freshly created worktree until the task and
# ownership stamp are installed. Other open/resume paths must not race it.
_dev_issue_preparing() {
  local gitdir reservation="${1:h}/.${1:t}.history-reservation"
  if [[ -f $reservation && $(<$reservation) != ${T_HISTORY_TOKEN:-} ]]; then return 0; fi
  gitdir=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
  [[ -f "$gitdir/t-issue-preparing" ]]
}

# _dev_repo_slots <repo> — every slot NUMBER this repo has any trace of, ascending:
# a live tmux session (under ANY sibling alias keying the repo's dir — dev-dot-3 and
# dev-dotfiles-3 are one slot), a worktree dir on disk, or a saved-transcript project
# dir (a slot whose worktree was swept is still resumable). Replaces `t resume`'s old
# hardcoded 1..20 scan: slot numbers grow without bound (a busy repo routinely runs
# past 20 — ff was on 24), and a fixed ceiling hid those slots BOTH ways — live ones
# never appeared as "● active", and exiting one made its conversation vanish rather
# than becoming resumable, which read as "t resume loses my sessions". Prints nothing
# and returns 1 for an unknown repo or one with no slots at all.
_dev_repo_slots() {
  setopt local_options null_glob bare_glob_qual
  local repo="$1" dir="${DEV_REPOS[$1]:-}"
  [[ -n $dir ]] || return 1
  local base=${dir:t} enc n p k
  local -A seen
  for k in ${(k)DEV_REPOS}; do
    [[ ${DEV_REPOS[$k]} == $dir ]] || continue
    for n in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^dev-${k}-")"}; do
      n=${n##*-}; [[ $n == <-> ]] && seen[$n]=1
    done
  done
  for p in ${DEV_WORKTREE_ROOT}/${base}/*(N/); do
    n=$(_dev_worktree_slot "$base" "${p:t}") && seen[$n]=1
  done
  enc=${${:-${DEV_WORKTREE_ROOT}/${base}}//[^A-Za-z0-9]/-}
  for p in "$HOME/.claude/projects/${enc}-"*(N/); do
    n=$(_dev_worktree_slot "${base//[^A-Za-z0-9]/-}" "${${p:t}#${enc}-}") && seen[$n]=1
  done
  # codex: its thread index knows which worktree paths have had a conversation
  # (a codex-only slot whose worktree was swept leaves no claude project dir)
  local row c
  for row in ${(f)"$(_codex_threads prefix "${DEV_WORKTREE_ROOT}/${base}/")"}; do
    c=${${row#*$'\t'}#*$'\t'}; c=${c%%$'\t'*}
    n=$(_dev_worktree_slot "$base" "${${c#${DEV_WORKTREE_ROOT}/${base}/}%%/*}") && seen[$n]=1
  done
  (( $#seen )) || return 1
  print -rl -- ${(no)${(k)seen}}
}

# _dev_worktree_create <repo> <slot> — idempotently materialize the slot's worktree and
# print its path. An already-present worktree is reused (reattach stays free: the one
# check it pays is _dev_worktree_freshen, below — a tmux probe and a `git status`, with
# the gh/fetch cost only on a dead+clean tree). A fresh slot branches off the just-fetched
# origin default branch. A slot whose tmux died but whose branch lingered resumes that branch ONLY
# WHILE IT STILL CARRIES UNMERGED WORK: once the branch's PR has landed it is a dead line,
# and resuming it starts a "fresh" session N commits behind origin/main with the merged
# commit still riding on top (hive-4: 42 behind, PR #13's commit on the branch — hive
# does not delete branches on merge, so `origin/dev/hive-4` outlived the sweep, which had
# reaped only the LOCAL worktree+branch, and the origin-only rule below checked the
# corpse out as the new session's start). A merged lingering branch is therefore dropped
# (locally, and on origin via _dev_worktree_drop_remote — the delete GitHub itself does
# under delete-branch-on-merge, and without which the slot's first push is rejected as
# non-fast-forward in a squash-merge repo) and the slot is cut off the origin default like a
# never-used one. Unmerged lingering work is resumed exactly as before — the sweep's
# "any doubt = not merged" rule (_dev_branch_merged) is what decides, so nothing
# unmerged is ever dropped. On failure (e.g. the branch is checked out in another
# worktree — should not happen with one branch per slot) it prints nothing and returns
# 1 so the caller can refuse. Runs git against the repo's (possibly shared) .git via -C.
_dev_worktree_create() {
  _t_with_progress 'Preparing session worktree' _dev_worktree_create_impl "$@"
}

_dev_worktree_create_impl() {
  local repo="$1" slot="$2"
  local repodir="${DEV_REPOS[$repo]}"
  local wt br; wt="$(_dev_worktree_path "$repo" "$slot")" || return 1
  # An exact resume/transfer may rebuild either supported layout, never rename it.
  if [[ -n ${3:-} ]]; then
    local -a candidates; candidates=("${(@f)$(_dev_worktree_paths "$repo" "$slot")}")
    [[ $3 == $candidates[1] || $3 == $candidates[2] ]] || return 1
    [[ $wt == "$3" || ( ! -e $wt && ! -L $wt ) ]] || {
      print -u2 -- 't: this slot already uses another worktree path; use t session resume to select the exact conversation'; return 1
    }
    wt=$3
  fi
  br="$(_dev_worktree_branch "$repo" "$slot")"
  if [[ -e "$wt/.git" ]]; then          # already materialized → reuse (idempotent)
    _dev_issue_preparing "$wt" && { print -u2 't: issue launch is reserved; rerun t issue start <issue> to recover it'; return 1; }
    if ! _dev_legacy_workspace_reserved "$wt"; then
      _dev_worktree_freshen "$repo" "$slot" "$wt" "$br"
    fi
    _dev_auto_trust "$wt"
    print -r -- "$wt"; return 0
  fi
  # A dir with NO .git here is debris, never a worktree: the sweep removed the tree but
  # a process still rooted in it (the slot's vite dev server, rewriting .vite/deps)
  # recreated the path. `git worktree add` refuses a non-empty target, which used to
  # fail this function and drop the session into the SHARED tree. Move it aside —
  # nothing tracked can live in it, but nothing is deleted either.
  if [[ -d $wt ]]; then
    local debris="${wt}.debris-${EPOCHSECONDS}"
    if mv "$wt" "$debris" 2>/dev/null; then
      print -r -- "⚠ $wt existed without a worktree (leftover files, e.g. a dev server still running there) — moved to $debris" >&2
    fi
  fi
  git -C "$repodir" worktree prune 2>/dev/null    # clear any stale registration first
  # --prune is load-bearing: without it a branch GitHub already deleted on merge lives on
  # as a stale refs/remotes/origin/<br>, and the origin-only arm below would resurrect
  # THAT — so the merged-corpse bug is not hive-only, it reaches every delete-on-merge
  # repo whose remote-tracking refs were never pruned.
  git -C "$repodir" fetch -q --prune origin 2>/dev/null   # refresh origin/* before branching
  local origin_base; origin_base=$(_dev_origin_base "$repodir")
  # Which line does the slot start on? `fresh` unless a lingering branch still carries
  # unmerged work: a local one (kill-without-merge) is resumed, an origin-only one (a
  # slot pushed from another machine, or a beam) is checked out tracking origin.
  local how=fresh why=
  if git -C "$repodir" show-ref --verify --quiet "refs/heads/$br"; then
    if _dev_branch_merged "$repodir" "$br"; then
      [[ -n $origin_base ]] || return 1   # do not delete a branch without a replacement base
      why="$br was merged ($_DEV_MERGED_HOW)"
      git -C "$repodir" branch -q -D "$br" 2>/dev/null
    else
      how=resume
    fi
  fi
  if [[ $how == fresh ]] && git -C "$repodir" show-ref --verify --quiet "refs/remotes/origin/$br"; then
    if _dev_branch_merged "$repodir" "$br" "refs/remotes/origin/$br"; then
      [[ -n $why ]] || why="origin/$br was merged ($_DEV_MERGED_HOW)"   # keep the local verdict when both linger
    else
      how=track
    fi
  fi
  [[ $how != fresh || -n $origin_base ]] || return 1
  [[ -n $why ]] && print -r -- "↻ $repo $slot: $why — starting fresh off $origin_base" >&2
  [[ $how == fresh ]] && _dev_worktree_drop_remote "$repodir" "$br"
  case $how in
    resume) git -C "$repodir" worktree add -q "$wt" "$br" 2>/dev/null ;;               # resume lingering local slot branch
    track)  git -C "$repodir" worktree add -q -b "$br" "$wt" "origin/$br" 2>/dev/null ;; # branch exists only on origin → create local tracking + check out
    *)      git -C "$repodir" worktree add -q -b "$br" "$wt" "refs/remotes/$origin_base" 2>/dev/null ;;
  esac
  [[ -e "$wt/.git" ]] || return 1
  _dev_auto_trust "$wt"
  print -r -- "$wt"
}

# _dev_worktree_freshen <repo> <slot> <wt> <br> — the existing-worktree half of the rule
# above: a slot whose worktree is still on disk but whose tmux is DEAD, whose branch is
# MERGED and whose tree is CLEAN is exactly what the sweep reaps — it just has not run
# yet (≤10 min), or gh/network failed it that pass. Reopening the slot in that window
# used to resume the merged line as-is. Now it is put back onto the origin default in place
# (`reset --hard`, safe precisely because merged+clean means every byte is already on
# main), the merged remote branch dropped, and the session starts current. Any live
# session rooted here (matched by session_path, like the sweep — alias drift cannot
# hide it), any uncommitted edit, or any doubt about the merge → untouched, silently:
# dirty-on-merged is the normal keep-working state. Ordered cheap→dear so the common
# reuse pays only a tmux probe and a local `git status`.
_dev_worktree_freshen() {
  local repo="$1" slot="$2" wt="$3" br="$4" repodir="${DEV_REPOS[$1]}"
  _dev_legacy_workspace_reserved "$wt" && return 0
  local -a livepaths
  livepaths=("${(@f)$(tmux list-sessions -F '#{session_path}' 2>/dev/null)}")
  (( ${livepaths[(Ie)$wt]} )) && return 0
  [[ $(git -C "$wt" symbolic-ref --short -q HEAD 2>/dev/null) == "$br" ]] || return 0   # detached / hand-switched → not ours to move
  local dirt; dirt=$(git -C "$wt" status --porcelain 2>/dev/null) || return 0
  [[ -z $dirt ]] || return 0
  git -C "$repodir" fetch -q --prune origin 2>/dev/null
  _dev_branch_merged "$repodir" "$br" || return 0
  local origin_base; origin_base=$(_dev_origin_base "$repodir") || return 0
  if git -C "$wt" reset -q --hard "refs/remotes/$origin_base" 2>/dev/null; then
    print -r -- "↻ $repo $slot: $br was merged ($_DEV_MERGED_HOW) — reset to $origin_base" >&2
    _dev_worktree_drop_remote "$repodir" "$br"
  fi
}

# _dev_worktree_drop_remote <repodir> <br> — delete origin/<br> iff it exists AND is
# merged (judged on the REMOTE tip, independently of any local copy). This is the delete
# GitHub performs under delete-branch-on-merge, which `t new` enables on every repo it
# creates; a repo that predates that setting (hive) keeps every merged slot branch
# forever, and a fresh slot cut off the origin default cannot push under the same name in
# a squash-merge repo (non-fast-forward). Best-effort: offline, the slot still starts
# fresh and the push problem is named rather than silently deferred. Nothing is lost by
# the delete — the PR keeps the commits (refs/pull/N/head) and the branch is, by the
# merged test, entirely contained in main.
_dev_worktree_drop_remote() {
  local repodir="$1" br="$2"
  git -C "$repodir" show-ref --verify --quiet "refs/remotes/origin/$br" || return 0
  _dev_branch_merged "$repodir" "$br" "refs/remotes/origin/$br" || return 0
  if git -C "$repodir" push -q origin --delete "$br" >/dev/null 2>&1; then
    print -r -- "  deleted origin/$br (merged $_DEV_MERGED_HOW — what delete-branch-on-merge would have done)" >&2
  else
    print -r -- "  ⚠ couldn't delete origin/$br — the slot's first push may be rejected as non-fast-forward" >&2
  fi
}

# _dev_slot_fresh <repo> <n> — is slot <n> a FRESH start for a new task? The one rule
# behind every "pick me a slot" path (`t open <repo>` with nothing to reattach, `--new`,
# `--fg`), replacing three loops that each answered differently: the auto-pick counted
# any slot with no tmux session as free — so a new task landed on a dead slot's parked
# worktree, or on a lingering branch it then resumed — while `new`/`--fg` skipped a
# worktree on disk but not a branch. Fresh means: no tmux session; and in worktree mode
# no worktree on disk (parked or merely unreaped work — the sweep clears merged+clean
# ones within ~10 min) and no lingering branch, local OR on origin, that still carries
# UNMERGED work. A merged leftover is fresh: _dev_worktree_create drops it and cuts the
# slot off the origin default, so counting it would only skip slot numbers forever in a repo
# without delete-branch-on-merge (hive). Cheap→dear: tmux, a stat, two show-refs; the
# gh call is paid only for a slot that actually has a lingering branch, and the drop
# makes it a one-time cost per slot. Shared-tree repos have no per-slot branch, so a slot
# there is fresh the moment its tmux name is free.
_dev_slot_fresh() {
  local repo="$1" n="$2"
  tmux has-session -t "=dev-${repo}-${n}:" 2>/dev/null && return 1
  _dev_worktree_enabled "$repo" || return 0
  local repodir="${DEV_REPOS[$repo]}" wt br ref
  wt="$(_dev_worktree_path "$repo" "$n")" || return 1; br="$(_dev_worktree_branch "$repo" "$n")"
  [[ -e "$wt/.git" ]] && return 1
  for ref in "refs/heads/$br" "refs/remotes/origin/$br"; do
    git -C "$repodir" show-ref --verify --quiet "$ref" || continue
    _dev_branch_merged "$repodir" "$br" "$ref" || return 1   # lingering line with unmerged work
  done
  return 0
}

# _dev_worktree_refuse <repo> <slot> — a worktree-enabled repo whose worktree could not
# be created does NOT get a session in the shared tree. That fallback used to be a
# one-line ↷ note scrolling past as the session started, and twice it parked an agent
# in ~/code/financial-forecast on the old shared dev branch, committing there for hours
# before anyone noticed (ff-12, ff-15). Failing loudly is cheaper than that.
_dev_worktree_refuse() {
  local repo="$1" slot="$2" repodir="${DEV_REPOS[$1]}"
  print -r -- "✗ could not create the worktree for $repo $slot — refusing to start in the shared tree $repodir" >&2
  local origin_base; origin_base=$(_dev_origin_base "$repodir")
  if [[ -n $origin_base ]]; then
    print -r -- "  try: git -C ${(q)repodir} worktree prune; git -C ${(q)repodir} worktree add ${(q)$(_dev_worktree_path "$repo" "$slot")} -b ${(q)$(_dev_worktree_branch "$repo" "$slot")} refs/remotes/${(q)origin_base}" >&2
  else
    print -r -- "  no fetched origin default branch found; try: git -C ${(q)repodir} fetch origin; git -C ${(q)repodir} remote set-head origin -a" >&2
  fi
  print -r -- "  (DEV_WORKTREE[$repo]=0 in ${T_LOCAL_RC} opts this repo out of worktrees entirely)" >&2
}

# New transfers publish an immutable snapshot instead of a shared slot branch.
# A dry-run catches transport/auth failures before a live source is stopped.
_dev_worktree_transfer_ref_valid() {
  local token=${1#refs/heads/t-transfer/}
  [[ $1 == refs/heads/t-transfer/* && ${#token} == 32 && $token != *[^0-9a-f]* ]]
}

_dev_worktree_beam_prepare() {
  local wt=${1:-$TB_WT} ref output
  [[ -n $DEV_WORKTREE_ROOT && $wt == $DEV_WORKTREE_ROOT/*/* && -e $wt/.git ]] || return 0
  git -C "$wt" rev-parse --verify HEAD >/dev/null || return 1
  ref="refs/heads/t-transfer/$(python3 -c 'import uuid; print(uuid.uuid4().hex)')" || return 1
  _dev_worktree_transfer_ref_valid "$ref" || return 1
  output=$(git -C "$wt" push --dry-run --porcelain --force-with-lease="$ref:" origin "HEAD:$ref" 2>&1) || {
    print -u2 -- "t session move: cannot publish a worktree snapshot; source was not stopped"
    print -u2 -r -- "$output"
    return 1
  }
  print -r -- "$ref"
}

# Remove only the snapshot we fetched, after the destination starts successfully.
# The private local ref remains available if the connection drops afterwards.
_dev_worktree_beam_finish() {
  [[ -n ${TB_TRANSFER_REF:-} ]] || return 0
  _dev_worktree_transfer_ref_valid "$TB_TRANSFER_REF" || return 1
  local tip remote
  tip=$(git -C "$1" rev-parse --verify "refs/t/transfers/${TB_TRANSFER_REF:t}^{commit}" 2>/dev/null) || return 1
  remote=$(git -C "$1" ls-remote --refs origin "$TB_TRANSFER_REF") || return 1
  [[ -n $remote ]] || return 0
  [[ ${remote%%$'\t'*} == "$tip" ]] || return 1
  git -C "$1" push -q --force-with-lease="$TB_TRANSFER_REF:$tip" origin ":$TB_TRANSFER_REF" 2>/dev/null || {
    print -u2 -- "t session move: snapshot retained on origin at $TB_TRANSFER_REF"
    return 1
  }
  return 0
}

# Commit the stopped source's final edits and publish the prepared snapshot.
# Older callers without a transfer ref keep their existing branch protocol.
_dev_worktree_beam_push() {
  local wt="${1:-$TB_WT}" host="${2:-$TB_HOST}" dirt br ref output
  [[ -n $DEV_WORKTREE_ROOT && $wt == ${DEV_WORKTREE_ROOT}/*/* && -e $wt/.git ]] || return 0
  br=$(git -C "$wt" symbolic-ref --short -q HEAD) || br='detached HEAD'
  ref=${TB_TRANSFER_REF:-$br}
  if [[ -n ${TB_TRANSFER_REF:-} ]]; then
    _dev_worktree_transfer_ref_valid "$ref" || return 1
  elif [[ $br == 'detached HEAD' ]]; then
    print -u2 'tbeam: cannot publish a detached worktree without a transfer snapshot'; return 1
  fi
  dirt=$(git -C "$wt" status --porcelain) || return 1
  if [[ -n $dirt ]]; then
    git -C "$wt" add -A && git -C "$wt" commit -q -m "wip: beam to ${host:-another host} [skip ci]" || {
      print -u2 -- "tbeam: couldn't commit all edits in $wt; source work was retained"; return 1
    }
    dirt=$(git -C "$wt" status --porcelain) || return 1
    [[ -z $dirt ]] || { print -u2 -- "tbeam: source changed while committing $wt; no snapshot published"; return 1; }
  fi
  local -a lease
  [[ -n ${TB_TRANSFER_REF:-} ]] && lease=("--force-with-lease=$ref:")
  output=$(git -C "$wt" push -q "${lease[@]}" origin "HEAD:$ref" 2>&1) || {
    print -u2 -- "tbeam: couldn't publish work from $wt; source commits and files were retained"
    print -u2 -r -- "$output"
    return 1
  }
  print -r -- "↑ carried $br → origin${TB_TRANSFER_REF:+ (transfer snapshot)}"
}

# Apply an exact fetched snapshot, failing closed on dirty/divergent work.
# Older peers without a snapshot retain their branch-based synchronization.
_dev_worktree_beam_sync() {
  local wt="$1"
  [[ -n $DEV_WORKTREE_ROOT && $wt == ${DEV_WORKTREE_ROOT}/*/* && -e $wt/.git ]] || return 0
  if [[ -n ${TB_TRANSFER_REF:-} ]]; then
    _dev_worktree_transfer_ref_valid "$TB_TRANSFER_REF" || return 1
    local snapshot="refs/t/transfers/${TB_TRANSFER_REF:t}" dirt
    git -C "$wt" rev-parse --verify "$snapshot^{commit}" >/dev/null 2>&1 || return 1
    dirt=$(git -C "$wt" status --porcelain) || return 1
    [[ -z $dirt ]] || { print -u2 'tbeam: destination changed before snapshot sync'; return 1; }
    git -C "$wt" merge --ff-only -q "$snapshot" || return 1
    return 0
  fi
  local br; br=$(git -C "$wt" symbolic-ref --short -q HEAD) || return 0
  local fetched=1
  git -C "$wt" fetch -q origin 2>/dev/null || fetched=0
  local head ref
  head=$(git -C "$wt" rev-parse -q HEAD 2>/dev/null)
  ref=$(git -C "$wt" rev-parse -q --verify "origin/${br}" 2>/dev/null)
  # If fetch failed, origin/${br} may be stale — warn so a missed beam edit isn't silent —
  # but still try the FF below in case the remote-tracking ref happens to be current.
  (( fetched )) || print -r -- "tbeam: couldn't fetch origin in ${wt} — origin/${br} may be stale" >&2
  [[ -n $ref && $head != "$ref" ]] || return 0          # no remote branch, or already at the tip
  if git -C "$wt" merge-base --is-ancestor "$head" "$ref" 2>/dev/null; then
    if git -C "$wt" merge -q --ff-only "origin/${br}" 2>/dev/null; then
      print -r -- "↓ synced ${br} to the beamed edits"
    else
      print -r -- "tbeam: couldn't fast-forward ${wt} to origin/${br} — left as-is (sync manually)" >&2
    fi
  elif _dev_branch_merged "$wt" "$br" "refs/remotes/origin/${br}"; then
    # The beamed tip is already on main (the slot went on after its PR merged with
    # nothing new to commit) and _dev_worktree_create cut this tree fresh off
    # the origin default instead of resurrecting it — the intended state.
    print -r -- "↓ origin/${br} is already merged ($_DEV_MERGED_HOW) — ${br} continues on its current branch"
  else
    print -r -- "tbeam: ${wt} diverged from origin/${br} — left as-is (resolve manually)" >&2
  fi
}

# Resolve a transfer destination without changing another session's worktree.
# New callers fetch an immutable snapshot; legacy callers use origin/<slot>.
# Live, dirty and divergent destinations reland on a fresh branch at that revision.
# Stdout contains only the selected directory; diagnostics go to stderr.
_dev_beam_land_cwd() {
  local cwd="$1" sid="${2:-}" ohost="${3:-}" agent="${4:-claude}"
  [[ -n $DEV_WORKTREE_ROOT && $cwd == ${DEV_WORKTREE_ROOT}/*/* ]] || { print -r -- "$cwd"; return 0 }
  local r repo slot; r=$(_dev_repo_of_dir "$cwd") || { print -r -- "$cwd"; return 0 }
  repo=${r%%$'\t'*}; slot=${r#*$'\t'}
  [[ -n $repo && -n $slot && -n ${DEV_REPOS[$repo]} ]] || { print -r -- "$cwd"; return 0 }
  local repodir="${DEV_REPOS[$repo]}"
  local br; br=$(_dev_worktree_branch "$repo" "$slot")
  # Is the slot taken ($why non-empty)? Judged by CONTENT, not name: a live dev session
  # ROOTED at this path (session_path, so alias drift can't hide it); else another
  # session's leftover work — a dirty worktree, a worktree whose HEAD diverged from the
  # beamed tip, or (worktree absent) a lingering local branch that diverged, which
  # _dev_worktree_create would otherwise resume under the beamed conversation.
  local source_ref="refs/remotes/origin/$br"
  local why= s p dirt
  if [[ -e $cwd/.git ]]; then
    _dev_legacy_workspace_reserved "$cwd" && why="it has a retired integration reservation"
    for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
      p=$(tmux display-message -p -t "=$s:" '#{session_path}' 2>/dev/null)
      [[ -n $p && ${p:A} == ${cwd:A} ]] && { why="$s is live in it"; break }
    done
    dirt=$(git -C "$cwd" status --porcelain) || return 1
    if [[ -z $why && -n $dirt ]]; then
      why="it holds another session's uncommitted work"
    fi
    if [[ -z $why && -n ${TB_TRANSFER_REF:-} &&
          $(git -C "$cwd" symbolic-ref --short -q HEAD) != $br ]]; then
      why="its checked-out branch belongs to other work"
    fi
  fi
  if [[ -n ${TB_TRANSFER_REF:-} ]]; then
    _dev_worktree_transfer_ref_valid "$TB_TRANSFER_REF" || return 1
    source_ref="refs/t/transfers/${TB_TRANSFER_REF:t}"
    git -C "$repodir" fetch -q origin || return 1
    git -C "$repodir" fetch -q --no-tags origin "$TB_TRANSFER_REF:$source_ref" || {
      print -u2 'tbeam: could not fetch the exact transfer snapshot; no destination started'; return 1
    }
    git -C "$repodir" rev-parse --verify "$source_ref^{commit}" >/dev/null || return 1
  else
    git -C "$repodir" fetch -q origin 2>/dev/null
  fi
  if [[ -z $why && -n ${TB_TRANSFER_REF:-} ]] &&
     git -C "$repodir" show-ref --verify --quiet "refs/remotes/origin/$br" &&
     ! git -C "$repodir" merge-base --is-ancestor "refs/remotes/origin/$br" "$source_ref"; then
    why="origin/$br belongs to a different line of work"
  fi
  if [[ -z $why ]] && git -C "$repodir" show-ref --verify --quiet "$source_ref"; then
    if [[ -e $cwd/.git ]]; then
      git -C "$cwd" merge-base --is-ancestor HEAD "$source_ref" 2>/dev/null \
        || why="it diverged from the source revision"
    elif git -C "$repodir" show-ref --verify --quiet "refs/heads/$br"; then
      git -C "$repodir" merge-base --is-ancestor "refs/heads/$br" "$source_ref" 2>/dev/null \
        || why="its lingering local branch diverged from the source revision"
    fi
  fi
  if [[ -z $why ]]; then
    if [[ -n ${TB_TRANSFER_REF:-} && ! -e $cwd/.git ]]; then
      if git -C "$repodir" show-ref --verify --quiet "refs/heads/$br"; then
        git -C "$repodir" worktree add -q "$cwd" "$br" || return 1
      else
        git -C "$repodir" worktree add -q --no-track -b "$br" "$cwd" "$source_ref" || return 1
      fi
    fi
    print -r -- "$cwd"; return 0
  fi

  # A collision requires the verified source revision; never substitute main.
  if ! git -C "$repodir" show-ref --verify --quiet "$source_ref"; then
    print -r -- "tbeam: slot $slot is taken here ($why) and origin has no $br to reland from (worktree push failed?) — the work is still on the origin machine; revive it there${ohost:+: t hosts run $ohost t session resume $repo $slot}" >&2
    return 1
  fi
  local n=1 nbr nwt
  while (( n <= 99 )); do
    nbr=$(_dev_worktree_branch "$repo" "$n"); nwt=$(_dev_worktree_path "$repo" "$n") || return 1
    if ! _dev_local_slot_live "$repo" "$n" && [[ ! -e $nwt/.git ]] \
       && ! git -C "$repodir" show-ref --verify --quiet "refs/heads/$nbr" \
       && ! git -C "$repodir" show-ref --verify --quiet "refs/remotes/origin/$nbr"; then
      break
    fi
    (( n++ ))
  done
  (( n <= 99 )) || { print -r -- "tbeam: no free slot to reland $repo into" >&2; return 1 }
  git -C "$repodir" worktree prune 2>/dev/null
  # --no-track: the new branch starts AT origin/<br> but must not track it, or a plain
  # `git push` in the new slot would aim at the OLD slot's branch.
  git -C "$repodir" worktree add -q --no-track -b "$nbr" "$nwt" "$source_ref" 2>/dev/null
  [[ -e $nwt/.git ]] || { print -r -- "tbeam: couldn't create $nwt to reland into" >&2; return 1 }
  # claude only: a codex rollout is date-keyed, so there is nothing to relocate for a
  # relanded slot (codex indexes it wherever it resumes)
  if [[ -n $sid && $agent == claude ]]; then
    local pdir="$HOME/.claude/projects" encold="${cwd//[^A-Za-z0-9]/-}" encnew="${nwt//[^A-Za-z0-9]/-}" f
    mkdir -p "$pdir/$encnew"
    # -R: the sid's files include a DIRECTORY named exactly <sid> (tool-results etc.,
    # newer Claude Code), not just <sid>.jsonl/<sid>.origin — a plain cp skips it.
    for f in "$pdir/$encold/$sid"*(N); do cp -Rp "$f" "$pdir/$encnew/${f:t}"; done
    [[ -e "$pdir/$encnew/$sid.jsonl" ]] || print -r -- "tbeam: no transcript for ${sid[1,8]}… under $encold — the relanded slot may not resume" >&2
  fi
  print -r -- "⚠ slot $slot is taken here ($why) — relanding as $repo $n ($nbr @ $source_ref)" >&2
  print -r -- "$nwt"
}

# _dev_repo_prepare <branch> — put a NEW session's checkout on <branch> without
# TRAMPLING sibling sessions that share this working tree. Every dev-<repo>-* slot
# cd's into the SAME tree, so the old `git stash; checkout; pull` dance was
# destructive: `git stash` silently pocketed a sibling's WIP, and `git pull` (a merge
# by default) could move the branch out from under a session mid-conversation. The
# earlier fix over-corrected — DIRTY → do nothing — which left every session on
# whatever branch the tree happened to be on (e.g. stuck on main), the very symptom
# this is named for. Policy now:
#   • Switch to <branch> (creating it if missing) EVEN WHEN the tree is dirty. We
#     never stash: `git checkout` carries non-conflicting uncommitted edits across
#     and refuses safely when a tracked edit would be overwritten — so the session
#     lands on the dev branch in the common case, and a genuine conflict just leaves
#     the checkout put (noted, not trampled; resolve it in a session worktree).
#   • Fast-forward (`pull --ff-only`) ONLY on a clean tree: a sibling may hold WIP,
#     and moving the branch under it mid-conversation is the trampling we avoid. When
#     dirty we are already on <branch>, so the session just starts; the ff waits for
#     a clean moment. (Matches the global pull.ff=only — never a merge/reset.)
# Runs in the session's own shell (cwd = the repo).
#
# HARD REFUSAL on the live tree. The canonical checkout is parked on `main` and IS
# the live surface ($HOME symlinks point at it), so switching its branch here would
# swap your live config out from under you mid-session — the exact bug the whole
# worktree model exists to prevent, reintroduced through a side door. It is reachable
# two ways: the DEV_WORKTREE[repo]=0 opt-out, and the _dev_worktree_create failure
# fallback. Note `_dev_branch_for` returns $DEV_BRANCH for an unpinned repo, so the
# `checkout -qb` below would CREATE that branch and move the live tree onto it.
# Refuse unconditionally, not just when the branch differs: the `pull --ff-only` half
# is `dots`' job too, and one flat rule beats a two-clause one.
_dev_repo_prepare() {
  local branch="$1" here live
  here=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
  live=$T_HOME
  if _t_tree_is_live "$here"; then
    # Name the repo the way `t` does (alias, not the path tail — a worktree path ends
    # in its SLOT number, so ${here:t} would suggest `t open 1`).
    local _r _alias; _r=$(_dev_repo_of_dir "$here" 2>/dev/null); _alias=${_r%%$'\t'*}
    echo "↷ branch sync skipped — $here contains active shell code; its updater owns the branch."
    echo "  develop in a per-session worktree instead${_alias:+: t session open $_alias}"
    return 0
  fi
  git fetch -q origin 2>/dev/null
  if [[ "$(git symbolic-ref --short -q HEAD)" != "$branch" ]]; then
    git checkout -q "$branch" 2>/dev/null || git checkout -qb "$branch" 2>/dev/null || {
      echo "↷ branch sync skipped — can't switch to $branch without overwriting local edits (commit them first)."
      return 0
    }
  fi
  # Only fast-forward on a clean tree — never move the branch under a sibling's WIP.
  if [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
    git pull -q --ff-only origin "$branch" 2>/dev/null || true
  fi
}

# Generate a cd shortcut per repo: each key jumps straight to its dir.
# _dev_procs_rooted_in <dir> — pids whose cwd is <dir> or below it, excluding shells,
# agents and their infrastructure. The dev server a slot starts (`npm run dev` → vite)
# is what this exists for: it is detached from the slot's tmux session, so it outlives
# both `t kill` and the sweep, keeps serving the OLD code on the slot's port (the next
# tenant of that slot number sees its URL in the statusline and trusts it), and
# rewrites .vite/deps into the reaped path — the debris dir that blocked the next
# `git worktree add`. Sixteen of them were found running, the oldest two weeks old.
# A cwd is NOT proof of session ownership: Codex's shared app-server daemon inherits
# the first CLI's worktree, and killing it disconnects every client. Protect agents,
# GUI apps, their descendants AND launcher ancestors, even outside the target cwd.
# One lsof and one ps snapshot on macOS, /proc on Linux — never a per-pid ps fork.
_dev_cwd_pids() {
  [[ -n $1 ]] || return 1
  local dir="${1:A}" pid cwd line listing
  [[ $dir != / ]] || return 1
  if [[ -d /proc ]]; then
    for pid in /proc/<->(N:t); do
      cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
      [[ $cwd == $dir || $cwd == $dir/* ]] && print -r -- "$pid"
    done
  else
    listing=$(lsof -a -d cwd -Fpn 2>/dev/null) || return 1
    for line in "${(@f)listing}"; do   # p<pid> / fcwd / n<path>
      case $line in
        p*) pid=${line#p} ;;
        n*) cwd=${line#n}; [[ $cwd == $dir || $cwd == $dir/* ]] && print -r -- "$pid" ;;
      esac
    done
  fi
  return 0
}

_dev_procs_rooted_in() {
  # The protected view is also a deletion veto: preserving a process while
  # deleting its cwd strands shared daemons and their detached update loops.
  local view="${2:-stoppable}" pid ppid comm line up keep cwd_pids
  local -A parents names roots protected seen
  local snapshot
  # An unreadable table is not permission to signal processes by cwd alone.
  snapshot=$(ps -A -o pid=,ppid=,comm= 2>/dev/null) || return 1
  for line in ${(f)snapshot}; do
    read -r pid ppid comm <<< "$line"
    [[ $pid == <-> && $ppid == <-> && -n $comm ]] || continue
    parents[$pid]=$ppid; names[$pid]=${comm:t}
    if _dev_agent_is_proc "$comm" || [[ $comm == *.app/Contents/* || ${comm:t} == cursor-agent ]]; then
      roots[$pid]=1
    fi
  done
  # Protect launchers too: terminating a node wrapper may close its child's IPC.
  # These ancestors are protected individually, NOT as roots of a protected tree.
  for pid in ${(k)roots} $$ $PPID; do
    up=$pid
    while [[ $up == <-> ]] && (( up > 1 )); do
      [[ -n ${protected[$up]} ]] && break
      protected[$up]=1
      up=${parents[$up]:-}
    done
  done
  cwd_pids=$(_dev_cwd_pids "$1") || return 1
  for pid in ${(f)cwd_pids}; do
    [[ $pid == <-> ]] && (( pid > 1 )) || continue
    if [[ -z ${names[$pid]} ]]; then
      # The process appeared after ps, or could not be inspected. Never signal
      # it, and do not delete its cwd on the strength of an incomplete snapshot.
      [[ $view == protected ]] && print -r -- "$pid"
      continue
    fi
    keep=${protected[$pid]:-0}
    comm=${names[$pid]}
    up=$pid; seen=()
    while [[ $up == <-> ]] && (( up > 1 )); do
      [[ -n ${roots[$up]} || -n ${seen[$up]} ]] && break
      seen[$up]=1
      up=${parents[$up]:-}
    done
    [[ $up == <-> && ( -n ${roots[$up]} || -n ${seen[$up]} ) ]] && keep=1
    if [[ $view == protected ]]; then
      (( keep )) && print -r -- "$pid"
      continue
    fi
    (( keep )) && continue
    case $comm in zsh|bash|sh|fish|dash|tmux|login|-*) continue ;; esac
    print -r -- "$pid"
  done
  return 0
}

_dev_worktree_process_busy() {
  local pids
  # Inspection failure is uncertainty, not permission to remove the directory.
  pids=$(_dev_procs_rooted_in "$1" protected) || return 0
  [[ -n $pids ]]
}

# _dev_stop_rooted <dir> [why] — SIGTERM unprotected leftover processes in <dir>
# and print one summary line (nothing when there was nothing to stop).
_dev_stop_rooted() {
  local dir="$1" why="${2:-}" n=0 pid
  for pid in $(_dev_procs_rooted_in "$dir"); do
    kill -TERM "$pid" 2>/dev/null && (( n++ ))
  done
  (( n )) && print -r -- "stopped $n process(es) still rooted in $dir${why:+ ($why)}"
  return 0
}

# Worktree sweep — reap per-session worktrees whose work has landed. A slot's worktree
# + branch (dev/<basename>-<slot>) are removed only when the tmux session is dead
# (matched by session_path, never by name — dodges alias drift), no agent infrastructure
# still uses the directory, the branch is merged to main, AND the working tree is clean.
# The clean gate exists because a merged
# TIP says nothing about the WORKING TREE: after a PR merges, the slot lives on and the
# next feature accumulates as uncommitted edits on the same branch (tip still == the
# merged PR head) — a reboot then kills every tmux session, the sweep sees dead+merged,
# and `worktree remove --force` erases the whole uncommitted feature (the 2026-07-14
# incident: five slots reaped at once, one holding a day of unpushed work). Unmerged or
# uncommitted work is never destroyed (a killed-but-unmerged slot keeps its worktree so
# reopening the slot resumes it). A prompt hook and stamp gate limit the sweep.
# _dev_branch_merged <repodir> <branch> [tip-ref] — true if <branch> has landed on main.
# Prefers gh (a merged PR with this head branch — catches GitHub SQUASH-merges, which
# leave no ancestor link so `git branch --merged`/merge-base miss them); an OPEN PR is a
# hard not-merged. Falls back to the git-only ancestor test when gh is absent/unauth. Any
# inconclusive answer is treated as NOT merged, so the sweep never deletes on a maybe.
# Both paths pin the answer to the CURRENT tip: per-slot branch names are reused after a
# sweep (`dev/<basename>-<slot>`), so an unrelated historical merged PR with the same
# head, OR a freshly-created branch sitting exactly at the origin default with uncommitted
# working-tree edits, must NOT be reported as merged — that would destroy live work.
# The tip defaults to the LOCAL branch; [tip-ref] (e.g. refs/remotes/origin/<branch>)
# judges another ref under the same rules — how _dev_worktree_create tells a merged
# origin-only lingering branch from one carrying another machine's live work. On success
# $_DEV_MERGED_HOW names the proof (`#N` or `in origin/<default>`) for the caller's message.
_dev_branch_merged() {
  local repodir="$1" br="$2" ref="${3:-refs/heads/$2}" tip pr merged_oid open main_oid
  _DEV_MERGED_HOW=
  tip=$(git -C "$repodir" rev-parse --verify -q "$ref" 2>/dev/null)
  [[ -n $tip ]] || return 1                       # no such ref → nothing to compare
  if command -v gh >/dev/null 2>&1; then
    pr=$(cd "$repodir" 2>/dev/null && gh pr list --head "$br" --state merged --json number,headRefOid -q '.[0] // empty | "\(.number)\t\(.headRefOid)"' 2>/dev/null)
    merged_oid=${pr#*$'\t'}
    if [[ -n $merged_oid && $merged_oid == "$tip" ]]; then   # this exact commit was merged
      _DEV_MERGED_HOW="#${pr%%$'\t'*}"; return 0
    fi
    open=$(cd "$repodir" 2>/dev/null && gh pr list --head "$br" --state open --json number -q '.[0].number' 2>/dev/null)
    [[ -n $open ]] && return 1
  fi
  local origin_base; origin_base=$(_dev_origin_base "$repodir") || return 1
  git -C "$repodir" fetch -q origin "${origin_base#origin/}" 2>/dev/null
  main_oid=$(git -C "$repodir" rev-parse --verify -q "refs/remotes/$origin_base" 2>/dev/null)
  [[ -n $main_oid ]] || return 1
  [[ "$tip" != "$main_oid" ]] || return 1         # equal to the default → no unique history yet; uncommitted edits may still be live
  git -C "$repodir" merge-base --is-ancestor "$tip" "$main_oid" 2>/dev/null || return 1
  _DEV_MERGED_HOW="in $origin_base"
}
# _dev_worktree_sweep_run — the actual reap (runs detached). Walks every
# $DEV_WORKTREE_ROOT/<basename>/<slot> worktree; skips ones with a live tmux session
# rooted there; removes the worktree + branch when merged.
_dev_worktree_sweep_run() {
  local root=$DEV_WORKTREE_ROOT
  [[ -n $root && -d $root ]] || return
  local -a livepaths
  livepaths=("${(@f)$(tmux list-sessions -F '#{session_path}' 2>/dev/null)}")
  # Resolved ONCE, outside the loop: after `dots --dev` the $HOME symlinks point into
  # a session worktree, and reaping it would dangle every managed link — including
  # ~/.zshrc, which takes `dots` itself down with it. Merge the PR, wait for a sweep,
  # and the machine loses its shell config with no obvious cause.
  local livewt=$T_HOME
  local wt repo slot repodir br r dirt
  for wt in $root/*/*(N/); do                       # <basename>/<slot> dirs
    [[ -e "$wt/.git" ]] || continue
    (( ${livepaths[(Ie)$wt]} )) && continue         # live session here → keep
    _dev_legacy_workspace_reserved "$wt" && continue
    _dev_issue_preparing "$wt" && continue        # issue launch is still being prepared
    # Unlike the dirty/merged skips below this one is LOGGED: a worktree that is
    # merged, clean, and never reaped is otherwise a silent mystery.
    if _t_tree_is_live "$wt"; then
      print -r -- "[$(strftime '%F %T' $EPOCHSECONDS 2>/dev/null)] sweep: keeping $wt — active shell code is loaded from this tree"
      continue
    fi
    r=$(_dev_repo_of_dir "$wt"); repo=${r%%$'\t'*}; slot=${r#*$'\t'}
    [[ -n $repo && -n $slot ]] || continue
    repodir=${DEV_REPOS[$repo]}
    [[ -n $repodir && -d $repodir ]] || continue
    br=$(_dev_worktree_branch "$repo" "$slot")
    _dev_branch_merged "$repodir" "$br" || continue
    # Merged tip ≠ clean tree: post-merge uncommitted work sits on a merged sha, and
    # `remove --force` (needed below because git refuses dirty removals) would erase it.
    # Skip silently, like the gates above — dirty-on-merged is the normal keep-working
    # state, not an anomaly worth a log line every pass. Unreadable status counts dirty.
    dirt=$(git -C "$wt" status --porcelain 2>/dev/null) || dirt='?'
    [[ -z $dirt ]] || continue
    if _dev_worktree_process_busy "$wt"; then
      print -r -- "[$(strftime '%F %T' $EPOCHSECONDS 2>/dev/null)] sweep: keeping $wt — agent infrastructure may still use this directory"
      continue
    fi
    print -r -- "[$(strftime '%F %T' $EPOCHSECONDS 2>/dev/null)] sweep: $wt (branch $br merged)"
    # The slot's dev server first: left running it serves stale code on the slot's
    # port and rewrites .vite/deps into the path we are about to remove.
    _dev_stop_rooted "$wt" "reaped"
    git -C "$repodir" worktree remove --force "$wt" 2>/dev/null \
      && git -C "$repodir" branch -D "$br" 2>/dev/null
    git -C "$repodir" worktree prune 2>/dev/null
  done
  # Debris: a reaped path that _dev_worktree_create moved aside because a process
  # still rooted there had recreated it. Nothing tracked can live in it (the remove
  # above already cleared the tree), so once its processes are stopped it is cache.
  for wt in $root/*/*.debris-<->(N/); do
    if _dev_worktree_process_busy "$wt"; then
      print -r -- "[$(strftime '%F %T' $EPOCHSECONDS 2>/dev/null)] sweep: keeping $wt — agent infrastructure may still use this directory"
      continue
    fi
    print -r -- "[$(strftime '%F %T' $EPOCHSECONDS 2>/dev/null)] sweep: debris $wt"
    _dev_stop_rooted "$wt" "debris"
    rm -rf "$wt" 2>/dev/null
  done
}
_dev_worktree_sweep() {
  [[ -n $DEV_WORKTREE_ROOT && -d $DEV_WORKTREE_ROOT ]] || return
  local interval=600 stamp="${XDG_CACHE_HOME:-$HOME/.cache}/t/dev-worktree-sweep" now=$EPOCHSECONDS last=0
  [[ -r "$stamp" ]] && last=$(<"$stamp")
  (( now - last >= interval )) || return
  print -r -- "$now" >| "$stamp"                    # stamp BEFORE the run (overlap guard)
  ( _dev_worktree_sweep_run >>"${XDG_STATE_HOME:-$HOME/.local/state}/t/dev-worktree-sweep.log" 2>&1 & )
}
add-zsh-hook precmd _dev_worktree_sweep

# _tpaste_claude_ready <session> — return 0 once Claude is accepting input in
