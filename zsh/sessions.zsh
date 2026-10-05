# the session's pane, else return 1. tpaste polls this after launching a fresh
# session so it knows when the path can be delivered.
_tpaste_claude_ready() {
  local session="$1"
  # The session bootstraps with `git …; claude`. While the git dance runs the
  # pane's foreground command is git/zsh; once Claude (a Node CLI) takes over it
  # becomes node. That process hand-off is a more robust readiness signal than
  # matching Claude's TUI text, which changes between versions. To gate on the
  # actual prompt instead, swap in a `tmux capture-pane -p` string match.
  local cmd
  cmd=$(tmux display-message -p -t "=$session:" '#{pane_current_command}' 2>/dev/null)
  [[ $cmd == node ]] && return 0
  _dev_agent_is_proc "$cmd" && return 0
  return 1
}

# _t_paste [-n] [-p] [repo] [slot] — the `t paste` verb: paste an iCloud Drive screenshot/doc
# path into a dev tmux session. Covers images (png/jpg/jpeg/heic) and docs (pdf/txt/md/csv/
# docx) in the iCloud Drive root, newest first; fzf-picks when there is a TTY + fzf, else (or
# with -n) falls back to the newest file. -p/--pick is a back-compat no-op (the picker is the
# default). User-facing help lives in bin/t (`t paste -h`) — the shim routes -h there, so this
# helper takes none of its own.
_t_paste() {
  # The picker is the default; -n/--newest forces the no-prompt fast path.
  # -p/--pick is kept as a no-op so old muscle memory/scripts still work.
  local newest=0 arg
  local -a _pos
  for arg in "$@"; do
    case "$arg" in
      -n|--newest) newest=1 ;;
      -p|--pick)   ;;
      *)           _pos+=("$arg") ;;
    esac
  done
  local repo="${_pos[1]}"
  local slot="${_pos[2]}"
  # Repo-aware: `t paste 4` ≡ `t paste <cwd-repo> 4`; bare `t paste` targets the
  # $PWD repo's next free slot (see _t_infer_repo).
  if [[ "$repo" == <-> && -z "$slot" ]]; then slot=$repo; repo=$(_t_infer_repo "$slot"); fi
  [[ -z "$repo" ]] && repo=$(_t_infer_repo)
  if [[ -z "$repo" ]]; then
    echo "Usage: tpaste [-n] [repo] [slot]   (no repo: the one \$PWD is in; repo: one of ${(k)DEV_REPOS:-(none configured — see ${T_LOCAL_RC})})"
    return 1
  fi

  local icloud="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
  # verify iCloud Drive is accessible
  if [[ ! -d "$icloud" ]]; then
    echo "iCloud Drive not found at: $icloud"
    return 1
  fi
  # collect pasteable files in the iCloud Drive root: images + docs, one flat
  # set, newest-by-mtime wins. (Screenshots used to be a priority tier, but that
  # made a just-exported PDF unreachable whenever any screenshot existed — the
  # newest file IS the one you just saved, so the tiering bought nothing.)
  # (N) is the nullglob qualifier: unmatched globs expand to nothing instead of
  # raising zsh's "no matches found" error. Collect into an array first so an
  # empty result never makes `ls` fall back to listing the current directory.
  # extended_glob is needed for the parenthesized alternation in the filename
  # pattern; local_options restores the caller's setopts on return. The leading
  # (#i) makes the whole pattern case-INSENSITIVE — iPhone screenshots save as
  # *.PNG (uppercase), and zsh globbing is case-sensitive even on macOS's
  # case-insensitive filesystem, so a bare *.png silently skipped them.
  setopt local_options extended_glob
  local src
  local -a files
  files=("$icloud"/(#i)*.(png|jpg|jpeg|heic|pdf|txt|md|csv|docx)(N))

  if (( ${#files} == 0 )); then
    echo "No images or docs found in iCloud Drive ($icloud)"
    return 1
  fi

  # Picker by default: open fzf whenever we have a TTY + fzf, unless -n forced
  # the fast path. No TTY/fzf (or -n) falls back to the newest file by mtime.
  if (( ! newest )) && [[ -t 0 && -t 1 ]] && command -v fzf >/dev/null 2>&1; then
    # ls -t sorts newest-first; show just the basename but return the full path
    src=$(ls -t "${files[@]}" | _t_fzf --prompt='tpaste> ' --height=40% --reverse \
          --delimiter=/ --with-nth=-1) || { echo "Cancelled."; return 1; }
  else
    src=$(ls -t "${files[@]}" | head -1)
  fi

  echo "Using: $src"

  # find session
  # repo→path map: see the global DEV_REPOS (defined near the cd shortcuts)

  if [[ -z "${DEV_REPOS[$repo]}" ]]; then
    echo "Unknown repo: $repo. Use one of: ${(k)DEV_REPOS:-(none configured — see ${T_LOCAL_RC})}"
    return 1
  fi

  # no slot → the next FRESH slot (_dev_slot_fresh: no session, no worktree on disk, no
  # lingering unmerged branch — the same rule `t open` uses), so the default is always
  # a genuinely fresh session, never a dead slot's parked work. Unbounded like `t open
  # --new`: slot numbers grow past 20 in practice, and a cap here read as "all in use".
  if [[ -z "$slot" ]]; then
    local n=1
    while ! _dev_slot_fresh "$repo" "$n"; do (( n++ )); done
    slot=$n
  fi

  local session="dev-${repo}-${slot}"

  # existing session → Claude is already live, so paste straight in (no attach)
  if tmux has-session -t "=$session:" 2>/dev/null; then
    tmux send-keys -t "=$session:" "$src"
    echo "Pasted path into $session — press Enter in that session to send to Claude."
    return
  fi

  if (( ${#REMOTE_HOSTS} )); then
    local app_owner; app_owner=$(_dev_remote_app_owner "$repo" "$slot")
    if [[ -n $app_owner ]]; then
      print -u2 -- "t paste: $repo $slot is reserved by the Codex desktop app on $app_owner; close it there and release the reservation before opening here"
      return 1
    fi
  fi

  # new session → bootstrap Claude, wait for it to come up, queue the path, attach
  local _pdir="${DEV_REPOS[$repo]}" _pskip=
  if _dev_worktree_enabled "$repo"; then
    local _pwt; _pwt="$(_dev_worktree_create "$repo" "$slot")"
    if [[ -n $_pwt ]]; then _pdir="$_pwt"; _pskip=1
    else _dev_worktree_refuse "$repo" "$slot"; return 1; fi
  fi
  echo "Starting $session for the file…"
  _dev_new_session "$session" "$_pdir" "$(_dev_branch_for "$repo")" "$_pskip"

  # wait (up to ~30s) for Claude's process to take over the pane; if the
  # readiness check never matches this degrades to a plain 30s wait
  local waited=0
  while (( waited < 30 )); do
    _tpaste_claude_ready "$session" && break
    sleep 1
    (( waited++ ))
  done
  # brief settle: the node process exists a beat before its input box mounts,
  # and keystrokes sent into that gap get dropped
  sleep 1

  tmux send-keys -t "=$session:" "$src"
  echo "Queued path in $session — attaching; press Enter to send to Claude."
  tmux attach-session -t "=$session:"
}

# ─── one-shot process/pane snapshot (the scan fast path) ─────────────────────────
# Every "is this slot running claude, and which pid is it?" question used to fork:
# a `tmux list-panes` per session, then `ps -o comm=` + `pgrep -P` per pid walking
# down the subtree — and _dev_session_rows asks it three times per session
# (_dev_session_sid, _dev_session_has_claude, _dev_session_summary→sid again) with
# _dev_fg_rows asking a fourth time for every session. On 17 slots that was ~800ms
# of pure fork overhead in `t ls` and `t resume`.
# _dev_ps_snapshot takes the whole process table and the whole pane→pid map in TWO
# forks, into globals the walkers read instead. It is TTL'd (3s) rather than
# explicitly scoped: `t ls` runs in a throwaway `zsh -lic` where the globals die with
# the process anyway, while an interactive shell calling _t_resume must not answer
# from a snapshot taken minutes ago. Every consumer keeps its original fork-based
# path and falls back to it when the snapshot is empty (no ps, no tmux server), so
# behaviour is identical — this is a cache, not a new source of truth.
typeset -gA _DEV_PS_COMM _DEV_PS_PPID _DEV_PS_KIDS _DEV_PANE_PIDS _DEV_SESS_PID _DEV_PS_APP
typeset -g _DEV_PS_AT=0
_dev_ps_snapshot() {
  (( ${#_DEV_PS_COMM} )) && (( EPOCHREALTIME - _DEV_PS_AT < 3 )) && return 0
  _DEV_PS_COMM=(); _DEV_PS_PPID=(); _DEV_PS_KIDS=(); _DEV_PANE_PIDS=(); _DEV_SESS_PID=(); _DEV_PS_APP=()
  local pid ppid comm sname
  # comm can contain spaces (an .app bundle path), so it takes the rest of the line
  # and :t trims it to the basename — same normalization the old per-pid ps did.
  # A binary living inside a GUI bundle is remembered as such: the ChatGPT app ships
  # its own `codex` core (/Applications/ChatGPT.app/Contents/Resources/codex), which
  # is not a terminal session and must not render as a foreground agent.
  while read -r pid ppid comm; do
    [[ $pid == <-> ]] || continue
    [[ $comm == *.app/Contents/* ]] && _DEV_PS_APP[$pid]=1
    _DEV_PS_COMM[$pid]=${comm:t}
    _DEV_PS_PPID[$pid]=$ppid
    _DEV_PS_KIDS[$ppid]="${_DEV_PS_KIDS[$ppid]:-} $pid"
  done < <(ps -Axo pid=,ppid=,comm= 2>/dev/null)
  while IFS=$'\t' read -r sname pid; do
    [[ -n $sname ]] || continue
    _DEV_PANE_PIDS[$sname]="${_DEV_PANE_PIDS[$sname]:-} $pid"
  done < <(tmux list-panes -a -F "#{session_name}"$'\t'"#{pane_pid}" 2>/dev/null)
  (( ${#_DEV_PS_COMM} )) && _DEV_PS_AT=$EPOCHREALTIME
}
# _dev_snap_ok — true when the snapshot can answer session→pid questions.
_dev_snap_ok() { (( ${#_DEV_PS_COMM} && ${#_DEV_PANE_PIDS} )) }

# _dev_session_has_claude <session> — true if a live `claude` process exists
# ANYWHERE in the session: the pane leader itself, a direct child, or deeper.
# We deliberately do NOT use pane_current_command: Claude sets its process title
# to its version string (e.g. "2.1.159"), so it never reads as "claude"/"node"
# there. `ps -o comm=` still reports "claude" (comm is the executable name, fixed
# at exec — argv/title rewrites don't touch it), which is the reliable signal.
#
# Why the whole subtree and not just direct children (this was an intermittent
# false-negative — a live, detached Claude rendering with a blank ✓):
#   • Claude can be the pane LEADER (e.g. `exec claude`): then pgrep -P pane_pid
#     returns only Claude's OWN children (python/caffeinate tool procs), none of
#     which read as claude — so the old direct-children-only scan missed it.
#   • Claude can be a GRANDCHILD (resumed / nested-shell launches), one level
#     below the direct child the old scan stopped at.
# The `node` hedge (Claude launched as `node`) is kept only for a DIRECT child of
# the pane shell — a node straight off a dev pane is claude-as-node; a node buried
# deeper is more likely an MCP server or dev tool, so we match only the
# unambiguous `claude` name there.
_dev_session_has_claude() {
  local s="$1" pane_pid kid comm pid
  local -a stack
  # Snapshot fast path (see _dev_ps_snapshot): same walk, zero forks. The `node`
  # special-case for a DIRECT child is preserved verbatim — a claude launched via a
  # node shim shows up that way, and dropping it would flip live slots to "(no
  # active session)".
  _dev_ps_snapshot
  if _dev_snap_ok; then
    for pane_pid in ${=_DEV_PANE_PIDS[$s]:-}; do
      _dev_agent_is_proc "${_DEV_PS_COMM[$pane_pid]:-}" && return 0
      for kid in ${=_DEV_PS_KIDS[$pane_pid]:-}; do
        case ${_DEV_PS_COMM[$kid]:-} in (claude|codex|codex-aarch64-*|codex-x86_64-*|node) return 0 ;; esac
        stack=($kid)
        while (( $#stack )); do
          pid=$stack[1]; shift stack
          _dev_agent_is_proc "${_DEV_PS_COMM[$pid]:-}" && return 0
          stack+=(${=_DEV_PS_KIDS[$pid]:-})
        done
      done
    done
    return 1
  fi
  for pane_pid in ${(f)"$(tmux list-panes -t "=$s:" -F '#{pane_pid}' 2>/dev/null)"}; do
    comm=$(ps -o comm= -p "$pane_pid" 2>/dev/null)
    _dev_agent_is_proc "$comm" && return 0
    for kid in ${(f)"$(pgrep -P "$pane_pid" 2>/dev/null)"}; do
      comm=$(ps -o comm= -p "$kid" 2>/dev/null)
      case "${comm:t}" in (claude|codex|codex-aarch64-*|codex-x86_64-*|node) return 0 ;; esac
      _dev_pid_tree_has_claude "$kid" && return 0
    done
  done
  return 1
}

# _dev_pid_tree_has_claude <pid> — true if <pid> or any descendant has comm
# `claude`. Recursive deep-search helper for _dev_session_has_claude.
_dev_pid_tree_has_claude() {
  local pid="$1" comm kid
  comm=$(ps -o comm= -p "$pid" 2>/dev/null)
  _dev_agent_is_proc "$comm" && return 0
  for kid in ${(f)"$(pgrep -P "$pid" 2>/dev/null)"}; do
    _dev_pid_tree_has_claude "$kid" && return 0
  done
  return 1
}

# _dev_session_at_welcome <session> — true if the session's Claude is parked on
# its startup splash (launched but never given a prompt), i.e. a LIVE claude with
# NO loaded conversation. _dev_session_has_claude can't tell these apart: a fresh
# `claude` sitting at the welcome screen is just as live a process as one mid-
# conversation. The "Welcome back" banner is only rendered before the first user
# message and scrolls away after it, so its presence in the visible pane is the
# reliable "nothing's actually happening here" signal. Used by _dev_list to
# withhold the ✓ active-context mark from idle splash sessions (this was the
# "dev ls says cfp-2 is active but it isn't" false positive — an old-style plain
# `claude` with no transcript to map, so pane content is the only signal).
_dev_session_at_welcome() {
  tmux capture-pane -t "=$1:" -p 2>/dev/null | grep -q 'Welcome back'
}

# _dev_session_claude_pid <session> — print the pid of the live `claude` in the
# session (first match, same subtree scan as _dev_session_has_claude), or nothing.
# Used to map OLD sessions (no CLAUDE_RESUME_ID) to their transcript by start time.
_dev_session_claude_pid() {
  local s="$1" pane_pid kid comm found pid
  local -a stack
  # Snapshot fast path, MEMOIZED per session (`-` = looked, found nothing): three
  # different callers ask this for the same session during one scan.
  _dev_ps_snapshot
  if _dev_snap_ok; then
    if [[ -n ${_DEV_SESS_PID[$s]:-} ]]; then
      [[ ${_DEV_SESS_PID[$s]} == - ]] && return 1
      print -r -- "${_DEV_SESS_PID[$s]}"; return 0
    fi
    stack=(${=_DEV_PANE_PIDS[$s]:-})
    while (( $#stack )); do
      pid=$stack[1]; shift stack
      if _dev_agent_is_proc "${_DEV_PS_COMM[$pid]:-}"; then
        _DEV_SESS_PID[$s]=$pid; print -r -- "$pid"; return 0
      fi
      stack+=(${=_DEV_PS_KIDS[$pid]:-})
    done
    _DEV_SESS_PID[$s]='-'
    return 1
  fi
  for pane_pid in ${(f)"$(tmux list-panes -t "=$s:" -F '#{pane_pid}' 2>/dev/null)"}; do
    comm=$(ps -o comm= -p "$pane_pid" 2>/dev/null)
    _dev_agent_is_proc "$comm" && { print -r -- "$pane_pid"; return 0; }
    for kid in ${(f)"$(pgrep -P "$pane_pid" 2>/dev/null)"}; do
      found=$(_dev_pid_tree_claude_pid "$kid") && { print -r -- "$found"; return 0; }
    done
  done
  return 1
}
# _dev_pid_tree_claude_pid <pid> — print first pid in the subtree whose comm is
# `claude` (companion to _dev_pid_tree_has_claude, but returns the pid).
_dev_pid_tree_claude_pid() {
  local pid="$1" comm kid found
  comm=$(ps -o comm= -p "$pid" 2>/dev/null)
  _dev_agent_is_proc "$comm" && { print -r -- "$pid"; return 0; }
  for kid in ${(f)"$(pgrep -P "$pid" 2>/dev/null)"}; do
    found=$(_dev_pid_tree_claude_pid "$kid") && { print -r -- "$found"; return 0; }
  done
  return 1
}

# _transcript_meta_batch — the transcript reader behind every title/PR-tag column.
# args: transcript paths. stdout: `path\ttitle\tpr-url` per line, same order, one line
# per input (missing/unreadable → empty fields). Paths ride in ARGV, not on stdin —
# the heredoc IS python3's stdin here, so a `sys.stdin.read()` would come back empty.
# Two things make it
# fast, and both were needed: `t resume ff` read 135MB across 43 transcripts twice
# (once for the title, once for a `grep -ao` PR-URL pass) and `t ls` forked python3
# once per live session — together ~2.2s and ~0.5s of pure re-reading.
#   BATCHED: one python3 for the whole scan instead of one per file, so the ~25ms
#   interpreter start is paid once. This is why callers collect their paths first.
#   INCREMENTALLY CACHED: per-transcript state (last title seen, first user prompt,
#   last PR URL, and the byte offset scanned so far) lives in
#   ~/.cache/claude-sessions/meta/<hash>, so a DEAD transcript is never re-read and a
#   LIVE one re-reads only the bytes appended since last time. Transcripts are
#   append-only .jsonl, which is what makes a resumable offset correct; the cache is
#   keyed on st_ino and invalidated whenever the inode changes (csync's rsync writes
#   a NEW file, so a synced-in transcript rescans in full) or the file is shorter
#   than the recorded offset (truncation/rewrite). The offset always stops at the
#   last complete newline, so a half-written final line is re-read next time rather
#   than parsed in halves.
# Cache misses are cheap to be wrong about (a title is cosmetic) but the invalidation
# above means a stale one needs an in-place same-inode rewrite that shrinks nothing —
# which append-only .jsonl never does.
#   CODEX TITLES COME FROM THE INDEX, NOT THE FILE. claude writes its /rename and its
#   generated title INTO the transcript (the `custom-title` / `ai-title` records
#   below); codex writes neither — verified on 0.154, a renamed thread's name appears
#   nowhere in its rollout, only in ~/.codex/state_5.sqlite's `threads.name` (its
#   `title` column is the raw first prompt, so it buys nothing over the file). So a
#   rollout's name is looked up LIVE, one read-only query per batch keyed on the sid
#   in the filename, and is deliberately NOT folded into the per-path cache: codex
#   generates the name a turn AFTER the prompt and a /rename appends no byte at all,
#   so a cache keyed on "bytes since last scan" would pin the first prompt forever —
#   which is exactly how a renamed codex slot kept its opening line as its `t ls`
#   title. Names are per-host by nature (the index is machine-local runtime state;
#   only rollouts travel), so a synced-in rollout reads as its first prompt until
#   that host's codex indexes it.
_transcript_meta_batch() {
  (( $# )) || return 0
  python3 - "$(_codex_db)" "$@" <<'PY'
import hashlib, json, os, re, sqlite3, sys

CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                     'claude-sessions', 'meta')
PR = re.compile(r'github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+')
SOFT = (ValueError, AttributeError, TypeError, KeyError)


def load(key):
    try:
        with open(os.path.join(CACHE, key)) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def save(key, st):
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = os.path.join(CACHE, '%s.%d.tmp' % (key, os.getpid()))
        with open(tmp, 'w') as fh:
            json.dump(st, fh)
        os.replace(tmp, os.path.join(CACHE, key))
    except OSError:
        pass


def codex_names(db, paths):
    """{rollout path: codex's own short thread title}, for the rollouts in this batch.
    Unknown ids, an unnamed thread, a missing/locked/corrupt index → simply absent, so
    the caller falls back to the first prompt."""
    ids = {}
    for p in paths:
        base = os.path.basename(p)
        if base.startswith('rollout-') and base.endswith('.jsonl'):
            ids[base[:-len('.jsonl')][-36:]] = p      # rollout-<ts>-<uuid>.jsonl
    if not ids or not db:
        return {}
    out = {}
    try:
        c = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=0.5)
        c.execute('pragma busy_timeout=500')
        for sid, name in c.execute('select id, name from threads where id in (%s)'
                                   % ','.join('?' * len(ids)), tuple(ids)):
            if name and name.strip() and sid in ids:
                out[ids[sid]] = name
    except Exception:
        pass
    return out


def scan_codex(st, text):
    # A codex rollout: `response_item` records whose payload is a user `message`
    # carry the prompts as content [{type: input_text, text}]; the first line is
    # session_meta. The same "first real prompt" rule as claude, with codex's own
    # injected user-role context skipped: the <recommended_plugins> /
    # <environment_context> notices (the `<` filter) and the `# AGENTS.md
    # instructions for <dir>` message it fronts a session with (verified on 0.154).
    for line in text.splitlines():
        if st['msg'] is None and '"response_item"' in line and '"role":"user"' in line.replace(' ', ''):
            try:
                pl = json.loads(line).get('payload', {})
                if pl.get('type') == 'message' and pl.get('role') == 'user':
                    txt = ' '.join(x.get('text', '') for x in pl.get('content') or []
                                   if isinstance(x, dict)).strip()
                    if txt and not txt.startswith('<') and not txt.startswith('# AGENTS.md instructions'):
                        st['msg'] = txt
            except SOFT: pass
        hits = PR.findall(line)
        if hits: st['pr'] = hits[-1]


def scan(st, text):
    if st.get('fmt') is None:
        # sniff ONCE per transcript: a rollout's first line is session_meta
        st['fmt'] = 'codex' if text[:4096].find('"session_meta"') >= 0 else 'claude'
    if st['fmt'] == 'codex':
        return scan_codex(st, text)
    for line in text.splitlines():
        if '"custom-title"' in line:                    # /rename — wins
            try:
                t = json.loads(line).get('customTitle')
                if t: st['ct'] = t                      # keep the most recent
            except SOFT: pass
        if '"ai-title"' in line:
            try:
                t = json.loads(line).get('aiTitle')
                if t: st['at'] = t                      # keep the most recent
            except SOFT: pass
        if st['msg'] is None and '"type":"user"' in line:   # first real prompt
            try:
                c = json.loads(line).get('message', {}).get('content')
                txt = c if isinstance(c, str) else (
                    ' '.join(x.get('text', '') for x in c if isinstance(x, dict))
                    if isinstance(c, list) else '')
                txt = txt.strip()
                if txt and not txt.startswith('<'): st['msg'] = txt
            except SOFT: pass
        hits = PR.findall(line)                         # LAST PR URL in the session
        if hits: st['pr'] = hits[-1]


db, paths = sys.argv[1], sys.argv[2:]
names = codex_names(db, paths)

for path in paths:
    if not path:
        continue
    try:
        info = os.stat(path)
    except OSError:
        sys.stdout.write('%s\t\t\n' % path)
        continue
    key = hashlib.sha1(path.encode('utf-8', 'surrogateescape')).hexdigest()[:20]
    st = load(key)
    if not st or st.get('ino') != info.st_ino or st.get('off', 0) > info.st_size:
        st = {'ino': info.st_ino, 'off': 0, 'ct': None, 'at': None, 'msg': None, 'pr': None, 'fmt': None}
    if st['off'] < info.st_size:
        data = b''
        try:
            with open(path, 'rb') as fh:
                fh.seek(st['off'])
                data = fh.read()
        except OSError:
            pass
        cut = data.rfind(b'\n')                         # whole lines only
        if cut >= 0:
            scan(st, data[:cut + 1].decode('utf-8', 'replace'))
            st['off'] += cut + 1
            save(key, st)
    # codex's own name wins over the rollout's first prompt, as claude's does
    title = ' '.join((names.get(path) or st['ct'] or st['at'] or st['msg'] or '').split())[:50]
    sys.stdout.write('%s\t%s\t%s\n' % (path, title, st['pr'] or ''))
PY
}

# _transcript_title <transcript.jsonl> — print the one-line title of a Claude
# transcript: customTitle (set by /rename) wins, else the generated aiTitle, else the
# first real user prompt; trimmed to 50 chars. Factored out so dev-slot summaries and
# foreground-session summaries share one parser — now a single-path call into
# _transcript_meta_batch above, so one-off callers still get the incremental cache.
# A caller with SEVERAL transcripts should call the batch directly instead: this one
# pays a python3 start per file, which is the whole cost once the cache is warm.
_transcript_title() {
  local row; row=$(_transcript_meta_batch "$1")
  row=${row#*$'\t'}          # drop the echoed path
  print -r -- "${row%$'\t'*}"  # drop the PR url
}

# _dev_summary_for_pid <dir> <claude-pid> — title of the transcript a LIVE claude
# (in <dir>, pid <claude-pid>) is driving, when there's no recorded session id. The
# dir's newest transcript is WRONG when two live claudes share a repo (both resolve
# to it, identical summaries); each `claude` creates its transcript within seconds of
# launch, so disambiguate by birthtime closest to (and at/after) the process start.
# Falls back to newest-by-mtime if the pid/birthtimes can't be read. Shared by
# unstamped dev slots and foreground sessions (neither has a CLAUDE_RESUME_ID).
_dev_summary_for_pid() {
  setopt local_options null_glob bare_glob_qual
  local dir="$1" cpid="$2"
  local proj="$HOME/.claude/projects/${dir//[^A-Za-z0-9]/-}" start
  local -a tx
  if [[ -n $cpid ]]; then
    start=$(ps -o lstart= -p "$cpid" 2>/dev/null)
    if [[ $OSTYPE == darwin* ]]; then
      start=$(date -j -f '%a %b %d %T %Y' "${start## #}" +%s 2>/dev/null)
    else
      start=$(date -d "${start## #}" +%s 2>/dev/null)   # GNU date parses lstart natively
    fi
  fi
  if [[ -n $start ]]; then
    local f b best bestdiff diff
    for f in "$proj"/*.jsonl(N); do
      b=$(_stat_birth "$f") || continue
      (( b < start - 2 )) && continue                  # born before this claude → not ours
      diff=$(( b - start ))
      if [[ -z $bestdiff ]] || (( diff < bestdiff )); then bestdiff=$diff; best=$f; fi
    done
    [[ -n $best ]] && tx=( "$best" )
  fi
  [[ -n ${tx[1]} ]] || tx=( "$proj"/*.jsonl(Nom[1]) )  # fallback: newest by mtime
  [[ -n ${tx[1]} ]] || return 0
  _transcript_title "${tx[1]}"
}

# _dev_session_sid <session> [dir] — the AUTHORITATIVE Claude session id for a dev
# slot, used both to render its summary and as the targeting id in _dev_session_rows.
#
# The tmux CLAUDE_RESUME_ID stamp alone is NOT trustworthy: it is a tmux *session*
# variable that outlives the claude that set it, so a slot whose pane is reused by a
# different conversation keeps the *predecessor's* id — even a cross-repo one (the
# "dot-3 shows an amex/financial-forecast title" bug). So resolve in order:
#   1. The SessionStart registry entry for the claude ACTUALLY running in the pane
#      (pid -> "sid\tcwd", written by claude-stamp-tmux). This is ground truth for
#      the live process and BEATS the stamp when they disagree. Guarded by a
#      cwd == dir check so a recycled pid's stale entry is ignored.
#   2. The tmux stamp — only if its transcript belongs to THIS slot's cwd: Claude's
#      project directory or Codex's index/rollout metadata. Cross-worktree stamps
#      are stale and dropped, even if their transcripts still exist.
#   3. For an unstamped live Codex, its pane title + current status footer matched
#      to a unique named thread in this cwd, with validated rollout metadata.
# Prints the id, or nothing (display callers may use a recency fallback).
_dev_session_sid() {
  setopt local_options null_glob bare_glob_qual
  local session="$1" dir="${2:-}" sid cpid reg line rcwd agent tx
  [[ -n $dir ]] || dir=$(tmux display-message -p -t "=$session:" '#{session_path}' 2>/dev/null)
  cpid=$(_dev_session_claude_pid "$session")
  reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions/$cpid"
  if [[ -n $cpid && -r $reg ]]; then
    line="$(<"$reg")"; sid="${line%%$'\t'*}"; rcwd="${line#*$'\t'}"
    [[ -n $sid && $rcwd == $dir ]] && { print -r -- "$sid"; return 0; }
    sid=
  fi
  sid=$(tmux show-environment -t "=$session" CLAUDE_RESUME_ID 2>/dev/null | cut -d= -f2)
  agent=$(_dev_agent_of_session "$session")
  # Claude's path is cwd-keyed; Codex needs an explicit cwd check because its
  # date-keyed rollout can exist even when the stamp belongs to another worktree.
  if [[ -n $sid ]] && tx=$(_dev_agent_transcript "$agent" "$sid" "$dir") &&
      { [[ $agent != codex ]] || _codex_sid_in_cwd "$sid" "$dir" "$tx"; }; then
    print -r -- "$sid"
  elif [[ $agent == codex ]]; then
    _codex_pane_sid "$session" "$dir" "$cpid"
  fi
  return 0
}

# _dev_session_summary <session> <dir> — one-line "what it's working on" for a dev
# session: the title of its Claude transcript, resolved by the AUTHORITATIVE id
# (_dev_session_sid — registry-first, stamp validated against the slot's repo). When
# that yields nothing (truly pre-hook session, or only a stale cross-repo stamp) it
# defers to _dev_summary_for_pid (birthtime match on the LIVE claude). None → "".
_dev_session_summary() {
  setopt local_options null_glob bare_glob_qual
  # <sid> is optional: callers that already resolved it (_dev_session_rows) pass it
  # in, because resolving it walks the process table and used to be done TWICE per
  # session — once for the row's id column and again in here.
  local session="$1" dir="$2" sid="${3:-}" agent tx
  agent=$(_dev_agent_of_session "$session")
  [[ -n $sid ]] || sid=$(_dev_session_sid "$session" "$dir")
  if [[ -n $sid ]]; then
    # A valid id (registry or validated stamp) always has its transcript under this
    # slot's own project dir, since the dir IS the conversation's cwd (claude); the
    # codex locator answers from the hook cache / sqlite instead.
    tx=$(_dev_agent_transcript "$agent" "$sid" "$dir") && { _transcript_title "$tx"; return 0; }
  fi
  if [[ $agent == codex ]]; then
    tx=$(_codex_live_transcript "$dir" "$(_dev_session_claude_pid "$session")")
    [[ -n $tx ]] && _transcript_title "$tx"
    return 0
  fi
  _dev_summary_for_pid "$dir" "$(_dev_session_claude_pid "$session")"
}

# _dev_fg_rows — emit FOREGROUND claude sessions (ones you ran directly in a terminal,
# NOT inside a dev-<repo>-<slot> tmux pane) in the same tab format as
# _dev_session_rows: "<sid>\t<cwd>\t<slot>\t<state>\t<context>\t<summary>". A
# foreground claude is a live process with comm `claude` that isn't the claude of any
# dev-* tmux pane. The session id + cwd come from the registry the claude-stamp-tmux
# SessionStart hook writes per live claude pid (~/.cache/claude-sessions/<pid>) — the
# reliable pid→id link, since macOS hides another process's env and csync scrambles
# transcript birthtimes; with the id we read the exact transcript title. A session
# started BEFORE the hook recorded it has no entry → cwd via `lsof`, summary
# "(foreground claude)". The slot label is "<repo>:fg" (repo from cwd, else basename).
# The claude THIS shell runs under is skipped so a session doesn't list itself; dead-pid
# registry entries are pruned here. Appended to _dev_session_rows (so `dev ls -r` shows
# them per host) and rendered by _dev_list.
_dev_fg_rows() {
  setopt local_options null_glob
  local reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions"
  # claude pids already owned by a dev-* tmux slot — exclude (listed as slots already).
  local -A inslot; local s p
  for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
    p=$(_dev_session_claude_pid "$s") && [[ -n $p ]] && inslot[$p]=1
  done
  # the claude THIS shell is running under (walk up $$), so we don't list ourselves.
  local me up=$$
  local -a claudes
  if _dev_snap_ok; then                     # snapshot: no ps fork per ancestor level
    while [[ -n $up && $up != 1 ]]; do
      _dev_agent_is_proc "${_DEV_PS_COMM[$up]:-}" && { me=$up; break; }
      up=${_DEV_PS_PPID[$up]:-}
    done
    for p in ${(k)_DEV_PS_COMM}; do
      [[ -n ${_DEV_PS_APP[$p]:-} ]] && continue     # a GUI bundle's agent core, not a session
      _dev_agent_is_proc "${_DEV_PS_COMM[$p]}" && claudes+=($p)
    done
    claudes=(${(no)claudes})                # assoc keys are unordered; pid order is stable
  else
    while [[ -n $up && $up != 1 ]]; do
      _dev_agent_is_proc "$(ps -o comm= -p $up 2>/dev/null)" && { me=$up; break; }
      up=$(ps -o ppid= -p $up 2>/dev/null | tr -d ' ')
    done
    claudes=(${(f)"$(ps -Axo pid,comm 2>/dev/null | awk '$0 ~ /\.app\/Contents\// {next} {n=$2; sub(/.*\//,"",n)} n=="claude"||n=="codex"||n~/^codex-(aarch64|x86_64)-/{print $1}')"})
  fi
  local -A live
  local pid cwd repo k label sid title summary context agent
  for pid in ${(@)claudes}; do
    live[$pid]=1
    [[ -n ${inslot[$pid]} || $pid == $me ]] && continue
    _dev_agent_nested "$pid" && continue          # part of another agent's session
    _dev_agent_is_service "$pid" && continue      # shared daemon, possibly reparented
    agent=$(_dev_agent_of_comm "${_DEV_PS_COMM[$pid]:-$(ps -o comm= -p $pid 2>/dev/null)}"); [[ -n $agent ]] || agent=claude
    sid= cwd=
    [[ -r $reg/$pid ]] && IFS=$'\t' read -r sid cwd < "$reg/$pid"
    [[ -n $cwd ]] || cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)   # Linux: no lsof needed
    [[ -n $cwd ]] || cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    [[ -n $cwd ]] || continue
    repo=$(_dev_repo_of_dir "$cwd" 2>/dev/null); repo=${repo%%$'\t'*}   # worktree-aware
    [[ -n $repo ]] || repo=${cwd:t}                                     # fall back to basename
    # Label is "<repo>:<short-sid>" — the short Claude session id makes each
    # foreground row UNIQUE (two `dot` foreground claudes were both "dot:fg" before)
    # and is the handle `t open <id>` reattaches by. The colon marks an fg row (tmux
    # slots use "<repo>-<num>"). A session with no registered id (a codex before its first
    # prompt mints the thread, a send-keys launch the hook never saw) is labelled by its
    # PID instead — "<repo>:p<pid>" (_dev_fg_label). It used to be a bare "<repo>:fg", so
    # three idle codexes in ff rendered as three identical "ff:fg" rows no handle could
    # tell apart ("they need some sort of uniq identifier", 2026-09-21).
    if [[ -n $sid ]]; then
      label="${repo}:${sid[1,8]}"
      local -a tx=( $(_dev_agent_transcript "$agent" "$sid" 2>/dev/null) )
      title=$([[ -n ${tx[1]} ]] && _transcript_title "${tx[1]}")
      if [[ -n $title ]]; then context=active; summary=$title
      else context=idle; summary='(idle — no conversation)'; fi
    else
      _dev_fg_label "$repo" - "$pid"; label=$REPLY; sid='-'; context=unknown; summary="(foreground $agent)"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sid" "$cwd" "$label" attached "$context" "$summary" "$agent"
  done
  # prune registry entries whose pid is no longer a live claude (sessions that ended)
  local f bpid
  for f in "$reg"/*(N.); do bpid=${f:t}; [[ -z ${live[$bpid]} ]] && rm -f "$f"; done
  # the prune loop exits 1 when its last entry is live ([[ ]] && short-circuit), and
  # as the last command here that became _dev_session_rows' status — making remote
  # scans look failed to _dev_rows_all, which drops their (good) rows.
  return 0
}

# _dev_fg_label <repo> <sid|-> <pid> — the handle an fg row is shown and addressed by:
# "<repo>:<short sid>" when the registry knows the session id, else "<repo>:p<pid>". Both
# are unique on their host; the `p` keeps a pid from reading as a hex id prefix or a slot
# number. ONE home for it: _dev_fg_rows renders the label and _dev_fg_pids matches on it,
# so the two must never spell it differently. Returns through $REPLY (no fork per row).
_dev_fg_label() {
  if [[ -n $2 && $2 != - ]]; then REPLY="$1:${2[1,8]}"; else REPLY="$1:p$3"; fi
}

# _dev_agent_nested <pid> — true when an ANCESTOR of <pid> is itself an agent process: then
# <pid> is part of that agent's session (a helper it spawned, a `claude -p` its tool ran),
# never a foreground session of its own. Both fg producers skip such pids, which makes the
# fg list robust to whatever an agent runs under itself — not only to the helper names
# _dev_agent_is_proc already refuses. Snapshot walk when one is fresh, else ps forks.
_dev_agent_nested() {
  local up comm n=0
  if _dev_snap_ok; then
    up=${_DEV_PS_PPID[$1]:-}
    while [[ -n $up && $up != 1 && $up != 0 ]] && (( n++ < 64 )); do
      _dev_agent_is_proc "${_DEV_PS_COMM[$up]:-}" && return 0
      up=${_DEV_PS_PPID[$up]:-}
    done
    return 1
  fi
  up=$(ps -o ppid= -p "$1" 2>/dev/null | tr -d ' ')
  while [[ -n $up && $up != 1 && $up != 0 ]] && (( n++ < 64 )); do
    comm=$(ps -o comm= -p "$up" 2>/dev/null)
    _dev_agent_is_proc "$comm" && return 0
    up=$(ps -o ppid= -p "$up" 2>/dev/null | tr -d ' ')
  done
  return 1
}

# _DEV_FG_MATCH_AWK — the fg-handle rules as one awk function, fgm(label, sid), shared by
# the local matcher (_dev_fg_match) and the two cross-host scans (_dev_remote_fg_kill /
# _dev_remote_fg_open), which had each open-coded them. Reads awk vars h (the handle),
# idp (its part after the last `:`), isrepo (non-empty = h is a DEV_REPOS key) and ro
# (non-empty = a DEV_REPOS key may still match its repo's fg rows — the open verbs).
# A row matches on: its exact label; its repo part (gated as above); `<repo>:fg`, the old
# id-less label, kept as "that repo's fg rows" so existing habits still resolve; a bare
# `p<pid>` against a pid label; or a bare short session-id prefix (never for a repo key).
_DEV_FG_MATCH_AWK='
function fgm(label, sid,   repo, lid) {
  repo = label; sub(/:.*/, "", repo); lid = label; sub(/^[^:]*:/, "", lid)
  if (label == h) return 1
  if (h == repo && (isrepo == "" || ro != "")) return 1
  if (h ~ /:fg$/ && substr(h, 1, length(h) - 3) == repo) return 1
  if (h ~ /^p[0-9]+$/ && lid == h) return 1
  if (index(h, ":") == 0 && isrepo == "" && sid != "-" && index(sid, idp) == 1) return 1
  return 0
}'

# _dev_pid_for_sid <sid> — print the live `claude` pid that owns session <sid>, via
# the claude-stamp-tmux registry (~/.cache/claude-sessions/<pid> = "<sid>\t<cwd>").
# The reverse of _dev_fg_rows' pid→sid read: _dev_adopt_fg needs the pid to STOP the
# foreground owner before resuming. Empty if no live pid maps to <sid> (already gone
# → resuming is safe anyway). A `-` sid (pre-registry) never matches, by design.
_dev_pid_for_sid() {
  local sid="$1" reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions" f bsid bcwd
  [[ -n $sid && $sid != - ]] || return 1
  for f in "$reg"/*(N.); do
    IFS=$'\t' read -r bsid bcwd < "$f"
    [[ $bsid == $sid ]] && kill -0 ${f:t} 2>/dev/null && { print -r -- ${f:t}; return 0; }
  done
  return 1
}

# _dev_fg_handle <arg> — is <arg> shaped like a foreground-session handle? True for
# the displayed `repo:id` / `repo:p<pid>` label (any `:`), a bare `p<pid>`, and for a short session-id prefix: 4+ chars,
# all hex, at least one letter — pure digits stay tmux slot numbers, so `t open dot
# 1234` can never be stolen by a (rare) all-digit id prefix; type more chars or the
# repo:id form for those. The dispatcher `_t_dev` uses this to tell slots from ids.
_dev_fg_handle() {
  local h="$1"
  [[ $h == *:* || $h == p<-> ]] && return 0
  (( ${#h} >= 4 )) || return 1
  [[ $h != *[^0-9a-f]* && $h == *[a-f]* ]]
}

# _dev_adopt_fg [repo|id] — reattach a FOREGROUND (:fg) session by MOVING it here. A
# foreground claude is bound to its own terminal (no tmux to attach to), so the only
# way to "get into it" from elsewhere is to stop that owner and resume the conversation
# in THIS terminal — matching how it is running (the tmux-slot analog is `t open <repo>
# <slot>` / `t beam … --from`). One-live-owner: Claude takes no transcript lock, so two live
# resumers of one id diverge — hence the SIGTERM-then-wait before `claude -r`, mirroring
# t pop. Only :fg rows whose id the registry recorded (sid != `-`) are resumable; a
# pre-registry one must be reattached from its own terminal. This is the LAST of the three
# ways `t open` gets into an fg row — _dev_open_fg is the dispatcher, and it tries
# attaching in place (here, then on the host that has it) first, since a row can also be
# an agent in a non-dev tmux session, which is attachable and carries no id to resume.
# Reached through `t open` (the one get-into-a-session verb; the old `t fg` is gone):
#   • `t open <id>`      — the short session id shown in `t ls` (e.g. `t open a4aa5f6a`,
#                          or the displayed `dot:a4aa5f6a`) → that exact session.
#                          `t open <repo> <id>` works too — the id alone decides.
#   • `t open <repo> fg` — that repo's foreground sessions (one → use it, several →
#                          fzf-pick); the `fg` slot keyword mirrors the `:fg` label.
_dev_adopt_fg() {
  local arg="$1" rows
  # all foreground rows (the colon in the slot label marks fg; tmux slots use a dash)
  rows=$(_dev_fg_rows 2>/dev/null | awk -F'\t' '$3 ~ /:/')
  [[ -n $rows ]] || { echo "t open: no foreground claude running (see \`t session list\`)." >&2; return 1; }
  local resumable; resumable=$(print -r -- "$rows" | awk -F'\t' '$1!="-"')
  # An id-less row (label `<repo>:p<pid>`) named exactly: say why it cannot be moved, not
  # "no match" — it is right there in `t ls`. It has no tmux either (_dev_attach_fg tried).
  if [[ -n $arg ]] && print -r -- "$rows" | awk -F'\t' -v a="$arg" '$1=="-" && ($3==a || substr($3, index($3, ":")+1)==a) {f=1} END {exit !f}'; then
    echo "t open: '$arg' has no session id recorded, so it cannot be moved here — and it runs in no tmux session to attach." >&2
    echo "  (a codex mints its id at the first prompt; an agent launched by send-keys is never registered)" >&2
    echo "  Get into it from the terminal it runs in, or \`t kill $arg\` it." >&2
    return 1
  fi
  [[ -n $resumable ]] || { echo "t open: foreground session(s) have no session id recorded — reattach from their own terminal." >&2; return 1; }
  if [[ -n $arg ]]; then
    local sel
    if [[ -n ${DEV_REPOS[$arg]} ]]; then            # an exact repo key → that repo's rows
      sel=$(print -r -- "$resumable" | awk -F'\t' -v r="$arg" 'index($3, r":")==1')
    else                                            # else a session id / short id (maybe "<repo>:<id>")
      local idpart="${arg##*:}"
      sel=$(print -r -- "$resumable" | awk -F'\t' -v p="$idpart" 'index($1,p)==1')
    fi
    [[ -n $sel ]] || { echo "t open: no foreground session matching '$arg' (see \`t session list\`)." >&2; return 1; }
    resumable=$sel
  else
    # Repo-aware: no-arg adoption prefers this repo's foreground claudes (row cwd in
    # field 2, under the $PWD repo dir); outside a repo — or no match — all of them.
    local scope; scope=$(_dev_cwd_repo_dir)
    if [[ -n $scope ]]; then
      local insc; insc=$(print -r -- "$resumable" | awk -F'\t' -v d="$scope" '$2==d || index($2, d"/")==1')
      [[ -n $insc ]] && resumable=$insc
    fi
  fi
  local sid cwd agent
  local -a lines=( ${(f)resumable} )
  if (( ${#lines} == 1 )); then
    IFS=$'\t' read -r sid cwd _ _ _ _ agent <<< "${lines[1]}"
  else
    [[ -t 0 && -t 1 ]] || { echo "t open: several foreground sessions — name one (\`t session open <id>\`) or pick from a terminal:" >&2; print -r -- "$resumable" | awk -F'\t' '{printf "  %s  %s\n",$3,$6}' >&2; return 1; }
    local pick
    pick=$(print -r -- "$resumable" | awk -F'\t' '{printf "%s\t%s\t%s\n", $1, $3, $6}' \
             | _t_fzf --with-nth=2.. --delimiter='\t' --prompt="t open > ") || return 1
    sid=${pick%%$'\t'*}
    cwd=$(print -r -- "$resumable" | awk -F'\t' -v s="$sid" '$1==s{print $2; exit}')
    agent=$(print -r -- "$resumable" | awk -F'\t' -v s="$sid" '$1==s{print $7; exit}')
  fi
  [[ -n $sid ]] || return 1
  _dev_agent_valid "$agent" || agent=claude   # a 6-field row (older producer) is claude

  echo "Adopting foreground session → here ($(_dev_agent_resume_cmd "$agent" "${sid[1,8]}…") in $cwd)"
  # Stop the foreground owner and wait for it to actually exit (≤5s) before resuming,
  # so only one live claude ever holds the id — same race tpop guards against.
  local pid; pid=$(_dev_pid_for_sid "$sid")
  if [[ -n $pid ]]; then
    kill -TERM "$pid" 2>/dev/null
    local n=0
    while kill -0 "$pid" 2>/dev/null && (( n++ < 100 )); do sleep 0.05; done
    kill -0 "$pid" 2>/dev/null && \
      echo "warning: foreground owner ($pid) didn't exit; resuming anyway — transcript may interleave." >&2
  fi
  cd "$cwd" || return 1
  if [[ $agent == codex ]]; then codex resume "$sid"; else claude -r "$sid"; fi
}

# _dev_attach_fg <handle> — ATTACH IN PLACE an fg-row session that actually lives in a
# tmux session. `t open` never moves a session; _dev_adopt_fg's stop-and-resume is the
# exception forced by a TRUE foreground agent having no tmux to attach to — but a
# `<repo>:<id>` / `:fg` row can equally be an agent inside a NON-dev tmux session
# (a claude you started in a tmux session yourself), and those attach like any slot.
# This is `t open`'s
# companion to _dev_kill_fg, which has reached those rows since the `t kill dotfiles-pr47`
# gap: before this, `t open dotfiles-pr136` said "no foreground session" and could not
# have adopted it either — the now-retired pr-watch launched claude by `send-keys`, so
# the SessionStart hook never registered it and the row carried no id to resume; any
# `send-keys`-launched agent has that shape. Returns 0 handled (attached,
# or told the user exactly which handle to name), 1 if <handle> matched rows but none live
# in tmux (the caller falls through to the adopt/move path), 2 if nothing matched at all
# (the caller may look at the other machines). Repo-key matching is ON (see _dev_fg_match):
# every caller has already decided this is an fg request, so `t open <repo> fg` means that
# repo's fg rows, not its dev slots.
_dev_attach_fg() {
  local handle="$1"
  [[ -n $handle ]] || return 2
  local rows; rows=$(_dev_fg_match "$handle" 1) || return 2
  local line pid tsess label
  local -a att=()
  for line in ${(f)rows}; do
    pid=${line%%$'\t'*}
    tsess=$(_dev_tmux_session_of_pid "$pid") || continue
    att+=( "${tsess}"$'\t'"${line}" )          # tsess(1) pid(2) sid(3) cwd(4) label(5) agent(6)
  done
  (( ${#att} )) || return 1
  local sel
  if (( ${#att} == 1 )); then
    sel=${att[1]}
  elif [[ -t 0 && -t 1 ]] && command -v fzf >/dev/null 2>&1; then
    sel=$(print -rl -- "${att[@]}" | _t_fzf --with-nth=5 --delimiter=$'\t' --prompt="t open > ") || return 0
  else
    echo "t open: several tmux'd sessions match '$handle' — name one:" >&2
    print -rl -- "${att[@]}" | awk -F'\t' '{printf "  t session open %s   (tmux %s)\n", $5, $1}' >&2
    return 0
  fi
  tsess=${sel%%$'\t'*}
  label=$(print -r -- "$sel" | awk -F'\t' '{print $5}')
  echo "Attaching $label in place (tmux session $tsess)"
  if [[ ! -t 1 || -n $CLAUDE_CODE_SESSION_ID ]]; then
    echo "  Attach: tmux attach -t $tsess"       # no TTY, or we are inside an agent
  elif [[ -n $TMUX ]]; then
    tmux switch-client -t "=$tsess:"               # tmux refuses a nested attach
  else
    # Title before attaching: tmux (set-titles off) swallows OSC from inside the pane,
    # so this pre-attach write is the one that sticks (same reason as the slot landing).
    _term_title "$tsess"
    tmux attach-session -t "=$tsess:"
  fi
  return 0
}

# _dev_open_fg <handle> — the one `t open` path for a foreground-row handle, tried in the
# order that never moves a session it could have attached instead:
#   1. attach it in place if it lives in a tmux session HERE (_dev_attach_fg);
#   2. else, if nothing here answers <handle> at all, attach it on the host that does
#      (_dev_remote_fg_open — the fg rows `t ls -r` shows but _dev_remote_resolve drops);
#   3. else adopt it (_dev_adopt_fg) — stop the owner and resume the conversation in this
#      terminal, the only way into a true foreground agent.
# Step 2 runs only on a local MISS: a local row that matched but has no tmux is step 3's
# job, not another machine's. Step 3 also owns the error messages for a true miss, so a
# handle nothing anywhere answers reports once, from the path that knows the local rows.
_dev_open_fg() {
  local handle="$1" rc
  _dev_attach_fg "$handle"; rc=$?
  (( rc == 0 )) && return 0
  if (( rc == 2 )) && (( ${#REMOTE_HOSTS} )); then
    _dev_remote_fg_open "$handle"; rc=$?
    (( rc != 2 )) && return $rc
  fi
  _dev_adopt_fg "$handle"
}

# _dev_list — print every dev-<repo>-<slot> tmux session, compact enough to read
# on a phone (Termius). One line per session: a two-glyph STATUS field + short
# name + what it's working on. The `dev-` prefix and the redundant
# "attached/detached" word are dropped (the glyph already says it) and the full
# repo path is dropped (it's in the name) so the line fits a narrow screen; the
# summary is truncated to $COLUMNS so it never wraps. The two glyphs are
# space-separated ("○ ✓", not "○✓") so the orthogonal states read as two columns,
# under a STATUS header. Glyphs:
#   ● attached / ○ detached   (any client viewing it)
#   ✓ active context          a live claude that has actually loaded a conversation.
#                             Blank for: a claude parked on its startup splash
#                             (_dev_session_at_welcome — "idle, no conversation"),
#                             and for a session that's exited to a shell ("no active
#                             session"). Independent of attach state.
# Shared by `dev list` and `dev ls`. With a <scope> dir arg, only sessions whose
# working dir is that dir (or below) are listed — `dev ls` passes the current repo.
#
# _dev_repo_of_dir <dir> — the single source of truth for "which repo does this path
# belong to". Prints "<alias>\t<slot>": the DEV_REPOS alias, plus a slot number when
# <dir> is a per-session worktree ($DEV_WORKTREE_ROOT/<basename>/<slot>); the slot is
# empty for a canonical repo dir or a subdir of one. The basename is the stable key
# (a dir is `dot` here, `dotfiles` there, but its basename is identical), so worktrees
# resolve back to whatever alias is in use locally. Alias choice for a basename mirrors
# _t_infer_repo's old rule: the key equal to the basename wins, else the shortest key.
# Prints nothing / rc 1 outside any DEV_REPOS dir. Reused by every cwd→repo resolver
# below (and the Python twin _repo_of_dir in bin/t) so worktree-awareness lives once.
_dev_repo_of_dir() {
  local dir="$1" base= slot= k best=
  if [[ -n $DEV_WORKTREE_ROOT && $dir == $DEV_WORKTREE_ROOT/*/* ]]; then
    local rest=${dir#$DEV_WORKTREE_ROOT/}   # <basename>/<slot>[/...]
    base=${rest%%/*}; rest=${rest#*/}; slot=${rest%%/*}
  else
    local d bestlen=0                       # canonical dir or subdir; longest/most-specific wins
    for k in ${(k)DEV_REPOS}; do
      d=${DEV_REPOS[$k]}
      [[ $dir == $d || $dir == $d/* ]] || continue
      (( ${#d} > bestlen )) && { base=${d:t}; bestlen=${#d}; }
    done
    [[ -n $base ]] || return 1
  fi
  for k in ${(k)DEV_REPOS}; do
    [[ ${DEV_REPOS[$k]:t} == $base ]] || continue
    [[ $k == $base ]] && { print -r -- "$k	$slot"; return 0 }
    if [[ -z $best ]] || (( ${#k} < ${#best} )); then best=$k; fi
  done
  [[ -n $best ]] && { print -r -- "$best	$slot"; return 0 }
  return 1
}

# _dev_dir_in_scope <dir> <scope> — true when <dir> belongs to the repo whose canonical
# dir is <scope>: an exact/ancestor match OR a per-session worktree of that repo (which
# lives under $DEV_WORKTREE_ROOT/<basename>/, not under <scope>). The scope filters in
# `dev ls` use this so worktree sessions are listed under their repo, not dropped.
_dev_dir_in_scope() {
  local d="$1" scope="$2"
  [[ $d == $scope || $d == $scope/* ]] && return 0
  [[ -n $DEV_WORKTREE_ROOT && $d == $DEV_WORKTREE_ROOT/${scope:t}/* ]]
}

# _dev_homerel <path> — strip the machine-local home prefix (/Users/<u>/ on macOS,
# /home/<u>/ on Linux) so REMOTE paths compare against local ones on hosts whose
# $HOME differs (a Linux node is /home/<u>; Macs are /Users/<u>). The cross-host
# key becomes the home-relative form (`code/dotfiles`); a path outside any home
# passes through absolute. The awk twin used on remote-row cwds is
#   { c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c) }
# — keep the two in sync. (Python twin: _homerel in bin/t.)
_dev_homerel() { print -r -- "${1#/(Users|home)/*/}" }

# _dev_ensure_session_cwd <cwd> — print a directory in which a session recorded at <cwd>
# can be resumed, materializing it on demand. <cwd> still present → echo it unchanged.
# <cwd> is a per-session worktree that is ABSENT here — a transcript synced from another
# machine (only the branch + transcript travel, never the ephemeral worktree) or a slot
# whose worktree was reaped after merge — → rebuild it from its branch on origin via
# _dev_worktree_create and echo the (identical) path, so `claude -r` lands on the same cwd
# the transcript recorded. Returns 1 when <cwd> is gone and is not a recoverable worktree
# (a non-DEV dir, or a worktree-opt-out repo) so the caller can error. This is what
# restores manual resume-through-sync under worktree-per-session: the worktree dir is
# ephemeral, but its branch + transcript are durable, so we rebuild the dir when needed —
# the same engine tbeam's _tbeam_land / _dev_pull already use on the receive side.
_dev_ensure_session_cwd() {
  local cwd="$1"
  [[ -d $cwd ]] && { print -r -- "$cwd"; return 0; }
  local r repo slot; r=$(_dev_repo_of_dir "$cwd") || return 1
  repo=${r%%$'\t'*}; slot=${r#*$'\t'}
  [[ -n $repo && -n $slot ]] || return 1   # not a worktree path → nothing to rebuild
  _dev_worktree_enabled "$repo" || return 1
  _dev_worktree_create "$repo" "$slot"     # prints the rebuilt path, or returns 1
}

# _dev_cwd_repo_dir — print the canonical DEV_REPOS directory that contains $PWD (or
# whose worktree contains $PWD), else nothing. The scope source for `dev ls`. Derived
# from _dev_repo_of_dir so it is worktree-aware: standing inside a slot's worktree still
# scopes `dev ls` to that repo.
_dev_cwd_repo_dir() {
  local r; r=$(_dev_repo_of_dir "$PWD") || return 0
  [[ -n $r ]] && print -r -- "${DEV_REPOS[${r%%$'\t'*}]}"
}

# _t_infer_repo [slot] — the repo ALIAS implied by $PWD, for the repo-aware verb
# defaults (`t paste 4` in ~/code/financial-forecast → that repo's slot 4; bare
# `t open` → the repo you're standing in). _dev_cwd_repo_dir gives the DIR, but
# slot verbs target session NAMES (dev-<alias>-<slot>) and several aliases can key
# one dir (dot-* and dotfiles-* both root at ~/code/dotfiles) — so a LIVE dev-*
# session rooted in this repo dir wins and the alias is read off the actual
# session name: the exact <slot>'s session when one is given, else the dir's
# first live session (so a bare `t open`/`t paste` joins the alias already in
# use here instead of minting a sibling slot under a second alias). No live
# session → the DEV_REPOS key for the dir: the key matching its basename, else
# the shortest (the customary shorthand). Prints nothing / rc 1 outside any
# DEV_REPOS dir, so callers can drop to their usage text.
_t_infer_repo() {
  local slot="$1" dir; dir=$(_dev_cwd_repo_dir)
  [[ -n $dir ]] || return 1
  # Two passes when a slot is given: that exact slot's session first, then any
  # live session here (a not-yet-live slot still joins the in-use alias).
  local -a pats=("[0-9]\{1,\}")
  [[ $slot == <-> ]] && pats=("$slot" "[0-9]\{1,\}")
  local pat s p
  for pat in $pats; do
    for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -- "^dev-.*-${pat}\$")"}; do
      p=$(tmux display-message -p -t "=$s:" '#{session_path}' 2>/dev/null)
      if _dev_dir_in_scope "$p" "$dir"; then           # exact, subdir, or a worktree of the repo
        s=${s#dev-}; print -r -- "${s%-*}"; return 0   # last dash splits off the slot
      fi
    done
  done
  local k best=
  for k in ${(k)DEV_REPOS}; do
    [[ ${DEV_REPOS[$k]} == $dir ]] || continue
    [[ $k == ${dir:t} ]] && { print -r -- "$k"; return 0 }
    if [[ -z $best ]] || (( ${#k} < ${#best} )); then best=$k; fi
  done
  [[ -n $best ]] || return 1
  print -r -- "$best"
}

_dev_list() {
  local scope="$1"
  local names
  names=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-' | sort)
  # Scope to the current repo dir (path-based) when asked: keep only sessions whose
  # session_path is <scope> or below it.
  if [[ -n $scope ]]; then
    local kept=() _s _d
    while IFS= read -r _s; do
      [[ -n $_s ]] || continue
      _d=$(tmux display-message -p -t "=$_s:" '#{session_path}' 2>/dev/null)
      _dev_dir_in_scope "$_d" "$scope" && kept+=("$_s")
    done <<< "$names"
    names=${(F)kept}
  fi
  # Foreground (non-tmux) claudes, same scope (rows carry cwd in field 2). Worktree
  # paths ($DEV_WORKTREE_ROOT/<basename>/...) count as the repo too — the wt clause.
  local fgrows; fgrows=$(_dev_fg_rows 2>/dev/null)
  [[ -n $scope && -n $fgrows ]] && fgrows=$(print -r -- "$fgrows" | awk -F'\t' \
    -v d="$scope" -v wt="${DEV_WORKTREE_ROOT}/${scope:t}" \
    '$2==d || index($2, d"/")==1 || index($2, wt"/")==1')
  if [[ -z "$names" && -z "$fgrows" ]]; then
    echo "No dev sessions running${scope:+ in ${scope:t} (dev ls --all for every repo)}."
    return 0
  fi
  local g c y b r0=
  if [[ -t 1 ]]; then g=$'\e[32m'; c=$'\e[36m'; y=$'\e[2m'; b=$'\e[1m'; r0=$'\e[0m'; fi
  # widest name (dev slot sans dev-, plus foreground "<repo>:fg" labels) so WORKING ON lines up
  local s short name_w=7
  while IFS= read -r s; do [[ -n $s ]] || continue; short="${s#dev-}"; (( ${#short} > name_w )) && name_w=${#short}; done <<< "$names"
  local _fsid _fcwd _fslot _frest
  while IFS=$'\t' read -r _fsid _fcwd _fslot _frest; do [[ -n $_fslot ]] || continue; (( ${#_fslot} > name_w )) && name_w=${#_fslot}; done <<< "$fgrows"
  # prefix before the summary = 2 (indent) + 8 (STATUS field) + name_w + 1 (gap)
  local avail=$(( ${COLUMNS:-80} - 11 - name_w ))
  (( avail < 12 )) && avail=$(( 80 - 11 - name_w ))
  print -r -- "dev sessions   ${g}●${r0} attached · ${c}✓${r0} active context${scope:+   ${y}(repo: ${r0}${b}${c}${scope:t}${r0}${y} — --all for all)${r0}}"
  print -r -- ""
  printf '  %s%-8s%-*s %s%s\n' "$y" 'STATUS' $name_w 'SESSION' 'WORKING ON' "$r0"
  local state dir amark cmark summary
  while IFS= read -r s; do
    [[ -n $s ]] || continue
    short="${s#dev-}"
    state=$(tmux display-message -p -t "=$s:" '#{?session_attached,attached,detached}' 2>/dev/null)
    dir=$(tmux display-message -p -t "=$s:" '#{session_path}' 2>/dev/null)
    if [[ $state == attached ]]; then amark="${g}●${r0}"; else amark='○'; fi
    if ! _dev_session_has_claude "$s"; then
      cmark=' '; summary='(no active session)'
    elif _dev_agent_at_welcome "$(_dev_agent_of_session "$s")" "$s"; then
      cmark=' '; summary='(idle — no conversation)'
    else
      cmark="${c}✓${r0}"
      summary=$(_dev_session_summary "$s" "$dir")
      [[ -n $summary ]] || summary='(untitled session)'
    fi
    (( ${#summary} > avail )) && summary="${summary[1,avail-1]}…"
    # STATUS field (8 cols): "<amark> <cmark>" = 3 visible glyph cols + 5 pad
    printf '  %s %s     %-*s %s%s%s\n' "$amark" "$cmark" $name_w "$short" "$y" "$summary" "$r0"
  done <<< "$names"
  # foreground rows: always ● (you're in the terminal); ✓ when context is active
  local fstate fcontext fsummary _fagent
  while IFS=$'\t' read -r _fsid _fcwd _fslot fstate fcontext fsummary _fagent; do
    [[ -n $_fslot ]] || continue
    [[ $fcontext == active ]] && cmark="${c}✓${r0}" || cmark=' '
    (( ${#fsummary} > avail )) && fsummary="${fsummary[1,avail-1]}…"
    printf '  %s %s     %-*s %s%s%s\n' "${g}●${r0}" "$cmark" $name_w "$_fslot" "$y" "$fsummary" "$r0"
  done <<< "$fgrows"
  # reattach legend: tmux slots via `t open <repo> <slot>`; a foreground (:fg) row is
  # bound to its terminal, so it comes back foreground via `t open <id>`. Kill a :fg row
  # (a foreground claude, or one in a tmux session of its own) with `t kill <id>` — not a
  # dev slot, so `t kill <repo> <slot>` does not apply. Both shown only when a :fg row exists.
  local foot="reattach: t open <repo> <slot>"
  [[ -n $fgrows ]] && foot+=" · foreground: t open <session> · kill: t kill <session>"
  print -r -- ""
  print -r -- "  ${y}${foot}${r0}"
}

# _dev_session_rows — machine-readable sibling of _dev_list: one tab-separated
# "<sid>\t<cwd>\t<slot>\t<state>\t<context>\t<summary>" row per live dev-<repo>-<slot>
# tmux session (sid = the stamped CLAUDE_RESUME_ID; slot = the name minus `dev-`;
# state = attached|detached; context = active|idle|none — the same distinction the
# ✓ glyph draws; summary = the "what it's working on" line). No glyphs/colors/headers
# — it's meant to be *collected* (locally and over ssh, like _claude_session_rows)
# and re-rendered. This is the per-host scan behind `dev ls -r`: live dev slots are
# what genuinely differ machine-to-machine (transcripts already converge via csync),
# so the cross-host view lists these, not transcripts.
# Loaded desktop conversations, read without starting or contacting an app-server.
_dev_desktop_threads() {
  [[ $OSTYPE == darwin* && -r ${CODEX_HOME:-$HOME/.codex}/state_5.sqlite ]] || return 0
  python3 "$T_HOME/libexec/t_desktop.py" 2>/dev/null
}

_dev_session_rows() {
  # Rich display rows opt into a trailing model field. Ownership consumers keep
  # the established seven-field contract and its authoritative targeting ids.
  if [[ ${1:-} == --details ]]; then
    _dev_session_rows | python3 "$T_HOME/libexec/t_session_details.py"
    return
  fi
  setopt local_options null_glob bare_glob_qual
  # Two passes, because the expensive part is per-transcript and batches. Pass 1
  # asks tmux and the process table (ONE list-sessions carrying name+path+attached,
  # instead of two display-message forks per session, over the shared
  # _dev_ps_snapshot); pass 2 prints, after ONE _transcript_meta_batch has read
  # every title at once rather than forking python3 per slot.
  local -a rows rowtx tpaths tx mrows f _PR_STALE
  local -A title_of pr_of _PR_SPAWNED had_path desktop_sid desktop_title desktop_ambiguous
  local s short sid psid dir state context summary agent i mr mrest REPLY
  _dev_ps_snapshot
  while IFS=$'\t' read -r sid dir summary; do
    [[ -n $sid && -n $dir ]] || continue
    if [[ -n ${desktop_sid[$dir]:-} ]]; then desktop_ambiguous[$dir]=1; fi
    desktop_sid[$dir]=$sid; desktop_title[$dir]=$summary
  done < <(_dev_desktop_threads)
  # Several desktop conversations may share a folder; never guess the slot owner.
  for dir in ${(k)desktop_ambiguous}; do unset "desktop_sid[$dir]"; done
  while IFS=$'\t' read -r s dir state; do
    [[ $s == dev-* && -n $dir ]] || continue
    had_path[$dir]=1
    short="${s#dev-}"
    agent=$(_dev_agent_of_session "$s")
    # Authoritative id (registry-first, stamp validated against the slot's repo) —
    # not the raw CLAUDE_RESUME_ID stamp, which a reused slot can carry stale from a
    # prior (even cross-repo) occupant; this is the targeting id callers act on.
    sid=$(_dev_session_sid "$s" "$dir")
    # `-` sentinel when the slot's conversation id is unknown: keeps every
    # field non-empty so a tab is never a *leading/consecutive* IFS-whitespace
    # delimiter that `read` would collapse, sliding the columns. It also keeps the
    # rows[] records below splittable with (ps:\t:).
    [[ -n $sid ]] || sid='-'
    tx=()
    if ! _dev_session_has_claude "$s"; then
      context=none
      # A retained tmux pane reserves the slot after desktop handoff. Its
      # attachment status describes the shell, not the conversation owner.
      if [[ $agent == codex ]] && { [[ -n ${desktop_sid[$dir]:-} ]] || _dev_app_slot_reserved "$dir"; }; then
        state=app
        if [[ -n ${desktop_sid[$dir]:-} ]]; then
          sid=${desktop_sid[$dir]}; context=active
        fi
        [[ $sid != - ]] && tx=( "$(_dev_agent_transcript codex "$sid" "$dir" 2>/dev/null)" )
      fi
    elif _dev_agent_at_welcome "$agent" "$s" "$dir"; then
      context=idle
    else
      context=active
      # A valid id always has its transcript under this slot's own project dir,
      # since the dir IS the conversation's cwd. Collect it for the batch; a slot
      # with no id (or no transcript) falls back to _dev_session_summary in pass 2,
      # which owns the birthtime-matching heuristic for that case. (claude only —
      # a codex slot's rollout is not cwd-keyed; its title lands with the seam's
      # transcript locator.)
      if [[ $agent == claude ]]; then
        [[ $sid != - ]] && tx=( "$HOME/.claude/projects/${dir//[^A-Za-z0-9]/-}/$sid".jsonl(N) )
      else
        # A fallback title never upgrades the row's unknown targeting id.
        tx=( "$(_dev_agent_transcript codex "$sid" "$dir" 2>/dev/null || _codex_live_transcript "$dir" "$(_dev_session_claude_pid "$s")")" )
      fi
    fi
    rows+=("$sid"$'\t'"$dir"$'\t'"$short"$'\t'"$state"$'\t'"$context"$'\t'"$agent")
    if [[ -n ${tx[1]:-} ]]; then rowtx+=("${tx[1]}"); tpaths+=("${tx[1]}")
    else rowtx+=('-'); fi
  done < <(tmux list-sessions -F "#{session_name}"$'\t'"#{session_path}"$'\t'"#{?session_attached,attached,detached}" 2>/dev/null | sort)

  (( $#tpaths )) && mrows=("${(@f)$(_transcript_meta_batch "${(@)tpaths}")}")
  for mr in "${(@)mrows}"; do
    [[ -n $mr ]] || continue
    mrest=${mr#*$'\t'}                      # peeled, not split: an empty title must
    title_of[${mr%%$'\t'*}]=${mrest%%$'\t'*}  # not collapse the column away
    pr_of[${mr%%$'\t'*}]=${mrest#*$'\t'}     # the batch already read the PR url
  done

  for (( i = 1; i <= $#rows; i++ )); do
    f=("${(@ps:\t:)rows[$i]}")
    sid=$f[1]; dir=$f[2]; short=$f[3]; state=$f[4]; context=$f[5]; agent=$f[6]
    case $context in
      none)
        if _dev_app_slot_reserved "$dir"; then
          summary=${title_of[${rowtx[$i]}]:-'(Codex desktop workspace — reopen with t open --app)'}
        else
          summary='(no active session)'
        fi ;;
      idle) summary='(idle — no conversation)' ;;
      *)
        if [[ ${rowtx[$i]} != - ]]; then
          summary=${title_of[${rowtx[$i]}]:-}
        else
          psid=$sid; [[ $psid == - ]] && psid=
          summary=$(_dev_session_summary "dev-$short" "$dir" "$psid")
        fi
        [[ $state == app && -n ${desktop_title[$dir]:-} ]] && summary=${desktop_title[$dir]}
        [[ -n $summary ]] || summary="(untitled $agent session)"
        # " · #N <state>" for the session's PR — the same tag `t resume` renders,
        # from the same cache, so a slot whose PR has landed says so where you
        # actually look at slots. It rides on the summary instead of a column of
        # its own because _transcript_meta_batch caps titles at 50 chars, leaving
        # title+tag comfortably inside WORKING ON at any sane width — and because a
        # dedicated column would sit empty for every slot that has not opened a PR
        # yet, which is most of them. Only a row with a known transcript can carry
        # one; the _dev_session_summary fallback below never resolved a URL.
        if [[ ${rowtx[$i]} != - ]]; then _pr_state_tag "${pr_of[${rowtx[$i]}]:-}" "$dir"; summary+=$REPLY; fi
        ;;
    esac
    # field 7 = agent (claude|codex): trailing, so every front-indexed consumer and a
    # stale host's 6-field parser keep working (bin/t _parse_rows defaults it to claude)
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sid" "$dir" "$short" "$state" "$context" "$summary" "$agent"
  done
  # Desktop-only workspaces have no tmux session, but their private Git marker
  # reserves the slot. Surface them in t ls without claiming an active agent.
  local wt match app_repo app_slot
  for wt in $DEV_WORKTREE_ROOT/*/<->(N/); do
    [[ -e $wt/.git && -z ${had_path[$wt]:-} ]] || continue
    [[ -n ${desktop_sid[$wt]:-} ]] || _dev_app_slot_reserved "$wt" || continue
    match=$(_dev_repo_of_dir "$wt") || continue
    app_repo=${match%%$'\t'*}; app_slot=${match#*$'\t'}
    [[ -n $app_repo && $app_slot == <-> ]] || continue
    sid=${desktop_sid[$wt]:--}; context=none; summary='(Codex desktop workspace — reopen with t open --app)'
    if [[ $sid != - ]]; then context=active; summary=${desktop_title[$wt]}; fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sid" "$wt" "$app_repo-$app_slot" app "$context" "$summary" codex
  done
  # plus any FOREGROUND (non-tmux) claudes on this machine, same row format.
  _dev_fg_rows
  # Dead last, and stdio-detached inside: stdout here IS the row stream (and is an
  # ssh pipe under `t ls -r`), so the refresh must not write a byte or hold it open.
  _pr_state_flush
}

# _dev_rows_all — fan _dev_session_rows out over THIS machine + every $REMOTE_HOSTS
# entry and print each row prefixed with its host ("<host>\t<sid>\t<cwd>\t<slot>\t
# <state>\t<context>\t<summary>"; host = the REMOTE_HOSTS key, or "local"). The data
# source behind `dev ls -r`. Remote hosts are scanned IN PARALLEL over ssh
# (background jobs → temp files → `wait`) with a short ConnectTimeout + BatchMode so
# a sleeping/offline Mac is skipped fast and noted on stderr, never waited on. Each
# host's ssh exit code is stashed in a `.rc` file so we can tell "unreachable"
# (rc 255) from "reachable but the scan errored" (e.g. rc 127 → stale dotfiles), and
# warn accordingly. local-first; within a host, _dev_session_rows' own order holds.
# Rows are prefixed verbatim (no re-splitting), so empty fields can't collapse.
_dev_rows_all() {
  # no_monitor/no_notify: this fans out with `&` + `wait`; in a monitor-mode shell that
  # would print in-run job-control notices ("[2] 67794", "exit 1 …", "done"). Silences
  # them for a *direct* interactive call. local_options restores the caller's settings
  # on return — which is why it canNOT silence the "N jobs SIGHUPed" warning printed at
  # SHELL EXIT (monitor is back on by then): the `t ls -r` path spawns `zsh -lic
  # _dev_rows_all` as the top-level command, and that exit warning is killed by bin/t's
  # ZSH_PRELUDE (top-level `setopt no_monitor`, which persists to exit). The `$(…)`
  # subshell callers below already run job-control-off, so they never warn either.
  setopt local_options no_monitor no_notify
  local tmpd; tmpd=$(mktemp -d) || return 1
  _dev_session_rows "$@" > "$tmpd/.local" 2>/dev/null &
  local scan=_dev_session_rows remote_scan
  [[ ${1:-} == --details ]] && scan+=' --details'
  remote_scan="zsh -lic ${(q)scan}"
  local h
  for h in ${(k)REMOTE_HOSTS}; do
    ( ssh -o ConnectTimeout=3 -o BatchMode=yes "${REMOTE_HOSTS[$h]}" "$remote_scan" \
        > "$tmpd/$h" 2>/dev/null
      print -r -- $? > "$tmpd/$h.rc" ) &
  done
  wait

  local host file rc line
  for host in local ${(k)REMOTE_HOSTS}; do
    if [[ $host == local ]]; then
      file="$tmpd/.local"
    else
      file="$tmpd/$host"
      rc=$(< "$tmpd/$host.rc" 2>/dev/null)
      if [[ -z $rc || $rc == 255 ]]; then          # ssh-level failure = unreachable
        print -u2 -r -- "dev: $host unreachable — skipped"
        continue
      elif [[ $rc == 127 ]]; then                  # command or shell integration missing
        print -u2 -r -- "dev: $host scan failed (rc=127; install or update t's shell integration there) — skipped"
        continue
      elif [[ $rc != 0 ]]; then                    # reachable, but the scan errored
        print -u2 -r -- "dev: $host scan failed (rc=$rc) — skipped"
        continue
      fi
    fi
    [[ -s $file ]] || continue
    while IFS= read -r line; do
      printf '%s\t%s\n' "$host" "$line"
    done < "$file"
  done
  rm -rf "$tmpd"
}

# _dev_list_remote — `dev ls -r`: _dev_list across THIS machine AND every
# $REMOTE_HOSTS host. Same rendering as _dev_list (the "● attached · ✓ active
# context" header, $COLUMNS-truncated summary), but driven by _dev_rows_all instead
# of a direct tmux scan, and with a dedicated HOST column (STATUS/HOST/SESSION/
# WORKING ON). A row on THIS machine leaves HOST *blank* (so "here" reads as absence,
# not a peer host named `local`) while remote rows show their $REMOTE_HOSTS key — that
# is the local/host disambiguation, and `local` never widens the column. A trailing
# footer spells out the two verbs: `t open <repo> <slot>` (auto-attaches it in place on
# its host) and `t beam <repo> <slot> --from <host>` (pulls it down here — a move).
# Read-only itself.
_dev_list_remote() {
  local scope="$1"
  local rows; rows=$(_dev_rows_all)
  # Scope to the current repo dir (rows carry cwd in field 3; repos share the same
  # ~/code path on every machine, so a local scope filters remote rows too). Worktree
  # paths ($DEV_WORKTREE_ROOT/<basename>/...) count as the repo too (wt clause); the
  # worktree root is the same path on every host, like ~/code.
  [[ -n $scope ]] && rows=$(print -r -- "$rows" | awk -F'\t' \
    -v d="$(_dev_homerel "$scope")" -v wt="$(_dev_homerel "$DEV_WORKTREE_ROOT")/${scope:t}" \
    '{ c=$3; sub(/^\/(Users|home)\/[^\/]+\//, "", c) }
     c==d || index(c, d"/")==1 || index(c, wt"/")==1')
  if [[ -z $rows ]]; then
    echo "No dev sessions running${scope:+ in ${scope:t} (dev ls -r --all for every repo)}${scope:+,} on this machine or any reachable host."
    return 0
  fi
  local g c y b r0=
  if [[ -t 1 ]]; then g=$'\e[32m'; c=$'\e[36m'; y=$'\e[2m'; b=$'\e[1m'; r0=$'\e[0m'; fi
  # widest HOST and SESSION cells so both columns line up (headers are the floor:
  # "HOST"=4, "SESSION"=7). HOST is its own column, SESSION stays the bare slot.
  local host sid cwd slot state context summary agent host_w=4 name_w=7
  while IFS=$'\t' read -r host sid cwd slot state context summary agent; do
    # local rows render with a BLANK host cell (see below), so they never widen it.
    [[ $host != local ]] && (( ${#host} > host_w )) && host_w=${#host}
    (( ${#slot} > name_w )) && name_w=${#slot}
  done <<< "$rows"
  # prefix before WORKING ON = 2 indent + 8 STATUS + host_w + 1 gap + name_w + 1 gap
  local avail=$(( ${COLUMNS:-80} - 12 - host_w - name_w ))
  (( avail < 12 )) && avail=$(( 80 - 12 - host_w - name_w ))
  print -r -- "dev sessions   ${g}●${r0} attached · ${c}✓${r0} active context${scope:+   ${y}(repo: ${r0}${b}${c}${scope:t}${r0}${y} — --all for all)${r0}}"
  print -r -- ""
  printf '  %s%-8s%-*s %-*s %s%s\n' "$y" 'STATUS' $host_w 'HOST' $name_w 'SESSION' 'WORKING ON' "$r0"
  local amark cmark hostcell
  while IFS=$'\t' read -r host sid cwd slot state context summary agent; do
    [[ $state == attached ]] && amark="${g}●${r0}" || amark='○'
    [[ $context == active ]] && cmark="${c}✓${r0}" || cmark=' '
    (( ${#summary} > avail )) && summary="${summary[1,avail-1]}…"
    # "this machine" rows leave HOST blank so they read as local, not a peer host.
    hostcell=$host; [[ $host == local ]] && hostcell=
    printf '  %s %s     %-*s %-*s %s%s%s\n' "$amark" "$cmark" $host_w "$hostcell" $name_w "$slot" "$y" "$summary" "$r0"
  done <<< "$rows"
  # reattach legend: `t open` auto-attaches a slot on its host; `t beam … --from <host>`
  # pulls it here (a move). A foreground (:fg) row is bound to its terminal, so it is
  # reattached on its host via `t on <host> t open <id>` (shown only when a :fg row is
  # present — slot is field 4 of the prefixed rows).
  local foot="attach (auto-finds its host): t open <repo> <slot> · pull here: t beam <repo> <slot> --from <host>"
  print -r -- "$rows" | awk -F'\t' '$4 ~ /:/{f=1} END{exit !f}' \
    && foot+=" · foreground: t open <session> · kill: t kill -r <session>"
  print -r -- ""
  print -r -- "  ${y}${foot}${r0}"
}

# _dev_kill_one <session> <force> — kill a single dev tmux session. When it holds
# a live Claude (active context) and <force> is empty, confirm first: killing only
# SIGHUPs Claude and the transcript is appended live (so the conversation stays
# resumable via tpop/dev), but we still guard against a typo dropping a live turn.
# The confirm is gated on ACTIVE CONTEXT, not merely a live process: a claude
# parked on its startup splash (_dev_session_at_welcome — what `dev ls` reports as
# "idle — no conversation") has no conversation to interrupt, so kill it without
# the prompt. Mirrors _dev_list's two-signal distinction; without it `dev kill`
# prompted "Claude is live there" for the very sessions ls calls idle.
_dev_kill_one() {
  local session="$1" force="$2" agent
  agent=$(_dev_agent_of_session "$session")
  if [[ -z "$force" ]] && _dev_session_has_claude "$session" \
       && ! _dev_agent_at_welcome "$agent" "$session"; then
    read -q "REPLY?Kill $session? $agent is live there (context interrupted). [y/N] " \
      || { print; echo "Skipped $session."; return 1; }
    print
  fi
  # NOT `local path`: in zsh `path` is the array tied to $PATH, and a plain local
  # keeps the tie but starts EMPTY — so every command below (the kill itself) was
  # "command not found". Pinned by test_zshrc_never_declares_a_tied_special_….
  local wt; wt=$(tmux display-message -p -t "=$session:" '#{session_path}' 2>/dev/null)
  local _kerr
  if _kerr=$(tmux kill-session -t "=$session:" 2>&1); then echo "Killed $session"
  else echo "t kill: tmux kill-session $session failed${_kerr:+: $_kerr}" >&2; return 1; fi
  # A slot's dev server is detached from its tmux session and would outlive it,
  # serving the old code on the slot's port. Only a per-session worktree is swept
  # this way — a shared tree's processes belong to everyone.
  [[ -n $DEV_WORKTREE_ROOT && -n $wt && $wt == $DEV_WORKTREE_ROOT/* ]] \
    && _dev_stop_rooted "$wt"
  return 0
}

# _dev_tmux_session_of_pid <pid> — print the tmux session <pid> runs inside (walk its
# ancestry until an ancestor is some pane's pid), else return 1. Lets _dev_kill_fg tell a
# claude living in a NON-dev tmux session (kill the whole session) from a true no-tmux
# foreground claude (SIGTERM the process). The map is
# built over ALL sessions, not just dev-*, precisely because these targets are non-dev.
_dev_tmux_session_of_pid() {
  local pid="$1" line up
  [[ -n $pid ]] || return 1
  local -A pane2sess
  for line in ${(f)"$(tmux list-panes -a -F '#{pane_pid} #{session_name}' 2>/dev/null)"}; do
    pane2sess[${line%% *}]=${line#* }
  done
  up=$pid
  while [[ -n $up && $up != 1 ]]; do
    [[ -n ${pane2sess[$up]} ]] && { print -r -- ${pane2sess[$up]}; return 0; }
    up=$(ps -o ppid= -p $up 2>/dev/null | tr -d ' ')
  done
  return 1
}

# _dev_fg_pids — one row per live agent process that is NOT the agent of a
# dev-<repo>-<slot> pane: "<pid>\t<sid>\t<cwd>\t<label>\t<agent>". The pid-keyed view of
# the `<repo>:<id>` / `<repo>:fg` rows `t ls` shows — true foreground agents AND agents
# living in a NON-dev tmux session of their own. Labels are built exactly
# as _dev_fg_rows builds them (registry sid → `<repo>:<short sid>`, else `<repo>:fg`) so
# the same handles address the same rows; the agent THIS shell runs under is skipped —
# never kill or attach the session you are typing in. _dev_fg_rows keeps its own copy of
# this walk on purpose: it renders transcript titles, reads the ps/pane SNAPSHOT and
# prunes the registry, and its rows deliberately carry no pid — which is the one field
# the kill/attach verbs need (both a tmux session and a signal are pid-resolved).
_dev_fg_pids() {
  local reg="${XDG_CACHE_HOME:-$HOME/.cache}/claude-sessions"
  # agent pids already owned by a dev-* slot are slots, not fg rows
  local -A inslot; local s p
  for s in ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^dev-')"}; do
    p=$(_dev_session_claude_pid "$s") && [[ -n $p ]] && inslot[$p]=1
  done
  local me up=$$
  while [[ -n $up && $up != 1 ]]; do
    _dev_agent_is_proc "$(ps -o comm= -p $up 2>/dev/null)" && { me=$up; break; }
    up=$(ps -o ppid= -p $up 2>/dev/null | tr -d ' ')
  done
  local pid cwd repo sid label agent
  for pid in ${(f)"$(ps -Axo pid,comm 2>/dev/null | awk '$0 ~ /\.app\/Contents\// {next} {n=$2; sub(/.*\//,"",n)} n=="claude"||n=="codex"||n~/^codex-(aarch64|x86_64)-/{print $1}')"}; do
    [[ -n ${inslot[$pid]} || $pid == $me ]] && continue
    _dev_agent_nested "$pid" && continue          # part of another agent's session
    _dev_agent_is_service "$pid" && continue      # never a foreground kill target
    sid= cwd=
    [[ -r $reg/$pid ]] && IFS=$'\t' read -r sid cwd < "$reg/$pid"
    [[ -n $cwd ]] || cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null)   # Linux: no lsof needed
    [[ -n $cwd ]] || cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    [[ -n $cwd ]] || continue
    repo=$(_dev_repo_of_dir "$cwd" 2>/dev/null); repo=${repo%%$'\t'*}   # worktree-aware
    [[ -n $repo ]] || repo=${cwd:t}                                     # else the basename
    agent=$(_dev_agent_of_comm "$(ps -o comm= -p $pid 2>/dev/null)"); [[ -n $agent ]] || agent=claude
    [[ -n $sid ]] || sid=-
    _dev_fg_label "$repo" "$sid" "$pid"; label=$REPLY
    printf '%s\t%s\t%s\t%s\t%s\n' "$pid" "$sid" "$cwd" "$label" "$agent"
  done
  return 0
}

# _dev_fg_match <handle> [repo_key_ok] — the _dev_fg_pids rows <handle> addresses; ONE
# home for the three rules the fg verbs share (they were open-coded in _dev_kill_fg and
# again as awk in _dev_remote_fg_kill): the displayed label exactly (`repo:id`/`repo:fg`),
# the label's repo part, or a bare short session-id prefix (never for a DEV_REPOS key — a
# key means dev slots). The repo-part rule is suppressed for a DEV_REPOS key unless
# <repo_key_ok>: `t kill dotfiles` must list dev slots rather than kill a same-repo fg
# row, while `t open dotfiles fg` asks for exactly that repo's fg rows. Returns 1
# (printing nothing) when nothing matches — including an empty <handle>, since every
# caller addresses one row set and the bare verbs have their own paths.
_dev_fg_match() {
  local handle="$1" repo_ok="$2"
  [[ -n $handle ]] || return 1
  local idpart="${handle##*:}" out
  out=$(_dev_fg_pids | awk -F'\t' -v h="$handle" -v idp="$idpart" \
          -v isrepo="${DEV_REPOS[$handle]:+1}" -v ro="${repo_ok:+1}" \
          "$_DEV_FG_MATCH_AWK"' fgm($4, $2) { print }')
  [[ -n $out ]] || return 1
  print -r -- "$out"
}

# _dev_kill_fg <handle> [force] — kill FOREGROUND / non-dev-slot claude sessions: the
# `<repo>:<id>` / `<repo>:fg` rows `t ls` shows via _dev_fg_rows (live claudes not owned
# by a dev-<repo>-<slot> pane — true foreground claudes AND claudes in a non-dev tmux
# session of its own). _dev_kill only tears down dev slots, so this is the companion
# path for those rows — the gap that made `t kill dotfiles-pr47` report
# "no session". Rows + handle matching come from _dev_fg_match (with the repo-part rule
# suppressed for a DEV_REPOS key, so `t kill dotfiles` still lists dev slots). Kill
# mechanics via _dev_tmux_session_of_pid: a claude inside a tmux session → kill that
# session; a no-tmux claude → SIGTERM it (clean exit, transcript stays resumable — the
# same signal tpush sends). Confirms once per target while a conversation is live unless
# <force>. Returns 0 if it killed something, 1 if a row matched but was not killed
# (confirm declined / kill failed), 2 if nothing matched — so _dev_kill only falls through
# to its "no session" path on a true miss (2), not after a deliberate skip.
_dev_kill_fg() {
  local handle="$1" force="$2"
  [[ -n $handle ]] || return 1
  setopt local_options null_glob
  local rows; rows=$(_dev_fg_match "$handle") || return 2
  local line pid sid cwd label agent context title tsess killed= matched=
  local -a tx
  for line in ${(f)rows}; do
    IFS=$'\t' read -r pid sid cwd label agent <<< "$line"
    matched=1
    context=idle
    if [[ -n $sid && $sid != - ]]; then
      tx=( "$HOME/.claude/projects"/*/"$sid".jsonl(N) )
      [[ -n ${tx[1]} ]] && title=$(_transcript_title "${tx[1]}") && [[ -n $title ]] && context=active
    fi
    if [[ -z $force && $context == active ]]; then
      read -q "REPLY?Kill $label? Claude is live there (context interrupted). [y/N] " \
        || { print; echo "Skipped $label."; continue; }
      print
    fi
    tsess=$(_dev_tmux_session_of_pid "$pid")
    if [[ -n $tsess ]]; then
      tmux kill-session -t "=$tsess:" 2>/dev/null && { echo "Killed $label (tmux session $tsess)"; killed=1; }
    else
      kill -TERM "$pid" 2>/dev/null && { echo "Killed $label (foreground pid $pid)"; killed=1; }
    fi
  done
  [[ -n $killed ]] && return 0
  [[ -n $matched ]] && return 1
  return 2
}

# _dev_kill <repo> <slot|all> [force] — tear down dev-<repo>-<slot> sessions.
# Keyed on session NAMES, not DEV_REPOS, so it also reaches orphaned sessions
# whose repo alias is gone. A slot (or `all`) is REQUIRED — with no slot we list
# the repo's sessions and bail rather than guess which to kill.
_dev_kill() {
  local repo="$1" slot="$2" force="$3"

  # Repo-aware: `t kill 4` / `t kill all` mean the repo $PWD is in (live-session
  # name wins — see _t_infer_repo); bare `t kill` infers the repo and then lists
  # its slots below (a slot is still required — no guessing on a kill).
  if [[ -z ${DEV_REPOS[$repo]:-} && -z $slot && ( $repo == <-> || $repo == all ) ]]; then
    slot=$repo; repo=$(_t_infer_repo "$slot")
  elif [[ -z $repo ]]; then
    repo=$(_t_infer_repo)
  fi

  # A DEV_REPOS dir can carry several aliases (`dot` and `dotfiles` both key
  # ~/code/dotfiles); a live local session is named after whichever alias started
  # it. `t kill dotfiles 2` must therefore also reach `dev-dot-2` in the same
  # tree — otherwise the name-prefix scan below finds nothing, the local live
  # check in _dev_remote_delegate/_dev_session_remote_fallback (which both key
  # off `dev-${repo}-${slot}`) also misses it, and kill delegates remotely while
  # the local same-numbered session keeps running (violating one-live-owner).
  # For a specific slot, canonicalize $repo to the sibling alias running it.
  # For `all`/no-slot, union every sibling alias keyed to this dir so a mass
  # kill reaches `dev-dot-1` AND `dev-dotfiles-2` in one tree (picking a single
  # alias would silently leave sessions running under the others).
  local -a _repos=( "$repo" )
  if [[ -n ${DEV_REPOS[$repo]:-} ]]; then
    local _kdir=${DEV_REPOS[$repo]} _kk
    if [[ -n $slot && $slot != all ]]; then
      for _kk in ${(k)DEV_REPOS}; do
        [[ $_kk == $repo || ${DEV_REPOS[$_kk]} != $_kdir ]] && continue
        tmux has-session -t "=dev-${_kk}-${slot}:" 2>/dev/null && { repo=$_kk; _repos=( $_kk ); break; }
      done
    else
      for _kk in ${(k)DEV_REPOS}; do
        [[ $_kk == $repo || ${DEV_REPOS[$_kk]} != $_kdir ]] && continue
        _repos+=( $_kk )
      done
    fi
  fi

  if [[ -z "$repo" ]]; then
    command t kill -h
    return 1
  fi

  # collect this repo's live sessions by name (dev-<repo>-<N>), numerically sorted.
  # For `all`/no-slot, $_repos is the union of sibling aliases sharing this dir.
  local -a sessions
  sessions=( ${(f)"$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
    | grep -E "^dev-(${(j:|:)_repos})-[0-9]+\$" | sort -t- -k3 -n)"} )

  if (( ! ${#sessions} )); then
    # None live HERE. If a specific slot was named and it is live on another host, tear
    # it down there over ssh -t (the remote `t kill` still prompts unless -y); -r forces
    # this explicitly. With no slot, never auto-pick a kill target — just point at where
    # to look.
    if [[ -n $slot && $slot != all ]]; then
      _dev_remote_delegate "$repo" "$slot" kill ${force:+-y} && return
    fi
    # No dev slot here. The target may be a FOREGROUND / non-dev-slot claude — the
    # `<repo>:<id>` / `:fg` rows `t ls` shows (a true foreground claude, or one in a
    # `pr-*` session). Those never match the dev-slot grep above, so try that path
    # before giving up (only for a slot-less handle — fg rows carry no slot number).
    # rc 2 = no fg row matched → fall through; 0/1 = matched (killed / skipped) → done.
    if [[ -z $slot ]]; then
      _dev_kill_fg "$repo" "$force"; local _frc=$?
      (( _frc != 2 )) && return $_frc
    fi
    echo "No sessions for '$repo' here."
    [[ -z $slot || $slot == all ]] && (( ${#REMOTE_HOSTS} )) && \
      echo "(check other hosts: \`t session list -r\`; kill there with \`t session close -r $repo${slot:+ $slot}\`)"
    return 1
  fi

  # `all` — kill every slot for the repo (explicit opt-in to a mass kill).
  if [[ "$slot" == all ]]; then
    local s
    for s in $sessions; do _dev_kill_one "$s" "$force"; done
    return
  fi

  # no slot — refuse to guess; show what's there so the user can pick one.
  if [[ -z "$slot" ]]; then
    echo "Specify a slot to kill (or 'all'). Sessions for '$repo':"
    local s
    for s in $sessions; do
      if _dev_session_has_claude "$s"; then echo "  $s  ✓ ($(_dev_agent_of_session "$s") live)"; else echo "  $s"; fi
    done
    return 1
  fi

  local session="dev-${repo}-${slot}"
  if ! tmux has-session -t "=$session:" 2>/dev/null; then
    # Live on another host? Tear it down there (shared fallback; remote `t kill` still
    # confirms unless -y). Same remote detection pop/plan get; -r forces it explicitly.
    _dev_session_remote_fallback "$session" kill ${force:+-y} && return
    echo "No session: $session"
    return 1
  fi
  _dev_kill_one "$session" "$force"
}

# The supervisor is independent of the agent, so a disconnected client cannot
# disable its recovery menu. It exits when no managed sessions remain.
_dev_recovery_watch() {
  [[ -z ${T_RECOVERY_DISABLE:-} ]] || return 0
  command t __recovery start "$1" >/dev/null 2>&1 || true
}

# _dev_new_session <session> <dir> [branch] [skip_prepare] — create a detached tmux
# session in <dir>, start logging, and launch Claude on <branch> (default $DEV_BRANCH).
# Callers pass the repo's resolved branch (see _dev_branch_for) since the repo
# alias isn't recoverable from <session> reliably. Shared by `dev` and `tpaste`
# so the bootstrap (branch dance, geometry, logging) lives in one place; callers
# attach (or not) and deliver input themselves afterwards.
# skip_prepare (non-empty) suppresses the in-pane _dev_repo_prepare branch dance —
# set it in worktree mode, where <dir> is the slot's own worktree already on its
# branch off main, so there is no shared tree and no sibling to trample.
#
# We *pre-assign* Claude's session id (a lowercased uuidgen) and pass it as
# `claude --session-id`, then stash it on the tmux session as CLAUDE_RESUME_ID —
# the same precise signal _dev_resume_session records. Without it, every slot in
# a repo shares one fallback (the dir's newest transcript), so `dev ls` showed
# identical summaries for sibling slots and `tpop` couldn't target a specific
# one. uuidgen is uppercase but Claude stores ids lowercase, so we lowercase to
# keep the transcript filename glob (`<sid>.jsonl`) matching.
_dev_new_session() {
  local session="$1" dir="$2" branch="${3:-$DEV_BRANCH}" skip_prepare="${4:-}" agent="${5:-claude}"
  _dev_auto_trust "$dir"
  local logfile="$HOME/.tmux-logs/${session}.log"
  local sid; sid="$(uuidgen | tr 'A-Z' 'a-z')"
  mkdir -p "$HOME/.tmux-logs"
  # No fixed -x/-y geometry: a session seeded oversized (was 220x50) stays bigger
  # than a narrow client until it resizes, so attaching from a phone (Termius)
  # showed tmux's status-right pan indicator ([x,y], reads like "20") and the UI
  # overflowed the screen. window-size latest makes the window track whichever
  # client is active, so it fits the phone on attach. (latest is tmux's default,
  # but we set it explicitly so it holds on machines with a different default.)
  # The trailing colon keeps dots in repo names out of tmux window/pane parsing.
  tmux new-session -d -s "$session" -c "$dir"
  tmux set-option -t "=$session:" window-size latest 2>/dev/null
  tmux pipe-pane -t "=$session:" -o "cat >> $logfile"
  # Which agent occupies the slot, for every later reader (_dev_agent_of_session). Only
  # claude takes a pre-assigned id; a codex slot is stamped by its SessionStart hook once
  # codex has minted one (there is no `codex --session-id`).
  tmux set-environment -t "=$session" DEV_AGENT "$agent"
  [[ $agent == claude ]] && tmux set-environment -t "=$session" CLAUDE_RESUME_ID "$sid"
  # `; exit` so quitting Claude closes the pane's shell and tears down the
  # (single-window) tmux session instead of leaving an idle prompt behind. Fires
  # on any exit (clean or crash); crash output survives in the pipe-pane logfile
  # (`t read`). `t pop` kill-sessions the slot itself, so the exit is moot there.
  local prep="_dev_repo_prepare ${(q)branch}; "
  [[ -n $skip_prepare ]] && prep=    # worktree mode: <dir> is already on its branch
  tmux send-keys -t "=$session:" "${prep}$(_dev_agent_new_cmd "$agent" "$sid"); exit" Enter
  _dev_recovery_watch "$session"
}

# _t_dev — the engine behind `t open`: open/reattach a Claude Code tmux session, local or on
# another host. Resolves the repo from $PWD when called bare, and a slot that is live only on
# another $REMOTE_HOSTS host is found and attached IN PLACE there (host inferred) — a live LOCAL
# slot always wins, and open never MOVES a session between machines (that is `t beam`). Handles
# --new (fresh), --fg (no tmux: foreground-resume the slot if live, else a fresh inline claude),
# -r/--host (remote), and -l/--local (force this machine). repo is a DEV_REPOS key; the branch
# is per-repo (DEV_BRANCHES[repo], else $DEV_BRANCH). User-facing help lives in bin/t
# (`t open -h`); the t() shim routes -h there, so this helper takes none of its own.
_t_dev() {
  local no_tmux= force= remote= all= local_only= agent_over=
  local -a pos
  local arg
  # -f/--fg = foreground/no-tmux EVERYWHERE (matches tbeam -f). The kill-confirm
  # skip moved off -f onto -y/--yes (--force kept as a long alias) so -f never
  # means two things. `dev kill` returns before the no_tmux check below, so a
  # stray -f on a kill is just an inert no-op rather than a silent force.
  for arg in "$@"; do
    case "$arg" in
      -f|--fg|--no-tmux) no_tmux=1 ;;
      -y|--yes|--force)  force=1 ;;
      -r|--remote)       remote=1 ;;
      -l|--local|--here) local_only=1 ;;
      -a|--all)          all=1 ;;
      --codex)           agent_over=codex ;;
      --claude)          agent_over=claude ;;
      # An unknown flag is an error, never a positional: `t open dot --news` (a typo of
      # --new) once became slot "--news" — a real session dev-dot---news with its own
      # worktree .../dotfiles/--news and branch dev/dotfiles---news.
      -*)
        echo "t open: unknown flag '$arg' (flags: --new --fg --here -r --host <h> --codex --claude)" >&2
        return 2 ;;
      *)                 pos+=("$arg") ;;
    esac
  done
  local repo="${pos[1]}"
  local slot="${pos[2]}"

  # `dev list` (or `ls`) — show sessions + state, then stop. `-r` spans every
  # $REMOTE_HOSTS host too (a cross-host view), else just this machine. By default
  # it's SCOPED to the repo you're in (path-based, so dot-*/dotfiles-* both show in
  # the dotfiles dir); `-a`/`--all` widens to every repo, as does standing outside
  # any DEV_REPOS dir (nothing to scope to).
  if [[ "$repo" == list || "$repo" == ls ]]; then
    local scope=; [[ -z $all ]] && scope=$(_dev_cwd_repo_dir)
    if [[ -n $remote ]]; then _dev_list_remote "$scope"; else _dev_list "$scope"; fi
    return
  fi

  # `dev kill <repo> <slot|all>` — tear down a session (or all of a repo's).
  # Operates on session NAMES, not DEV_REPOS keys, so it can also clean up
  # orphaned sessions whose repo alias no longer exists (e.g. dev-dotfiles-*).
  # With -r, kill it on its $REMOTE_HOSTS host instead (host auto-inferred).
  if [[ "$repo" == kill ]]; then
    if [[ -n $remote ]]; then
      _dev_remote_kill "${pos[2]}" "${pos[3]}" "$force"
    else
      _dev_kill "${pos[2]}" "${pos[3]}" "$force"
    fi
    return
  fi

  # Remote-aware open (explicit half): a slot can live on THIS machine or another.
  # -r/--remote FORCES the remote branch — attach in place on its host (the session
  # stays put; MOVING it between machines is `t beam`'s job), and a bare `t open -r` is
  # the all-remote picker. The AUTO half runs further down, once <repo> is resolved from
  # the cwd, so even a bare `t open` in a repo dir can find a slot that is live only on
  # another host. To pull a remote session HERE (a move), use `t beam … --from <host>`.
  # -l/--local/--here and -r/--remote are opposite intents (force local vs. force
  # remote), so reject the combination rather than silently letting -r win.
  if [[ -n $remote && -n $local_only ]]; then
    echo "t open: --local/--here and -r/--remote are mutually exclusive" >&2
    return 2
  fi
  if [[ -n $remote ]]; then
    _dev_remote "$repo" "$slot" "$no_tmux"     # explicit -r: force attach in place on its host
    return
  fi

  # An id-shaped lone arg (`t open f0f1bbef`, or the displayed `dot:f0f1bbef` label)
  # is a foreground-session handle, not a repo — adopt that session here. The id
  # alone identifies it, so no repo is needed; a real DEV_REPOS key always wins
  # (checked first), and _dev_fg_handle never matches slot numbers or `new`.
  if [[ -n "$repo" && -z "$slot" && -z "${DEV_REPOS[$repo]}" ]] && _dev_fg_handle "$repo"; then
    _dev_open_fg "$repo"
    return
  fi

  # Repo-aware defaults: bare `t open` targets the repo $PWD is in, and a lone
  # numeric/slot-keyword first arg is a SLOT of it (`t open 4` ≡ `t open <cwd-repo> 4`,
  # `t open --new` → a fresh slot here, `t open fg` → adopt this repo's :fg session)
  # — see _t_infer_repo. Deliberately AFTER the -r branch (so the documented bare
  # `t open -r` every-slot picker survives) and the id-shaped fg adoption above;
  # the remote auto-probe runs further down, once <repo> is resolved, so even a
  # bare `t open` can find a slot that is live only on another host. Outside every
  # DEV_REPOS dir this leaves repo empty and the usage below explains.
  # `t open 4 --new` lands here as `repo=4 slot=new` (--new became a positional in
  # _t_open) — the explicit numeric slot still wins, the redundant keyword drops.
  local _bare_repo=
  [[ -z $repo ]] && _bare_repo=1
  if [[ -z ${DEV_REPOS[$repo]:-} && ( $repo == <-> || $repo == new || $repo == fg ) \
        && ( -z $slot || $slot == new || $slot == fg ) ]]; then
    slot=$repo; repo=; _bare_repo=1
  fi
  [[ -z $repo ]] && repo=$(_t_infer_repo "$slot")

  # Worktree-aware bare open: standing inside a per-session worktree
  # ($DEV_WORKTREE_ROOT/<basename>/<slot>) defaults the slot to ITS slot, so
  # `t open` from that dir targets the matching session instead of the lowest
  # free gap. Only when the repo was inferred from $PWD (no explicit repo arg) —
  # an explicit `t open <other-repo>` from inside an unrelated repo's worktree
  # must not adopt that worktree's slot. _dev_repo_of_dir prints "<alias>\t<slot>"
  # (slot empty for a canonical repo dir or subdir, populated for a worktree
  # path) — same source `t read` already uses for cwd→slot inference in bin/t.
  if [[ -z $slot && -n $repo && -n $_bare_repo ]]; then
    local _wsr _wss
    _wsr=$(_dev_repo_of_dir "$PWD" 2>/dev/null) && {
      _wss=${_wsr#*$'\t'}
      [[ -n $_wss ]] && slot=$_wss
    }
  fi

  # repo→path map: see the global DEV_REPOS (defined near the cd shortcuts)

  if [[ -z "$repo" || -z "${DEV_REPOS[$repo]}" ]]; then
    # Styled gh-style (piped through _help_style), but the Repos: section is built
    # from the ACTUAL ${(k)DEV_REPOS} — the dynamic bit static help can't show.
    command t open -h
    return 1
  fi

  local dir="${DEV_REPOS[$repo]}"
  local branch="$(_dev_branch_for "$repo")"

  if [[ ! -d "$dir" ]]; then
    echo "Repo dir not found: $dir"
    return 1
  fi

  # `t open <repo> fg|<id>` — reattach a FOREGROUND (:fg) session listed by `t ls`.
  # _dev_open_fg picks how: a row living in a non-dev tmux session of its own, here or
  # on another host, is ATTACHED in place; a true foreground
  # agent is bound to its own terminal, so getting into it means MOVING it — stop that
  # owner and `claude -r` in THIS terminal. The tmux-slot analog is `t open <repo> <slot>`. (`fg` as the slot keyword mirrors the
  # `:fg` label and scopes to the repo; an id-shaped slot — see _dev_fg_handle —
  # names the exact session. -f is irrelevant here: fg adoption is inherently a
  # foreground resume.)
  if [[ "$slot" == fg ]]; then
    _dev_open_fg "$repo"
    return
  elif _dev_fg_handle "$slot"; then
    _dev_open_fg "$slot"
    return
  fi

  # Same-dir sibling aliases (see _dev_kill / _t_pop): a `dev-dot-2` answers
  # `t open dotfiles 2` when `dot` and `dotfiles` both key ~/code/dotfiles.
  # Without this canonicalization the local live check below misses it and we
  # either ssh to a remote slot of the same number or mint a duplicate local
  # `dev-dotfiles-2`, violating the one-live-owner invariant for the slot.
  if [[ -n $slot && $slot != new && $slot != fg && -n ${DEV_REPOS[$repo]:-} ]] \
     && ! tmux has-session -t "=dev-${repo}-${slot}:" 2>/dev/null; then
    local _odir=${DEV_REPOS[$repo]} _ok
    for _ok in ${(k)DEV_REPOS}; do
      [[ $_ok == $repo || ${DEV_REPOS[$_ok]} != $_odir ]] && continue
      tmux has-session -t "=dev-${_ok}-${slot}:" 2>/dev/null && { repo=$_ok; break; }
    done
  fi

  # Remote-aware open (auto half): <repo> is a valid key now (cwd-defaulted if bare) and
  # fg adoption already returned, so a slot that is NOT live HERE but IS live on a
  # $REMOTE_HOSTS host gets attached IN PLACE there (host inferred) — a live LOCAL slot
  # always wins (cheap tmux check, no ssh). Skipped with no hosts, for -f/`new` (a
  # foreground/fresh request stays local), and when a local slot is already live; if
  # nothing is live remotely either, fall through to the local fresh-start path.
  # -l/--local/--here forces this machine: skip the probe entirely so a slot that
  # is live only on another host is NOT attached — a fresh local slot is opened
  # instead (combine with --fg for a local foreground resume / inline claude).
  if (( ${#REMOTE_HOSTS} )) && [[ -z $no_tmux && -z $local_only && $slot != new ]] && ! _dev_local_slot_live "$repo" "$slot"; then
    local res; res=$(_dev_remote_resolve "$repo" "$slot" 2>/dev/null)   # quiet probe
    if [[ -n $res ]]; then
      echo "(not live here — attaching on ${res%%$'\t'*}; Ctrl-b d to detach)"
      _dev_remote_attach "$res" "$no_tmux"
      return
    fi
    # nothing live remotely either → fall through to the local path (fresh start)
  fi

  # Which agent a FRESH slot gets: the --codex/--claude flag > DEV_AGENT[repo] >
  # DEV_AGENT_DEFAULT (the seam above). Resolved here, after the repo is known and
  # before any tmux work, so a missing binary fails with the `t install` pointer
  # instead of a pane that dies on "command not found". A reattach ignores it.
  local agent
  agent=$(_dev_agent_for "$repo" "$agent_over") || return 1

  # An explicit foreground slot must respect desktop ownership before the
  # no-tmux branch, which otherwise bypasses the ordinary slot preflight.
  if [[ -n $no_tmux && $slot == <-> ]] && (( ${#REMOTE_HOSTS} )) \
     && ! tmux has-session -t "=dev-${repo}-${slot}:" 2>/dev/null; then
    local app_owner; app_owner=$(_dev_remote_app_owner "$repo" "$slot")
    if [[ -n $app_owner ]]; then
      print -u2 -- "t open: $repo $slot is reserved by the Codex desktop app on $app_owner; close it there and release the reservation before opening here"
      return 1
    fi
  fi

  # -f/--fg (a.k.a. --no-tmux): run claude inline, no tmux. If a SPECIFIC slot is
  # named and it's live, foreground-RESUME that conversation (`claude -r`) — matching
  # what -f means in tbeam / `dev -r`; that's exactly `tpop`, so delegate to it (it
  # kills the slot first, honoring the one-live-owner invariant). Otherwise (no slot,
  # `new`, or that slot doesn't exist yet) start a FRESH claude inline after the
  # branch dance — slot is a tmux concept, so the fresh path has none.
  if [[ -n "$no_tmux" ]]; then
    if [[ -n "$slot" && "$slot" != new ]] && tmux has-session -t "=dev-${repo}-${slot}:" 2>/dev/null; then
      _t_pop "$repo" "$slot"
      return
    fi
    # Fresh inline claude. In worktree mode give it an isolated worktree too, keyed
    # to a slot number (the named one, else the next FRESH one — _dev_slot_fresh) so it
    # can't collide with a tmux slot's worktree or land on parked work; on failure/opt-out
    # fall back to the shared tree + prepare.
    local skip_prepare=
    if _dev_worktree_enabled "$repo"; then
      local _wslot="$slot"
      if [[ -z $_wslot || $_wslot == new ]]; then
        _wslot=1
        while ! _dev_slot_fresh "$repo" "$_wslot"; do (( _wslot++ )); done
      fi
      if [[ $slot != <-> ]] && (( ${#REMOTE_HOSTS} )); then
        local app_owner; app_owner=$(_dev_remote_app_owner "$repo" "$_wslot")
        if [[ -n $app_owner ]]; then
          print -u2 -- "t open: $repo $_wslot is reserved by the Codex desktop app on $app_owner; close it there and release the reservation before opening here"
          return 1
        fi
      fi
      local _wt; _wt="$(_dev_worktree_create "$repo" "$_wslot")"
      if [[ -n $_wt ]]; then dir="$_wt"; skip_prepare=1
      else _dev_worktree_refuse "$repo" "$_wslot"; return 1; fi
    fi
    _dev_agent_check "$agent" || return 1
    _dev_auto_trust "$dir"
    echo "Starting $agent in $dir (no tmux)"
    cd "$dir" || return 1
    [[ -n $skip_prepare ]] || _dev_repo_prepare "$branch"
    local -a model_args=()
    [[ -n ${DEV_MODEL[$agent]:-} ]] && model_args=(--model "${DEV_MODEL[$agent]}")
    if [[ -n ${DEV_EFFORT[$agent]:-} ]]; then
      case "$agent" in
        codex) model_args+=(-c "model_reasoning_effort=${DEV_EFFORT[$agent]}") ;;
        claude) model_args+=(--effort "${DEV_EFFORT[$agent]}") ;;
      esac
    fi
    case "$agent:${DEV_FAST[$agent]:-}" in
      codex:1) model_args+=(-c service_tier=fast --enable fast_mode) ;;
      codex:0) model_args+=(-c service_tier=default) ;;
      claude:1) model_args+=(--settings '{"fastMode":true}') ;;
      claude:0) model_args+=(--settings '{"fastMode":false}') ;;
    esac
    "$agent" "${model_args[@]}"   # retain the claude()/codex() wrappers (tpush sentinel)
    return
  fi

  # `dev <repo> new` — force the next never-used slot (skip reattaching to an
  # existing unattached session); always spins up a fresh Claude. "Never-used" is
  # _dev_slot_fresh's rule — no session, no worktree on disk, no lingering unmerged
  # branch — the same one the auto-pick and --fg use.
  if [[ "$slot" == new ]]; then
    local n=1
    while ! _dev_slot_fresh "$repo" "$n"; do (( n++ )); done
    slot=$n
  fi

  # auto-pick slot: REATTACH to the lowest-numbered existing-but-unattached
  # session before ever spawning a fresh one — so `t open <repo>` lands on the
  # session that's actually there (a detached slot 2) instead of minting slot 1.
  # Nothing to reattach → the lowest FRESH slot (_dev_slot_fresh): a gap with no
  # session is not enough — a dead slot's worktree or lingering unmerged branch is
  # parked work, and a new task must never start on it (it used to: a bare
  # `t open hive` reused slot 4, whose merged branch was then resurrected 42
  # commits behind main). Bounded scan (fresh is a non-breaking fallback, so
  # `while true` would spin past the highest slot forever); 20 matches the cap
  # `t kill` uses.
  if [[ -z "$slot" ]]; then
    local n=1 free=
    while (( n <= 20 )); do
      local sname="dev-${repo}-${n}"
      if ! tmux has-session -t "=$sname:" 2>/dev/null; then
        [[ -z $free ]] && _dev_slot_fresh "$repo" "$n" && free=$n  # lowest fresh slot → fallback
      elif ! tmux list-clients -t "=$sname:" 2>/dev/null | grep -q .; then
        slot=$n; break                                            # existing + unattached → reattach (wins)
      fi
      (( n++ ))
    done
    if [[ -z $slot ]]; then
      if [[ -n $free ]]; then
        slot=$free                                                 # nothing to reattach → lowest fresh slot
      else
        # nothing fresh in 1..20 → keep scanning unbounded, still preferring an
        # existing-but-unattached session above 20 over a fresh slot.
        while ! _dev_slot_fresh "$repo" "$n"; do
          if tmux has-session -t "=dev-${repo}-${n}:" 2>/dev/null \
             && ! tmux list-clients -t "=dev-${repo}-${n}:" 2>/dev/null | grep -q .; then
            slot=$n; break
          fi
          (( n++ ))
        done
        [[ -z $slot ]] && slot=$n                                  # nothing to reattach → next fresh slot
      fi
    fi
  fi

  local session="dev-${repo}-${slot}"

  # Remote Codex desktop workspaces own their numbered slot without tmux. The
  # attach resolver deliberately excludes them, so check their reservation before
  # creating a local CLI on the same worktree/branch.
  if ! tmux has-session -t "=$session:" 2>/dev/null && (( ${#REMOTE_HOSTS} )); then
    local app_owner; app_owner=$(_dev_remote_app_owner "$repo" "$slot")
    if [[ -n $app_owner ]]; then
      print -u2 -- "t open: $repo $slot is reserved by the Codex desktop app on $app_owner; close it there and release the reservation before opening here"
      return 1
    fi
  fi

  local logdir="$HOME/.tmux-logs"
  local logfile="$logdir/${session}.log"
  mkdir -p "$logdir"

  if tmux has-session -t "=$session:" 2>/dev/null; then
    local live_agent; live_agent=$(_dev_agent_of_session "$session")
    if [[ -n $agent_over && $agent_over != $live_agent ]]; then
      echo "Reattaching $session (it is live as $live_agent — --$agent_over applies to a fresh slot only)"
    else
      echo "Reattaching $session"
    fi
    # resume logging if it stopped (e.g. after server restart)
    tmux pipe-pane -t "=$session:" -o "cat >> $logfile"
    _dev_recovery_watch "$session"
    tmux attach-session -t "=$session:"
  else
    # Worktree mode: this slot gets its OWN worktree on dev/<basename>-<slot> off
    # main, so it shares no tree with siblings. _dev_worktree_create is idempotent —
    # a slot whose tmux died but whose worktree lingers is reused in place (work
    # preserved). On failure (or a per-repo opt-out) fall back to the shared tree +
    # the in-pane branch dance.
    local skip_prepare=
    if _dev_worktree_enabled "$repo"; then
      local _wt; _wt="$(_dev_worktree_create "$repo" "$slot")"
      if [[ -n $_wt ]]; then dir="$_wt"; skip_prepare=1
      else _dev_worktree_refuse "$repo" "$slot"; return 1; fi
    fi
    _dev_agent_check "$agent" || return 1
    local agent_note=; [[ $agent != claude ]] && agent_note=" · $agent"
    echo "Starting $session in $dir$agent_note (logging to $logfile)"
    _dev_new_session "$session" "$dir" "$branch" "$skip_prepare" "$agent"
    tmux attach-session -t "=$session:"
  fi
}

# _dev_remote_resolve <repo> <slot> — resolve a live REMOTE dev slot to one
# "<host>\t<repo>\t<slot>" line on stdout (host AUTO-INFERRED). Scans every
# $REMOTE_HOSTS host (via _dev_rows_all, minus local) for the live candidates:
# `<repo> <slot>` → just that slot; `<repo>` (no slot) → every slot of that repo;
# empty → every remote slot. One candidate → it; several → **fzf-pick** (host/slot +
# summary; needs a TTY+fzf, else it lists them and returns 1); none → return 1 with a
# `dev ls -r` hint. The chosen slot name "<repo>-<num>" is split on the LAST dash
# (repos like `dotfiles` have none) so the repo+slot come back fully resolved. Shared
# by `t open` (explicit -r and the auto-detect attach) and `dev -r kill`. Diagnostics
