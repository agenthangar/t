"""Retry cleanup only for verified last-terminal losses."""
from functools import lru_cache
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
DAY = 24 * 60 * 60


@lru_cache(maxsize=1)
def terminal():
    return runpy.run_path(str(ROOT / 'libexec/t_terminal_close.py'))


def base():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/background'


def queue():
    return base() / 'cleanup'


def identity(path):
    info = Path(path).stat()
    return [info.st_dev, info.st_ino]


def git(workspace, *args):
    result = terminal()['run'](['git', '-C', workspace, *args], timeout=5)
    return result.stdout.strip() if not result.returncode else None


def branch_identity(workspace):
    url = git(workspace, 'remote', 'get-url', 'origin') or ''
    known = re.fullmatch(r'(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?', url)
    branch = git(workspace, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    return (known[1], branch) if known and branch else (None, None)


def context(socket, session, pane, pane_pid, generation, agent_pid, transcript):
    api = terminal()
    if not api['attachment_matches'](socket, session, pane, pane_pid, generation):
        raise ValueError('session reattached or changed')
    socket_id = api['runtime']()['socket_identity'](socket)
    if not socket_id:
        raise ValueError('unknown tmux server')
    return dict(socket_id=list(socket_id), transcript_id=identity(transcript),
                agent_birth=api['process_detail'](int(agent_pid))['birth'],
                pane_birth=api['process_detail'](int(pane_pid))['birth'])


def arm(socket, session, pane, pane_pid, generation, agent, sid, pid, workspace, transcript, minutes):
    if not (re.fullmatch(r'[0-9]{1,4}', minutes) and 1 <= int(minutes) < 1440 and
            re.fullmatch(r'[\da-fA-F]{8}(?:-[\da-fA-F]{4}){3}-[\da-fA-F]{12}', sid) and
            agent in ('codex', 'claude') and pid.isdecimal()):
        return
    api = terminal()
    ticket = dict(socket=socket, session=session, pane=pane, pane_pid=pane_pid, generation=generation,
                  agent=agent, sid=sid, pid=pid, workspace=workspace, transcript=transcript)
    workspace_id = identity(workspace)
    ticket.update(context(socket, session, pane, pane_pid, generation, pid, transcript))
    state, prs, turn = api['transcript_observation'](agent, transcript)
    if not turn or state not in ('busy', 'idle'):
        return
    if state == 'idle':
        retained = api['history_prs'](agent, sid)
        if retained is None or api['prs_complete'](workspace, prs | retained) is not False:
            return
    slug, branch = branch_identity(workspace)
    if not slug or not branch:
        return
    now = time.time()
    interval = int(minutes) * 60
    ticket.update(version=1, workspace_id=workspace_id, slug=slug, branch=branch, turn=turn,
                  expires=now + DAY, due=now + interval, interval=interval)
    if not unchanged(ticket):
        return
    folder = queue()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        return
    key = hashlib.sha256(json.dumps([ticket['socket_id'], session, generation]).encode()).hexdigest()
    path = folder / (key + '.json')
    with tempfile.NamedTemporaryFile(dir=folder, mode='w', delete=False) as stream:
        pending = Path(stream.name)
        json.dump(ticket, stream)
    try:
        os.link(pending, path)
    except FileExistsError:
        pass
    finally:
        pending.unlink(missing_ok=True)


def unchanged(ticket):
    if (ticket['version'] != 1 or time.time() >= ticket['expires'] or
            identity(ticket['workspace']) != ticket['workspace_id']):
        return False
    observed = context(*(ticket[key] for key in
                         ('socket', 'session', 'pane', 'pane_pid', 'generation', 'pid', 'transcript')))
    return all(ticket.get(key) == value for key, value in observed.items())


def merged_head(ticket, head):
    api = terminal()
    slug, branch = ticket['slug'], ticket['branch']
    repo = api['run'](['gh', 'repo', 'view', slug, '--json', 'defaultBranchRef'], timeout=5)
    rows = api['run'](['gh', 'pr', 'list', '--repo', slug, '--head', branch, '--state', 'merged',
                       '--limit', '20', '--json', 'headRefName,headRefOid,baseRefName,isCrossRepository,mergedAt'],
                      timeout=8)
    if repo.returncode or rows.returncode:
        return False
    try:
        base_branch = json.loads(repo.stdout)['defaultBranchRef']['name']
        return bool(base_branch and any(row.get('headRefName') == branch and row.get('headRefOid') == head and
                    row.get('baseRefName') == base_branch and row.get('isCrossRepository') is False and
                    row.get('mergedAt') for row in json.loads(rows.stdout)))
    except (ValueError, TypeError, AttributeError, KeyError):
        return False


class Cancelled(ValueError):
    """Permanent cancellation of the specific window-loss authorization."""


def validate(proof, *binding):
    ticket, head = proof['ticket'], proof['head']
    if binding and binding != tuple(ticket[key] for key in ('agent', 'sid', 'pid', 'workspace', 'transcript')):
        return False
    if (base() / 'cleanup.stopped').exists() or not unchanged(ticket):
        return False
    if branch_identity(ticket['workspace']) != (ticket['slug'], ticket['branch']):
        return False
    api = terminal()
    before = api['revision'](ticket['workspace'], ticket['transcript'])
    if before is None or before[0].strip() != head:
        return False
    state, _, turn = api['transcript_observation'](ticket['agent'], ticket['transcript'])
    return (state == 'idle' and turn == ticket['turn'] and
            api['revision'](ticket['workspace'], ticket['transcript']) == before)


def prepare(ticket):
    api = terminal()
    if not unchanged(ticket) or branch_identity(ticket['workspace']) != (ticket['slug'], ticket['branch']):
        raise Cancelled('owner or branch changed')
    workspace, transcript = ticket['workspace'], ticket['transcript']
    state, prs, turn = api['transcript_observation'](ticket['agent'], transcript)
    if turn and turn != ticket['turn']:
        raise Cancelled('new user turn')
    if state != 'idle':
        return
    revision = api['revision'](workspace, transcript)
    if not revision:
        return
    current = api['owner'](*(ticket[key] for key in ('socket', 'session', 'pane', 'pane_pid')))
    if not current:
        raise Cancelled('owner changed or attached')
    screen = api['tmux'](ticket['socket'], 'capture-pane', '-p', '-e', '-t', ticket['pane'])
    if screen.returncode:
        return
    composer = api['composer_empty'](ticket['agent'], screen.stdout, current['cursor'], current.get('column'))
    if composer is False:
        raise Cancelled('unsent draft')
    if composer is not True:
        return
    try:
        api['process_plan'](ticket['pid'], workspace, os.getpid())
    except (OSError, ValueError, KeyError):
        return
    retained = api['history_prs'](ticket['agent'], ticket['sid'])
    if retained is None or api['prs_complete'](workspace, prs | retained) is not True:
        return
    proof = dict(ticket=ticket, head=revision[0].strip())
    if merged_head(ticket, proof['head']) and validate(proof):
        return proof


def read_ticket(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('oversized cleanup ticket')
    return json.loads(data)


def tick(stopped):
    for path in sorted(queue().glob('*.json')):
        if stopped.is_set() or (base() / 'cleanup.stopped').exists():
            break
        try:
            ticket = read_ticket(path)
            now = time.time()
            if ticket['expires'] <= now:
                path.unlink(missing_ok=True)
                continue
            if ticket['due'] > now:
                continue
            proof = prepare(ticket)
            if proof and not stopped.is_set():
                # Consume before any lifecycle effects so a crash never repeats a stop.
                path.unlink()
                api = terminal()
                result = api['dispatch_close'](ticket['socket'], ticket['session'], ticket['pane'],
                                               ticket['pane_pid'], ticket['generation'], json.dumps(proof))
                api['receipt'](ticket['socket'], ticket['session'],
                               dict(delayed=True, decision='preserve' if result.returncode else 'terminate',
                                    code=result.returncode, reason=result.stderr[-2000:].strip()))
            else:
                ticket['due'] = now + ticket['interval']
                terminal()['atomic_write'](path, json.dumps(ticket))
        except (Cancelled, FileNotFoundError, ProcessLookupError):
            path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError, SyntaxError):
            # Unknown private evidence cannot authorize a stop.
            path.unlink(missing_ok=True)


def watch():
    folder = queue()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        return 1
    fd = os.open(folder / 'worker.lock', os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        (base() / 'cleanup.stopped').unlink(missing_ok=True)
        terminal()['atomic_write'](base() / 'cleanup.fingerprint', hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        while not stopped.is_set():
            tick(stopped)
            stopped.wait(5)
    return 0


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['watch']:
            raise SystemExit(watch())
        if len(sys.argv) == 13 and sys.argv[1] == 'arm':
            arm(*sys.argv[2:])
            raise SystemExit(0)
        if len(sys.argv) in (3, 8) and sys.argv[1] == 'validate':
            raise SystemExit(0 if validate(json.loads(sys.argv[2]), *sys.argv[3:]) else 1)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        raise SystemExit(1)
    raise SystemExit(2)
