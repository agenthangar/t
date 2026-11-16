"""Conservative, event-driven cleanup after a managed terminal is lost.

No keyboard input is read. A detached client with a usable terminal is deliberately
ignored. Unknown agent/composer/Git/process evidence always retains the owner.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HOOK = 'client-detached[50100]'
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def run(args, **kwargs):
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=kwargs.pop('timeout', 15), cwd='/', **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 1, '', str(error))


def tmux(socket, *args):
    return run(['tmux', '-S', socket, *args])


def hung_up(fd):
    poll = select.poll()
    poll.register(fd, select.POLLHUP | select.POLLERR)
    return any(events & select.POLLHUP for _, events in poll.poll(0))


def install(session):
    result = run(['tmux', 'display-message', '-p', '-t', '=' + session + ':',
                  '#{session_name}\t#{session_id}\t#{socket_path}\t#{pane_id}\t#{pane_pid}'])
    fields = result.stdout.strip().split('\t')
    if result.returncode or len(fields) != 5 or not fields[0].startswith('dev-'):
        return 1
    _, identity, socket, pane, pane_pid = fields
    worker = shlex.join([sys.executable, str(Path(__file__).resolve()), 'detached',
                        socket, identity, pane, pane_pid])
    # Ordinary client formats can be gone by the time run-shell executes. The
    # hook's saved client name is the terminal path; pin the owner at install.
    worker += ' #{q:hook_client} >/dev/null 2>&1 || :'
    # A private array element preserves the user's other session hooks. Hook
    # expansion quotes event data, not executable shell fragments.
    result = tmux(socket, 'set-hook', '-t', identity + ':', HOOK,
                  shlex.join(['run-shell', '-b', worker]))
    if result.returncode:
        return result.returncode
    attached = shlex.join([sys.executable, str(Path(__file__).resolve()), 'attached', socket])
    # Older tmux hooks retain hook_client but not ordinary client formats.
    # Resolve the PID from that saved terminal name while it is still attached.
    # Hook failures must never put the agent pane into run-shell copy mode.
    attached += ' #{q:hook_client} >/dev/null 2>&1 || :'
    for hook in ('client-attached[50100]', 'client-session-changed[50100]'):
        result = tmux(socket, 'set-hook', '-t', identity + ':', hook,
                      shlex.join(['run-shell', attached]))
        if result.returncode:
            return result.returncode
    return result.returncode


def event_path(socket, tty):
    root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/terminal-close'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError('unsafe terminal observer state')
    return root / hashlib.sha256((socket + tty).encode()).hexdigest()


def attached(socket, tty, pid=None):
    """Hold the original PTY open, so a recycled /dev/tty name is never evidence."""
    if not re.fullmatch(r'/dev/(?:pts/\d+|tty[\w.-]+)', tty):
        return 1
    if pid is None:
        result = tmux(socket, 'list-clients', '-F', '#{client_tty}|#{client_pid}')
        matches = [line.split('|')[1] for line in result.stdout.splitlines()
                   if line.startswith(tty + '|') and line.count('|') == 1]
        if result.returncode or len(matches) != 1:
            return 1
        pid = matches[0]
    if not pid.isdigit():
        return 1
    base = event_path(socket, tty)
    lock = os.open(str(base) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fd = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # the same client switched managed sessions
        fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        if hung_up(fd):
            return 1  # missed attachment: preserve, never guess
        Path(str(base) + '.event').unlink(missing_ok=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watch', socket, tty, pid,
                          str(fd), str(lock)], pass_fds=(fd, lock), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd='/', env=env)
        atomic_write(Path(str(base) + '.ready'), pid)
        return 0
    finally:
        if fd is not None:
            os.close(fd)
        os.close(lock)


def notify_detached(socket, identity, pane, pane_pid, tty):
    base = event_path(socket, tty)
    atomic_write(Path(str(base) + '.event'), json.dumps([identity, pane, pane_pid]))
    return 0


def watch(socket, tty, pid, descriptor, lock_descriptor):
    fd, lock = int(descriptor), int(lock_descriptor)
    event = Path(str(event_path(socket, tty)) + '.event')
    gone_at = None
    try:
        while True:
            if event.exists():
                identity, pane, pane_pid = json.loads(event.read_text())
                event.unlink(missing_ok=True)
                return detached(socket, identity, pane, pane_pid, tty, fd)
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                gone_at = gone_at or time.monotonic()
            if hung_up(fd):
                gone_at = gone_at or time.monotonic()
            if gone_at is not None and time.monotonic() - gone_at > 5:
                return 0  # callback unavailable: no cleanup, even after PID reuse
            time.sleep(.1)
    finally:
        Path(str(event_path(socket, tty)) + '.ready').unlink(missing_ok=True)
        os.close(fd)
        os.close(lock)


def owner(socket, identity, pane, pane_pid):
    result = tmux(socket, 'list-panes', '-s', '-t', identity + ':', '-F',
                  '#{session_id}\t#{session_name}\t#{pane_id}\t#{pane_pid}\t'
                  '#{session_path}\t#{session_attached}\t#{pane_dead}\t#{cursor_y}')
    rows = result.stdout.splitlines()
    if result.returncode or len(rows) != 1:
        return None
    fields = rows[0].split('\t')
    if (len(fields) != 8 or fields[0] != identity or fields[2:4] != [pane, pane_pid]
            or not fields[1].startswith('dev-') or fields[5:7] != ['0', '0']):
        return None
    return dict(zip(('identity', 'session', 'pane', 'pid', 'workspace', 'attached',
                     'dead', 'cursor'), fields))


def composer_empty(agent, screen, cursor):
    """Recognize a blank current composer, never a prompt quoted in scrollback."""
    if agent not in ('codex', 'claude') or not cursor.isdigit():
        return None
    lines = screen.splitlines()
    row = int(cursor)
    if row >= len(lines):
        return None
    marker = '›' if agent == 'codex' else '❯'
    line = ANSI.sub('', lines[row]).strip()
    if not line.startswith(marker):
        return None
    if line[len(marker):].strip():
        # Codex paints its startup suggestion dim; it is not submitted input.
        # Require the actual style and known placeholder, not just its text.
        raw_input = lines[row].split(marker, 1)[1]
        suggestion = line[len(marker):].strip()
        placeholder = (agent == 'codex' and re.match(r'^\s*\x1b\[(?:\d+;)*2(?:;\d+)*m', raw_input)
                       and re.fullmatch(r'(?:Implement \{feature\}|Write tests for @filename|'
                                        r'Explain this codebase|Find and fix a bug in @filename|'
                                        r'Improve documentation in @filename|Summarize recent commits)', suggestion))
        if not placeholder:
            return False
    # Multiline drafts/dialogs below the cursor must not be mistaken for a
    # blank prompt. Only recognized CLI footer lines and borders are accepted.
    for raw in lines[row + 1:]:
        text = ANSI.sub('', raw).strip()
        if not text:
            continue
        # Draft text can imitate footer wording or a border. Only actual dim
        # chrome is eligible; unstyled multiline content must be retained.
        dim = re.match(r'^\s*\x1b\[(?:\d+;)*2(?:;\d+)*m', raw)
        if dim and re.fullmatch(r'[─━═\s]+', text):
            continue
        if dim and re.fullmatch(r'\? for shortcuts(?:\s+.*)?', text):
            continue
        if dim and agent == 'codex' and re.fullmatch(r'\d+% context left(?:\s+.*)?', text):
            continue
        return None
    return True


def transcript_state(agent, path):
    """Return idle/empty/busy/unknown and associated PR URLs from real records."""
    if not path:
        return 'unknown', set()
    try:
        with open(path, 'rb') as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024 or (data and not data.endswith(b'\n')):
            return 'unknown', set()
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
    except (OSError, ValueError):
        return 'unknown', set()
    state, prs = 'empty', set()
    for record in records:
        if not isinstance(record, dict):
            return 'unknown', prs
        prs.update('https://' + value for value in re.findall(r'github\.com/[\w.-]+/[\w.-]+/pull/\d+', json.dumps(record)))
        if agent == 'codex' and record.get('type') == 'response_item':
            payload = record.get('payload') or {}
            if not isinstance(payload, dict):
                return 'unknown', prs
            if payload.get('role') in ('user', 'assistant') or payload.get('type') in ('function_call', 'function_call_output'):
                state = 'busy'
        elif agent == 'codex' and record.get('type') == 'event_msg':
            payload = record.get('payload') or {}
            if not isinstance(payload, dict):
                return 'unknown', prs
            kind = payload.get('type')
            if kind in ('task_started', 'user_message'):
                state = 'busy'
            elif kind == 'task_complete':
                state = 'idle'
            elif kind == 'turn_aborted':
                state = 'unknown'  # an interrupted task is not completed work
            elif kind in ('agent_message', 'agent_reasoning') and state == 'empty':
                state = 'unknown'
        elif agent == 'claude':
            message = record.get('message') or {}
            if not isinstance(message, dict):
                return 'unknown', prs
            if record.get('type') == 'user' and not any(record.get(key) for key in
                    ('isMeta', 'isSidechain', 'isCompactSummary')):
                state = 'busy'
            elif record.get('type') == 'assistant' and not record.get('isSidechain'):
                state = 'idle' if message.get('stop_reason') == 'end_turn' else 'busy'
    return (state if agent in ('codex', 'claude') else 'unknown'), prs


def history_prs(agent, sid):
    """Retained associations survive transcript compaction and branch changes."""
    if not sid:
        return set()
    path = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/history/v1/history.sqlite3'
    try:
        path.stat()
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)
        try:
            rows = db.execute("select record from conversations where json_extract(record,'$.agent')=? and json_extract(record,'$.sid')=?", (agent, sid)).fetchall()
        finally:
            db.close()
        urls = set()
        for row in rows:
            for pr in json.loads(row[0]).get('prs', []):
                if (not isinstance(pr, dict) or not re.fullmatch(r'[\w.-]+/[\w.-]+', pr.get('repo', ''))
                        or type(pr.get('number')) is not int or pr['number'] < 1):
                    return None
                urls.add(f'https://github.com/{pr["repo"]}/pull/{pr["number"]}')
        return urls
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        return None


def prs_complete(workspace, prs):
    if len(prs) > 8:
        return None
    origin = run(['git', '-C', workspace, 'remote', 'get-url', 'origin'])
    if origin.returncode:
        return None
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?', origin.stdout.strip())
    if not match:
        return None  # other hosts have no verified PR reader yet
    branch = run(['git', '-C', workspace, 'symbolic-ref', '--quiet', '--short', 'HEAD'])
    if branch.returncode or not branch.stdout.strip():
        return None
    deadline = time.monotonic() + 10
    result = run(['gh', 'pr', 'list', '--repo', match[1], '--head', branch.stdout.strip(),
                  '--state', 'open', '--limit', '1', '--json', 'number'], timeout=10)
    try:
        rows = json.loads(result.stdout)
        if result.returncode or not isinstance(rows, list):
            return None
        if rows:
            return False
        for url in sorted(prs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            result = run(['gh', 'pr', 'view', url, '--json', 'state'], timeout=remaining)
            value = json.loads(result.stdout)
            if result.returncode or value.get('state') not in ('MERGED', 'CLOSED'):
                return False if value.get('state') == 'OPEN' else None
    except (ValueError, AttributeError):
        return None
    return True


def startup_screen(agent, screen, cursor):
    lines = [ANSI.sub('', line).strip(' │') for line in screen.splitlines()[:int(cursor)]]
    brand = 'OpenAI Codex' if agent == 'codex' else 'Welcome back'
    if not any(brand in line for line in lines):
        return False
    # No-transcript startup is only accepted while the visible screen is still
    # the recognized welcome panel. Ordinary output cannot stand in for history.
    return all(not line or re.fullmatch(r'[╭╮╰╯┌┐└┘─━═\s]+', line)
               or brand in line or re.match(r'^(?:model:|directory:|Tip:)', line)
               for line in lines)


def revision(workspace, transcript):
    """A changed observation during slow PR checks revokes termination."""
    head = run(['git', '-C', workspace, 'rev-parse', 'HEAD'])
    dirty = run(['git', '-C', workspace, 'status', '--porcelain', '--untracked-files=all'])
    if head.returncode or dirty.returncode or dirty.stdout:
        return None
    try:
        stat = os.stat(transcript) if transcript else None
        return (head.stdout, (stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat else None)
    except OSError:
        return None


def eligibility(socket, identity, pane, pane_pid, agent, transcript, fresh, git_clean, processes_clear, sid=''):
    current = owner(socket, identity, pane, pane_pid)
    if not current:
        return {'decision': 'preserve', 'reason': 'owner changed or attached'}
    if git_clean != '1' or processes_clear != '1':
        return {'decision': 'preserve', 'reason': 'pending work or unknown process/Git state'}
    before = revision(current['workspace'], transcript)
    if before is None:
        return {'decision': 'unknown', 'reason': 'unverified revision'}
    screen = tmux(socket, 'capture-pane', '-p', '-e', '-t', pane)
    empty = None if screen.returncode else composer_empty(agent, screen.stdout, current['cursor'])
    if empty is not True:
        return {'decision': 'preserve' if empty is False else 'unknown', 'reason': 'draft or unrecognized composer'}
    state, prs = transcript_state(agent, transcript)
    # Only a fresh lifecycle launch can prove a no-transcript startup. A missing
    # resumed transcript or a missing session ID never constitutes that proof.
    if (state == 'unknown' and not transcript and fresh == '1'
            and startup_screen(agent, screen.stdout, current['cursor'])):
        state = 'empty'
    if state not in ('idle', 'empty'):
        return {'decision': 'preserve' if state == 'busy' else 'unknown', 'reason': 'unfinished or unknown turn'}
    retained = history_prs(agent, sid)
    if retained is None:
        return {'decision': 'unknown', 'reason': 'unreadable PR associations'}
    complete = prs_complete(current['workspace'], prs | retained)
    if complete is not True:
        return {'decision': 'preserve' if complete is False else 'unknown', 'reason': 'open or unverified PR'}
    # Include the screen and transcript identity in the final observation. The
    # shell performs this check again inside lifecycle stop before any signal.
    after_screen = tmux(socket, 'capture-pane', '-p', '-e', '-t', pane)
    if (owner(socket, identity, pane, pane_pid) != current or after_screen.returncode
            or after_screen.stdout != screen.stdout or revision(current['workspace'], transcript) != before):
        return {'decision': 'preserve', 'reason': 'owner changed during inspection'}
    return {'decision': 'terminate', 'reason': 'empty or completed session'}


def detached(socket, identity, pane, pane_pid, tty, terminal_fd):
    if not (socket.startswith('/') and re.fullmatch(r'\$\d+', identity)
            and re.fullmatch(r'%\d+', pane) and pane_pid.isdigit()):
        return 1
    # Use the original descriptor, never a device name which may be recycled.
    lost = hung_up(terminal_fd)
    current = owner(socket, identity, pane, pane_pid)
    if not lost or not current:
        receipt(socket, identity, {'decision': 'preserve', 'reason': 'terminal still usable' if not lost else 'owner changed or attached'})
        return 0
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env['T_HOME'] = str(ROOT)
    # Source repository helpers explicitly: no personal dotfiles, no source
    # selector which could redirect this event into another development tree.
    script = '''
setopt no_monitor no_notify
autoload -Uz add-zsh-hook
zmodload zsh/datetime
zmodload -F zsh/stat b:zstat
source "$T_HOME/ui/fzf.sh"
for module in config agent worktree sessions remote resume lifecycle; do
  source "$T_HOME/zsh/$module.zsh" || exit
done
_t_terminal_close_event "$@"
'''
    result = run(['zsh', '-f', '-c', script, 't-terminal-close', socket, identity,
                  pane, pane_pid], env=env, timeout=90)
    receipt(socket, identity, {'decision': 'terminate' if result.returncode == 0 else 'preserve',
                              'reason': result.stderr[-2000:], 'code': result.returncode})
    return result.returncode


def receipt(socket, identity, value):
    root = event_path(socket, identity).parent
    key = hashlib.sha256((socket + identity).encode()).hexdigest()
    atomic_write(root / (key + '.json'), json.dumps(value))


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
    try:
        os.replace(stream.name, path)
    finally:
        Path(stream.name).unlink(missing_ok=True)


def main(args):
    if len(args) == 2 and args[0] == 'install':
        return install(args[1])
    if len(args) == 6 and args[0] == 'detached':
        return notify_detached(*args[1:])
    if len(args) in (3, 4) and args[0] == 'attached':
        return attached(*args[1:])
    if len(args) == 6 and args[0] == 'watch':
        return watch(*args[1:])
    if len(args) == 11 and args[0] == 'check':
        result = eligibility(*args[1:])
        print(json.dumps(result))
        return 0 if result['decision'] == 'terminate' else 1
    return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
