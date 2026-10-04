# ─── Agent seam — which CLI occupies a slot: claude (default) or codex ───────────
#
# Everything the slot tooling knows about "the agent" goes through these; nothing
# else compares a process name to `claude` or spells its launch line. claude keeps
# its pre-assigned id (`claude --session-id`, see _dev_new_session); codex has NO
# such flag, so a codex slot's id arrives only through the SessionStart hook
# (bin/claude-stamp-tmux --agent codex → CLAUDE_RESUME_ID + the pid registry), which
# is why the hook must be trusted once under /hooks in codex (t doctor says when it
# never fired). The agent of a LIVE slot is read off its process (comm, ground truth)
# and only then off the DEV_AGENT tmux stamp _dev_new_session/_dev_resume_session and
# the hook set.
# _dev_agent_valid <name> — the closed set (a typo in DEV_AGENT must not launch `$a`).
_dev_agent_valid() { case "$1" in claude|codex) return 0 ;; *) return 1 ;; esac }
# _DEV_AGENTS / _DEV_AGENT_GLYPH — every agent the tooling supports, in legend order,
# and each one's ICON: the twin of bin/t's _INSTALL_AGENTS glyphs, pinned equal by
# test_zsh_agent_glyphs_match_bin_t — a new agent lands in BOTH tables with an icon,
# or the suite refuses it. Wider than _dev_agent_valid on purpose: cursor never
# occupies a slot, but every legend lists every supported tool. `t resume` rows show
# `<icon> <name>`; `t ls` rows the icon alone under a header that spells the legend.
typeset -ga _DEV_AGENTS=(claude codex cursor)
typeset -gA _DEV_AGENT_GLYPH=( claude '✱' codex '⬡' cursor '◆' )
# _dev_agent_glyph <agent> — its icon (`?` for anything outside the table).
_dev_agent_glyph() { print -r -- "${_DEV_AGENT_GLYPH[$1]:-?}" }
# _dev_agent_legend — `✱ claude · ⬡ codex · ◆ cursor`, the one legend every view prints.
_dev_agent_legend() {
  local a; local -a parts
  for a in $_DEV_AGENTS; do parts+=("${_DEV_AGENT_GLYPH[$a]} $a"); done
  print -r -- "${(j: · :)parts}"
}
# _dev_agent_for <repo> [override] — the agent a NEW slot of <repo> gets: the --codex /
# --claude flag, else DEV_AGENT[repo], else DEV_AGENT_DEFAULT. Prints it; rc 1 + a
# pointer at ${T_LOCAL_RC} for anything outside the set.
_dev_agent_for() {
  local repo="$1" a="${2:-}"
  [[ -n $a ]] || a=${DEV_AGENT[$repo]:-${DEV_AGENT_DEFAULT:-claude}}
  _dev_agent_valid "$a" || {
    print -u2 -r -- "t: unknown agent '$a' (claude or codex) — check DEV_AGENT[$repo] / DEV_AGENT_DEFAULT in ${T_LOCAL_RC}"
    return 1
  }
  print -r -- "$a"
}
# _dev_agent_is_proc <comm> — is this process name an agent CLI? The ONE match every
# process walk uses (the npm-launched codex can present as a native `codex-<triple>`
# child of `node`; brew's cask is a bare `codex`). The triple is spelled out, never a
# bare `codex-*`: codex 0.154 runs HELPERS under itself (`codex-code-mode-host`), and the
# wildcard counted each as an agent — every codex slot grew a phantom `(foreground
# codex)` row in `t ls` (2026-09-21). Linux truncates comm to 15 chars, which still
# keeps the `codex-aarch64-`/`codex-x86_64-` prefix.
_dev_agent_is_proc() { case "${1:t}" in claude|codex|codex-aarch64-*|codex-x86_64-*) return 0 ;; *) return 1 ;; esac }
# A detached Codex app-server (including its pid-update-loop) is shared machinery,
# never a foreground conversation, even after its launching CLI has exited.
_dev_agent_is_service() {
  local cmdline; cmdline=$(ps -ww -o args= -p "$1" 2>/dev/null)
  local -a words; read -r -A words <<< "$cmdline"
  (( ${words[(Ie)app-server]} ))
}
# _dev_agent_of_comm <comm> — the agent name for a process name (empty if none).
_dev_agent_of_comm() { case "${1:t}" in claude) print -r -- claude ;; codex|codex-aarch64-*|codex-x86_64-*) print -r -- codex ;; esac }
# _dev_agent_of_session <tmux-session> — which agent a slot runs: the comm of its live
# agent process (zero extra forks under _dev_ps_snapshot), else the DEV_AGENT stamp,
# else claude (every pre-seam slot).
_dev_agent_of_session() {
  local s="$1" pid a
  if pid=$(_dev_session_claude_pid "$s") && [[ -n $pid ]]; then
    a=${_DEV_PS_COMM[$pid]:-}
    [[ -n $a ]] || a=$(ps -o comm= -p "$pid" 2>/dev/null)
    a=$(_dev_agent_of_comm "$a")
    [[ -n $a ]] && { print -r -- "$a"; return 0; }
  fi
  a=$(tmux show-environment -t "=$s" DEV_AGENT 2>/dev/null | cut -d= -f2)
  _dev_agent_valid "$a" || a=claude
  print -r -- "$a"
}
# _dev_agent_check <agent> — the binary is here, or say how to get it.
_dev_agent_check() {
  command -v "$1" >/dev/null 2>&1 && return 0
  print -u2 -r -- "t: '$1' is not installed here — run: t install $1"
  return 1
}
# _dev_agent_new_cmd <agent> [sid] — the pane command for a FRESH slot.
# DEV_MODEL is keyed by agent, so switching tools never carries the other tool's model.
# Resumes deliberately retain their model/effort behavior, without these new-session defaults.
_dev_agent_new_cmd() {
  local agent="$1" model="${DEV_MODEL[$1]:-}"
  local effort="${DEV_EFFORT[$1]:-}"
  local -a launch_args=("$agent")
  [[ $agent == claude && -n $2 ]] && launch_args+=(--session-id "$2")
  [[ -n $model ]] && launch_args+=(--model "$model")
  if [[ -n $effort ]]; then
    case "$agent" in
      codex) launch_args+=(-c "model_reasoning_effort=$effort") ;;
      claude) launch_args+=(--effort "$effort") ;;
    esac
  fi
  case "$agent:${DEV_FAST[$agent]:-}" in
    codex:1) launch_args+=(-c service_tier=fast --enable fast_mode) ;;
    codex:0) launch_args+=(-c service_tier=default) ;;
    claude:1) launch_args+=(--settings '{"fastMode":true}') ;;
    claude:0) launch_args+=(--settings '{"fastMode":false}') ;;
  esac
  print -r -- "${(j: :)${(@q)launch_args}}"
}
# _dev_agent_resume_cmd <agent> <sid> — the pane command that resumes conversation <sid>.
_dev_agent_resume_cmd() {
  case "$1" in codex) print -r -- "codex resume $2" ;; *) print -r -- "claude -r $2" ;; esac
}
# _dev_agent_at_welcome <agent> <session> [cwd] — live but no conversation yet.
# claude: the 'Welcome back' banner (_dev_session_at_welcome). codex: a recorded id
# OR recent conversation evidence from its index. A missing SessionStart stamp is
# not proof of an empty conversation (untrusted/disabled hooks never stamp). Its
# boxed startup banner also stays visible through a short exchange.
_dev_agent_at_welcome() {
  local dir sid
  case "$1" in
    codex)
      dir=${3:-$(tmux display-message -p -t "=$2:" '#{session_path}' 2>/dev/null)}
      sid=$(_dev_session_sid "$2" "$dir")
      [[ -n $sid ]] && return 1
      [[ -z $(_codex_live_transcript "$dir" "$(_dev_session_claude_pid "$2")") ]] ;;
    *)     _dev_session_at_welcome "$2" ;;
  esac
}
# _dev_agent_pid_above — walk up from this shell to the nearest agent process (the
# claude or codex whose tool shell we are running in) and print its pid; rc 1 on miss.
# A Codex app-server is shared by multiple threads: its pid registry entry cannot
# identify the calling thread, so it must not count as a self-owned conversation.
# Capped at init. The generalised _tpush_claude_pid (kept below as an alias).
_dev_agent_pid_above() {
  local pid=$$ comm
  while (( pid > 1 )); do
    comm=$(ps -o comm= -p "$pid" 2>/dev/null) || return 1
    if _dev_agent_is_proc "$comm"; then
      [[ $(_dev_agent_of_comm "$comm") == codex ]] && _dev_agent_is_service "$pid" && return 1
      print -r -- "$pid"; return 0
    fi
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null) || return 1
    pid=${pid//[[:space:]]/}
    [[ -n $pid ]] || return 1
  done
  return 1
}

# ─── Codex thread store + the agent-agnostic transcript locators ─────────────────
#
# A claude conversation lives at a cwd-keyed path (~/.claude/projects/<enc cwd>/<sid>
# .jsonl), which is how every "what is this slot working on" question was answered:
# glob the slot's project dir. A codex conversation is a DATE-keyed rollout
# (~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl) — nothing about its path
# says which directory it belongs to. Two sources fill that gap: the hook's
# rollouts/<sid> cache (sid → path, written at SessionStart) and codex's own sqlite
# thread index (~/.codex/state_5.sqlite, table threads: id, rollout_path, cwd, title,
# name, first_user_message, updated_at — the row exists while the session is LIVE and
# its cwd follows the last resume). Reads are python3 stdlib sqlite3 in read-only URI
# mode; a missing or locked DB reads as "no threads", never an error, so a box that
# never ran codex pays one failed open and nothing else.
_codex_db() { print -r -- "${CODEX_HOME:-$HOME/.codex}/state_5.sqlite" }
# _codex_threads <where> <arg> — rows `sid\tpath\tcwd\ttitle\tupdated_at` newest first;
# <where> is cwd | prefix | sid. Internal; the wrappers below name the intent.
_codex_threads() {
  local db; db=$(_codex_db)
  [[ -r $db ]] || return 0
  python3 - "$db" "$1" "$2" <<'PY' 2>/dev/null
import sqlite3, sys
db, mode, arg = sys.argv[1:4]
try:
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=0.5)
    c.execute('pragma busy_timeout=500')
    q = ("select id, rollout_path, cwd, coalesce(nullif(name,''), title, ''), updated_at "
         "from threads where archived=0 and ")
    # A subagent thread (source = '{"subagent": {"thread_spawn": {"parent_thread_id": …}}}',
    # title '') is a helper the parent spawned — codex's `<sid>/subagents/` — not a
    # conversation: it copies the parent's brief as its first prompt, so listed as one it
    # is the parent's row repeated once per helper (ff-35 showed four). An exact-id
    # lookup stays unfiltered (a beam by id is deliberate).
    sub = "instr(source, '\"subagent\"')=0 and "
    if mode == 'cwd':
        rows = c.execute(q + sub + "cwd=? order by updated_at desc", (arg,)).fetchall()
    elif mode == 'prefix':
        rows = c.execute(q + sub + "cwd like ? order by updated_at desc", (arg.replace('%', '') + '%',)).fetchall()
    else:
        rows = c.execute(q + "id=? order by updated_at desc", (arg,)).fetchall()
except Exception:
    rows = []
for r in rows:
    sys.stdout.write('\t'.join(str(x if x is not None else '').replace('\t', ' ').replace('\n', ' ') for x in r) + '\n')
PY
}
# _codex_threads_for_cwd <cwd> — the codex conversations recorded in <cwd>, newest first.
_codex_threads_for_cwd() { _codex_threads cwd "$1" }
# _codex_thread_lookup <sid> — one row for a thread id (empty if unknown).
_codex_thread_lookup()   { _codex_threads sid "$1" }

# _codex_live_transcript <cwd> <pid> — display-only fallback when a live Codex never
# fired SessionStart. Require one nonempty, non-subagent thread in this exact cwd
# updated during this process's lifetime. Old conversations in a reused slot and
# ambiguous candidates must not turn a fresh welcome screen into an active row.
# This is evidence for a title/context, NOT an authoritative pid→sid mapping: never
# use it in _dev_session_sid or stamp it into tmux (beam/app act on those ids).
_codex_live_transcript() {
  local db start
  [[ -n $1 && $2 == <-> ]] || return 0
  db=$(_codex_db); [[ -r $db ]] || return 0
  start=$(LC_ALL=C ps -o lstart= -p "$2" 2>/dev/null) || return 0
  python3 - "$db" "$1" "$start" <<'PY' 2>/dev/null
import datetime, os, sqlite3, sys
try:
    start = datetime.datetime.strptime(sys.argv[3].strip(), '%a %b %d %H:%M:%S %Y').timestamp()
    c = sqlite3.connect('file:%s?mode=ro' % sys.argv[1], uri=True, timeout=0.5)
    rows = c.execute("select rollout_path from threads where archived=0 and cwd=? "
                     "and updated_at>=? and instr(source, '\"subagent\"')=0 "
                     "and trim(first_user_message)<>'' limit 2", (sys.argv[2], start)).fetchall()
    if len(rows) == 1 and os.path.isfile(rows[0][0]):
        print(rows[0][0])
except (OSError, ValueError, sqlite3.Error):
    pass
PY
}

# Recover a missing hook stamp from Codex's CURRENT pane title and status footer.
# The shared app-server can run outside the CLI's ancestry, leaving no pid stamp.
# Unlike the display-only recency fallback, this requires the same exact named
# conversation in both live UI surfaces and a unique thread in this worktree.
# Do not cache it: the CLI may switch conversations without changing its pid.
_codex_pane_sid() {
  local session="$1" dir="$2" pid="$3" db start pane_title pane_text
  [[ -n $dir && $pid == <-> ]] || return 0
  db=$(_codex_db); [[ -r $db ]] || return 0
  start=$(LC_ALL=C ps -o lstart= -p "$pid" 2>/dev/null) || return 0
  pane_title=$(tmux display-message -p -t "=$session:" '#{pane_title}' 2>/dev/null)
  [[ -n $pane_title ]] || return 0
  pane_text=$(tmux capture-pane -p -t "=$session:" 2>/dev/null)
  python3 - "$db" "$dir" "$start" "$pane_title" "$pane_text" <<'PY' 2>/dev/null
import datetime, json, os, re, sqlite3, sys
try:
    db, cwd, started, pane_title, pane = sys.argv[1:]
    start = datetime.datetime.strptime(started.strip(), '%a %b %d %H:%M:%S %Y').timestamp()
    name, sep, folder = pane_title.rpartition(' | ')
    if not sep or not name or folder != os.path.basename(cwd):
        sys.exit(0)
    # The optional profile field may be absent. Identify status lines by their
    # workspace field so the three-part shortcut help below them is not mistaken
    # for a footer. Only the last status line counts, even if it is incomplete or
    # points elsewhere: a title in earlier output is not current evidence.
    footers = [parts for line in pane.splitlines()
               if len(parts := line.strip().split(' · ')) >= 2
               and parts[1].startswith(('/', '~/'))]
    if not footers:
        sys.exit(0)
    footer = footers[-1]
    if len(footer) < 3 or os.path.expanduser(footer[1]) != cwd or footer[2] != name:
        sys.exit(0)
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=0.5)
    rows = c.execute("select id,rollout_path,updated_at,archived,first_user_message from threads "
                     "where cwd=? and name=? and instr(source, '\"subagent\"')=0 limit 2",
                     (cwd, name)).fetchall()
    # Check uniqueness BEFORE recency: an older namesake is still ambiguous.
    if len(rows) != 1:
        sys.exit(0)
    sid, rollout, updated, archived, prompt = rows[0]
    if (updated < start or archived or not prompt.strip() or
            not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sid)):
        sys.exit(0)
    with open(rollout) as f:
        meta = json.loads(f.readline())
    payload = meta.get('payload', {})
    if (meta.get('type') == 'session_meta' and payload.get('id') == sid and
            payload.get('cwd') == cwd and payload.get('source') in ('cli', 'vscode') and
            payload.get('thread_source') != 'subagent'):
        print(sid)
except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
    pass
PY
}

# _dev_transcript_agent <path> — which agent wrote this transcript, from its name
# (a codex rollout is `rollout-…`; everything else is a claude <sid>.jsonl).
_dev_transcript_agent() { [[ ${1:t} == rollout-* ]] && print -r -- codex || print -r -- claude }
# _dev_transcript_sid <path> — the session id a transcript belongs to: the filename
# for claude, the trailing uuid of `rollout-<ts>-<uuid>.jsonl` for codex.
_dev_transcript_sid() {
  local b=${1:t}; b=${b%.jsonl}
  if [[ $b == rollout-* ]]; then print -r -- "${b: -36}"; else print -r -- "$b"; fi
}
# _dev_agent_transcript <agent> <sid> [cwd] — the transcript path for a session id, or
# nothing. claude: the cwd-keyed project dir (any project dir when cwd is omitted).
# codex: the hook's rollouts/<sid> cache, else the sqlite row, else a glob over the
# date tree (a synced-in rollout on a host whose hook never saw it).
_dev_agent_transcript() {
  setopt local_options null_glob
  local agent="$1" sid="$2" cwd="${3:-}" p
  [[ -n $sid && $sid != - ]] || return 1
  if [[ $agent == codex ]]; then
    p="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/rollouts/$sid"
    [[ -r $p ]] && { p=$(<"$p"); [[ -f $p ]] && { print -r -- "$p"; return 0; }; }
    p=$(_codex_thread_lookup "$sid"); p=${${p#*$'\t'}%%$'\t'*}
    [[ -n $p && -f $p ]] && { print -r -- "$p"; return 0; }
    local -a g=( "${CODEX_HOME:-$HOME/.codex}"/sessions/*/*/*/rollout-*-"$sid".jsonl )
    [[ -n ${g[1]} ]] && { print -r -- "${g[1]}"; return 0; }
    return 1
  fi
  local -a tx
  if [[ -n $cwd ]]; then tx=( "$HOME/.claude/projects/${cwd//[^A-Za-z0-9]/-}/$sid".jsonl )
  else tx=( "$HOME/.claude/projects"/*/"$sid".jsonl ); fi
  [[ -n ${tx[1]} && -f ${tx[1]} ]] && { print -r -- "${tx[1]}"; return 0; }   # -f: a literal path has no glob to null
  return 1
}
# _codex_rollout_scan <cwd> — rollouts whose session_meta says <cwd>, newest first,
# from the date tree itself: the index only learns a rollout when codex next touches
# it (verified: a copied-in rollout is resumable and gets indexed ON resume), so a
# rollout that csync / `t resume -r` / a beam just synced in would otherwise be
# invisible to `t resume` until then. One first-line read per rollout, cached by
# inode in ~/.cache/claude-sessions/rollout-cwd.json so a settled tree costs a stat
# per file; a missing tree prints nothing.
_codex_rollout_scan() {
  local root="${CODEX_HOME:-$HOME/.codex}/sessions"
  [[ -d $root ]] || return 0
  python3 - "$root" "$1" "${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/rollout-cwd.json" <<'PY' 2>/dev/null
import glob, json, os, sys
root, want, cache = sys.argv[1:4]
try:
    with open(cache) as fh: known = json.load(fh)
except (OSError, ValueError): known = {}
out, changed = [], False
for p in glob.glob(os.path.join(root, '*', '*', '*', 'rollout-*.jsonl')):
    try: st = os.stat(p)
    except OSError: continue
    k = known.get(p)
    # entry = [inode, cwd, kind]; kind 'sub' marks a subagent thread's rollout (see
    # _codex_threads) — a 2-element entry is from before that field and is re-read once
    if not (isinstance(k, list) and len(k) == 3 and k[0] == st.st_ino):
        cwd, kind = '', ''
        try:
            with open(p, 'rb') as fh: first = fh.readline(65536).decode('utf-8', 'replace')
            d = json.loads(first)
            if d.get('type') == 'session_meta':
                pl = d.get('payload') or {}
                cwd = pl.get('cwd') or ''
                if pl.get('thread_source') == 'subagent' or isinstance(pl.get('source'), dict) and 'subagent' in pl['source']:
                    kind = 'sub'
        except (OSError, ValueError, AttributeError): pass
        known[p] = k = [st.st_ino, cwd, kind]; changed = True
    if k[1] == want and not k[2]: out.append((st.st_mtime, p))
if changed:
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = '%s.%d.tmp' % (cache, os.getpid())
        with open(tmp, 'w') as fh: json.dump(known, fh)
        os.replace(tmp, cache)
    except OSError: pass
for _, p in sorted(out, reverse=True): sys.stdout.write(p + '\n')
PY
}
# _dev_agent_transcripts_for_cwd <agent> <cwd> — every transcript recorded in <cwd>,
# newest first, one path per line (claude: the project dir by mtime; codex: the sqlite
# rows UNION the date-tree scan, deduped). The dead-slot scan behind `t resume` is
# built on this.
_dev_agent_transcripts_for_cwd() {
  setopt local_options null_glob bare_glob_qual
  local agent="$1" cwd="$2" row p
  if [[ $agent == codex ]]; then
    local -A seen
    for row in ${(f)"$(_codex_threads_for_cwd "$cwd")"}; do
      p=${${row#*$'\t'}%%$'\t'*}; [[ -f $p && -z ${seen[$p]:-} ]] && { seen[$p]=1; print -r -- "$p"; }
    done
    for p in ${(f)"$(_codex_rollout_scan "$cwd")"}; do
      [[ -z ${seen[$p]:-} ]] && { seen[$p]=1; print -r -- "$p"; }
    done
    return 0
  fi
  local -a tx=( "$HOME/.claude/projects/${cwd//[^A-Za-z0-9]/-}"/*.jsonl(Nom) )
  (( $#tx )) && print -rl -- "${(@)tx}"
  return 0
}
# _dev_agent_newest_sid <agent> <cwd> — the id of the newest conversation in <cwd>:
# the pre-hook fallback every pop/plan path had, now per agent.
_dev_agent_newest_sid() {
  local p; p=$(_dev_agent_transcripts_for_cwd "$1" "$2" | head -1)
  [[ -n $p ]] && _dev_transcript_sid "$p"
}
# _dev_self_agent / _dev_self_sid — "am I running inside an agent's tool shell, and
# which conversation?" claude exports CLAUDE_CODE_SESSION_ID; codex exports nothing
# (openai/codex#8923), so its answer is the pid registry entry the hook wrote for the
# codex process above this shell (_dev_agent_pid_above). Both print nothing when
# this is a plain terminal.
_dev_self_agent() {
  [[ -n $CLAUDE_CODE_SESSION_ID ]] && { print -r -- claude; return 0; }
  local pid; pid=$(_dev_agent_pid_above) || return 1
  _dev_agent_of_comm "$(ps -o comm= -p "$pid" 2>/dev/null)"
}
_dev_self_sid() {
  [[ -n $CLAUDE_CODE_SESSION_ID ]] && { print -r -- "$CLAUDE_CODE_SESSION_ID"; return 0; }
  local pid reg; pid=$(_dev_agent_pid_above) || return 1
  reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/$pid"
  [[ -r $reg ]] || return 1
  local line; line="$(<"$reg")"; print -r -- "${line%%$'\t'*}"
}

# Worktree-per-session helpers. The path + branch are derived from the repo's BASENAME
# (${DEV_REPOS[repo]:t}), not the alias, so they are identical on every host (a dir is
# `dot` here and `dotfiles` there, but its basename `dotfiles` is stable) — which keeps
# cross-host resolution (tbeam, _dev_remote_resolve) coherent.
