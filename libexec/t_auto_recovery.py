"""Opt-in host recovery. Durable intent is separate from disposable UI recovery offers."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import random
import re
import runpy
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.agenthangar.t.recovery'
DISPLAY_NAME = 't Recovery'
LSREGISTER = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
SID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
AGENTS = ('codex', 'claude', 'cursor')
LIMIT = 8 * 1024 * 1024


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def root():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'t/recovery'


def settings_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'t/recovery.json'


def read(path, default=None):
    try:
        if path.is_symlink() or path.stat().st_size > LIMIT:
            raise ValueError('unsafe recovery file: ' + str(path))
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('invalid recovery file: ' + str(path))
        return value
    except FileNotFoundError:
        return {} if default is None else default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('refusing recovery symlink: ' + str(path))
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(name):
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root()/name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update(fn):
    with locked('journal.lock'):
        data = read(root()/'journal.json', {'sessions': {}, 'launches': {}})
        result = fn(data)
        write(root()/'journal.json', data)
        return result


def enabled():
    return read(settings_path()).get('enabled') is True


def run(argv, **kwargs):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=kwargs.pop('timeout', 30), **kwargs)


def boot():
    if sys.platform == 'darwin':
        # kern.boottime is a wall-clock estimate and can change within one boot.
        result = run(['sysctl', '-n', 'kern.bootsessionuuid'])
        if result.returncode:
            raise ValueError('cannot determine boot identity')
        return str(uuid.UUID(result.stdout.strip()))
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def workspace(cwd):
    directory = Path(cwd).resolve(strict=True)
    stat = directory.stat()
    result = run(['git', '-C', str(directory), 'rev-parse', '--path-format=absolute', '--git-common-dir'])
    common = result.stdout.strip() if not result.returncode else ''
    result = {'path': str(directory), 'device': stat.st_dev, 'inode': stat.st_ino, 'git': common}
    if hasattr(stat, 'st_birthtime_ns'):
        result['birth'] = stat.st_birthtime_ns
    return result


def workspace_matches(row, cwd=None):
    current = workspace(cwd or row['cwd'])
    expected = row['workspace']
    if 'birth' in expected:
        # The APFS mount device number can change after power loss.
        return all(current.get(field) == expected[field] for field in ('path', 'inode', 'git', 'birth'))
    current.pop('birth', None)  # A journal from t-9 lacks the optional birth stamp.
    return current == expected


def key(agent, sid):
    if agent not in AGENTS or not isinstance(sid, str) or not SID.fullmatch(sid):
        raise ValueError('an exact supported conversation is required')
    return agent + ':' + sid


def retry_delay(attempt):
    return min(300, 2 ** min(attempt + 1, 9) * random.uniform(.9, 1.1))


def snapshot():
    recovery = helper('t_recovery')
    result = run(['tmux', 'display-message', '-p', '#{socket_path}'])
    sockets = {result.stdout.strip()} if result.returncode == 0 else set()
    sockets.update(r.get('socket', '') for r in read(root()/'journal.json').get('sessions', {}).values())
    rows = []
    for socket in sorted(sockets - {''}):
        for pane in recovery['panes'](socket):
            owner = recovery['owner'](socket, pane)
            if owner and owner.get('sid') and owner.get('pid'):
                rows.append(owner)
    return rows


def enroll(rows):
    if not rows:
        return
    current_boot = boot()
    # Never infer option boundaries from the flattened output of ps.
    process_detail = helper('t_terminal_close')['process_detail']
    recovery = helper('t_recovery')
    pending = {}
    before = read(root()/'journal.json').get('sessions', {})
    for row in rows:
        identity = key(row['agent'], row['sid'])
        old = before.get(identity, {})
        if old and (not old.get('open') or old.get('supervisor_pid') or
                    (old.get('argv_verified') and old.get('boot') == current_boot)):
            continue
        spec = {'safe': False}
        try:
            location = workspace(row['cwd'])
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
        try:
            native = process_detail(int(row['pid']))
            if (native['import_safe'] and Path(native['cwd']).resolve() == Path(row['cwd']).resolve()
                    and recovery['current'](row)):
                parsed = launch_options(row['agent'], native['argv'])
                if parsed.get('sid') in ('', row['sid']) and native['argv'][1:2] != ['app-server']:
                    spec = {**parsed, 'executable': native['executable']}
        except (ValueError, OSError, IndexError, KeyError, subprocess.SubprocessError):
            pass
        spec.pop('sid', None)
        pending[identity] = (row, old.get('generation'), location, spec)
    if not pending:
        return

    def change(data):
        for identity, (row, generation, location, spec) in pending.items():
            old = data['sessions'].get(identity, {})
            if old.get('generation') != generation:
                continue
            if old:
                old.update({k: row[k] for k in ('session', 'cwd', 'socket', 'pane', 'pane_pid', 'pid', 'start')
                            if k in row})
                old.update(**spec, argv_verified=True, workspace=location, boot=current_boot)
                if old.get('phase') == 'attention' and not old.get('submission'):
                    old.update(phase='unknown', failures=0, next_retry=0,
                               outcome='', reason='Existing session: waiting for verified lifecycle events')
                continue
            data['sessions'][identity] = {**row, 'workspace': location,
                **spec, 'argv_verified': True,
                'boot': current_boot, 'generation': uuid.uuid4().hex, 'phase': 'unknown', 'open': True, 'paused': False,
                'reason': 'Existing session: waiting for verified lifecycle events', 'updated': time.time()}
    update(change)


def launch_options(agent, argv):
    """Persist only explicit launch options, never the original prompt or credentials."""
    values = {'--model', '-m', '--effort', '--sandbox', '-s', '--ask-for-approval', '-a',
              '--permission-mode', '--allowedTools', '--disallowedTools', '--add-dir',
              '--enable', '--disable', '-c', '--config', '--settings'}
    flags = {'--no-daemon', '--full-auto', '--approve-for-me', '--dangerously-bypass-approvals-and-sandbox',
             '--dangerously-skip-permissions', '--allow-dangerously-skip-permissions',
             '--force', '--yolo', '--plan'}
    saved, sid, safe = [], '', True
    args = iter(argv[1:])
    for arg in args:
        if arg == '--':
            break
        if arg in ('resume', '--resume', '-r', '--session-id'):
            sid = next(args, '')
        elif arg.startswith('--resume='):
            sid = arg.split('=', 1)[1]
        elif arg in ('--cd', '-C', '--workspace'):
            next(args, None)
        elif arg in values:
            value = next(args, None)
            approved = True
            if arg in ('-c', '--config') and value:
                approved = value.split('=', 1)[0] in ('model', 'model_reasoning_effort', 'service_tier',
                    'sandbox_mode', 'approval_policy', 'approvals_reviewer')
            if arg == '--settings' and value:
                try:
                    settings = json.loads(value)
                    approved = isinstance(settings, dict) and set(settings) <= {'fastMode'}
                except ValueError:
                    approved = False
            if not approved or value is None or re.search(r'(api[_-]?key|token|secret|password|credential|authorization)', value, re.I):
                safe = False
            else:
                saved.extend([arg, value])
        elif arg in flags:
            saved.append(arg)
        elif arg.startswith('-'):
            safe = False
        else:
            break  # positional prompt
    return {'executable': argv[0], 'options': saved, 'sid': sid, 'safe': safe}


def holding_panes():
    result = run(['tmux', 'list-panes', '-a', '-F',
                  '#{session_name}|#{pane_id}|#{@t_recovery_holding}|#{socket_path}'])
    if result.returncode:
        return []
    return [(socket, session, pane, marker) for line in result.stdout.splitlines()
            if len(fields := line.split('|', 3)) == 4
            for session, pane, marker, socket in (fields,) if marker]


def find_holding(row, panes=None):
    if panes is None:
        panes = holding_panes()
    return next(((sock, pane) for sock, session, pane, marker in panes
                 if session == row.get('session') and marker == row['sid']), None)


def display_text(value):
    return ''.join(ch if ch.isprintable() else ' ' for ch in str(value or '')).strip()


def holding_metadata(row):
    # Resolve only the exact conversation. Never attribute a newer conversation
    # in the same directory to this pane.
    try:
        agent, sid, cwd = row['agent'], row['sid'], row['cwd']
        key(agent, sid)
        if agent == 'claude':
            claude = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))
            transcript = claude/'projects'/re.sub(r'[^A-Za-z0-9]', '-', cwd)/(sid + '.jsonl')
        elif agent == 'codex':
            codex = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))
            paths = [Path(row['transcript'])] if row.get('transcript') else list(
                codex.glob('sessions/*/*/*/rollout-*-' + sid + '.jsonl'))
            if len(paths) != 1 or not paths[0].is_file():
                return {}
            transcript = Path(paths[0])
            location = helper('t_codex_ownership')['_rollout_cwd'](transcript, sid)
            if Path(location).resolve() != Path(cwd).resolve():
                return {}
        else:
            return {}
        record = helper('t_history')['transcript'](transcript, agent, metadata_only=True)
        if record['sid'] != sid:
            return {}
        detail = helper('t_session_details')
        started = detail['transcript_last_turn'](transcript, agent)
        return {'title': record.get('title') or record.get('summary'),
                'last_turn': detail['format_last_turn'](started) if started else ''}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def holding_rows():
    if not enabled():
        return
    panes = holding_panes()
    if not panes:
        return
    for row in read(root()/'journal.json').get('sessions', {}).values():
        if not row.get('open') or not find_holding(row, panes):
            continue
        metadata = holding_metadata(row)
        title = display_text(metadata.get('title')) or 'Saved conversation'
        yield '\t'.join((row['sid'], row['cwd'], row['session'].removeprefix('dev-'),
                         'detached', 'idle', title + ' · Holding pane', row['agent']))


def holding_resume_notice(error):
    if isinstance(error, subprocess.TimeoutExpired):
        return 'Resume timed out. Choose Resume conversation to try again.'
    # The shell writes incidental scan warnings before the final blocking reason.
    lines = str(error).strip().splitlines()
    cause = display_text(lines[-1]) if lines else ''
    if cause == 'warning: t: saved conversation ownership is unknown because a local or configured host scan failed':
        return ('Couldn’t check whether this conversation is already open on this computer. '
                'Choose Resume conversation to try again.')
    owner_prefix = 't: saved conversation is already live in '
    if cause.startswith(owner_prefix):
        return f'This conversation is already open in {cause[len(owner_prefix):]}. Retry after that session closes.'
    if not cause or cause.startswith('warning:') or cause == 'guarded session restore failed':
        return 'Couldn’t resume this conversation. Choose Resume conversation to try again.'
    return 'Couldn’t resume. ' + cause.removeprefix('t recovery: ').removeprefix('t: ')


def holding(agent, sid, generation):
    identity = key(agent, sid)
    os.environ['T_DEV_CONTEXT'] = str(ROOT)
    os.environ['T_DEV_CONTEXT_OWNER'] = str(os.getpid())
    ui = runpy.run_path(str(ROOT/'bin/t'))['_RailUI']()
    ui.key_separator = '  '
    runtime = Runtime()
    notice = ''
    while True:
        row = read(root()/'journal.json').get('sessions', {}).get(identity)
        if not enabled() or not row or not row.get('open') or row['generation'] != generation:
            return 0
        info = holding_metadata(row)
        slot = row['session'].removeprefix('dev-')
        context = [('Working on', display_text(info.get('title')) or 'Saved conversation'),
                   ('Agent', agent.title()), ('Conversation', sid),
                   ('Workspace', display_text(row['cwd'])),
                   ('Status', 'Open pane')]
        if info.get('last_turn'):
            context.insert(-1, ('Last turn', display_text(info['last_turn'])))
        ui.raw()
        pane_menu = False
        try:
            action = ui.pick(slot + '  Recovery',
                             [('resume', 'Resume conversation'), ('dismiss', 'Dismiss holding pane'),
                              ('back', 'Detach')], context=context, back_label='detach', notice=notice)
            notice = ''
            if action == 'resume':
                pane_menu = True
            elif action == 'dismiss':
                pane_menu = True
                action = ui.pick('Dismiss this holding pane?',
                                 [('back', 'Keep holding pane'), ('remove', 'Dismiss this holding pane')],
                                 back_label='back')
        except KeyboardInterrupt:
            action = None
        finally:
            ui.restore()
        if action in (None, 'back') and not pane_menu:
            client = run(['tmux', 'display-message', '-p', '-t', os.environ['TMUX_PANE'], '#{client_name}'])
            if client.returncode == 0 and client.stdout.strip():
                run(['tmux', 'detach-client', '-t', client.stdout.strip()])
            continue
        if action not in ('resume', 'remove'):
            continue
        with locked('session-'+hashlib.sha256(identity.encode()).hexdigest()+'.lock'):
            latest = read(root()/'journal.json').get('sessions', {}).get(identity, {})
            pane = find_holding(row)
            if (not enabled() or latest.get('paused') or not latest.get('open') or
                    latest.get('generation') != generation or not pane
                    or pane[1] != os.environ.get('TMUX_PANE')):
                notice = 'Recovery state changed. Reopen the slot to review it again.'
                continue
            if action == 'remove':
                retire(agent, sid)
                return 0
            # This explicit selection authorizes current defaults. Discard all
            # partially recovered options and require a fresh managed launch.
            try:
                options = []
                runtime.restore({**latest, 'safe': True, 'executable': {'cursor': 'cursor-agent'}.get(agent, agent),
                                 'options': options}, '', pane=pane)
                return 0
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                notice = holding_resume_notice(exc)


def begin(agent, argv, cwd):
    token = uuid.uuid4().hex
    spec = launch_options(agent, argv)
    record = {**spec, 'agent': agent, 'cwd': str(Path(cwd).resolve()), 'workspace': workspace(cwd),
              'boot': boot(), 'generation': token, 'phase': 'unknown', 'open': True,
              'paused': False, 'updated': time.time(), 'supervisor_pid': os.getpid()}
    record['supervisor_start'] = helper('t_recovery')['process'](os.getpid()).get('start', '')
    if os.environ.get('TMUX_PANE') and os.environ.get('TMUX'):
        pane = run(['tmux', 'display-message', '-p', '-t', os.environ['TMUX_PANE'],
                    '#{session_name}|#{pane_id}|#{pane_pid}|#{session_path}'])
        fields = pane.stdout.rstrip('\n').split('|', 3)
        if not pane.returncode and len(fields) == 4 and os.path.realpath(fields[3]) == record['cwd']:
            record.update(dict(zip(('session', 'pane', 'pane_pid', 'cwd'), fields)),
                          socket=os.environ['TMUX'].rsplit(',', 2)[0])
    def change(data):
        restoring = os.environ.get('T_RECOVERY_RESTORE_GENERATION')
        if restoring:
            old = data['sessions'].get(key(agent, spec['sid']), {})
            if (not enabled() or old.get('generation', '') != restoring or not old.get('open') or old.get('paused')):
                raise ValueError('recovery intent changed before native launch')
        data['launches'][token] = record
        if spec['sid'] and SID.fullmatch(spec['sid']):
            old = data['sessions'].get(key(agent, spec['sid']))
            if old:
                pending = old.get('submission', {})
                if pending.get('state') == 'queued' and any(pending['marker'] in value for value in argv):
                    pending['state'] = 'prepared'
                old.update(record, paused=old.get('paused', False), restoring=False)
    update(change)
    return token


def event(agent, payload, token=None):
    token = token or os.environ.get('T_RECOVERY_TOKEN', '')
    if not token or not enabled() or payload.get('agent_id') or payload.get('subagent_id'):
        return
    def change(data):
        launch = data['launches'].get(token)
        if not launch or launch['agent'] != agent:
            return
        sid = payload.get('conversation_id') or payload.get('session_id') or launch.get('sid')
        if not sid or not SID.fullmatch(sid):
            return
        identity = key(agent, sid)
        for other_key, other in data['sessions'].items():
            if other_key != identity and other.get('generation') == token:
                other['open'] = False
        prior = data['sessions'].get(identity, {})
        # A late event from the old runtime cannot overwrite its replacement.
        if prior.get('generation') not in (None, token) and prior.get('updated', 0) > launch['updated']:
            return
        row = data['sessions'].setdefault(identity, {})
        if row.get('generation') != token:
            row.update(launch, sid=sid, paused=prior.get('paused', False), restoring=False)
        launch['sid'] = sid
        row['updated'] = time.time()
        if payload.get('transcript_path'):
            row['transcript'] = payload['transcript_path']
        kind = payload.get('hook_event_name') or payload.get('event')
        phase = {
            'UserPromptSubmit': 'running', 'beforeSubmitPrompt': 'running', 'turn/started': 'running',
            'PermissionRequest': 'waiting', 'Elicitation': 'waiting', 'waiting': 'waiting',
            'PreToolUse': 'waiting', 'preToolUse': 'waiting',
            'PostToolUse': 'running', 'postToolUse': 'running', 'PostToolUseFailure': 'running',
            'postToolUseFailure': 'running', 'Stop': 'idle', 'completed': 'idle',
            'interrupted': 'interrupted', 'transport_failed': 'outage', 'failed': 'attention',
        }.get(kind)
        if kind in ('SessionStart', 'sessionStart'):
            row.setdefault('phase', 'unknown')
            row.update(next_retry=0, outcome='')
            if launch.get('socket') and launch.get('session'):
                for name, value in (('CLAUDE_RESUME_ID', sid), ('DEV_AGENT', agent)):
                    run(['tmux', '-S', launch['socket'], 'set-environment', '-t', '='+launch['session'], name, value])
        elif kind == 'stop':
            phase = {'completed': 'idle', 'aborted': 'interrupted', 'error': 'attention'}.get(payload.get('status'), 'unknown')
        elif kind == 'StopFailure':
            text = payload.get('last_assistant_message', '')
            phase = 'outage' if helper('t_recovery')['failure'](agent, text) else 'attention'
        elif kind in ('SessionEnd', 'sessionEnd'):
            if payload.get('reason') in ('prompt_input_exit', 'logout', 'user_close', 'window_close', 'clear'):
                row['open'] = False
            phase = {'completed': 'idle', 'aborted': 'interrupted'}.get(payload.get('reason'), 'unknown')
            if payload.get('reason') == 'error' and helper('t_recovery')['failure'](agent, payload.get('error_message', '')):
                phase = 'outage'
        if payload.get('is_interrupt') is True:
            phase = 'interrupted'
        if phase:
            row['phase'] = phase
            row['reason'] = payload.get('reason', '')[:500]
            if phase == 'idle':
                row['failures'] = 0
        if kind in ('UserPromptSubmit', 'beforeSubmitPrompt', 'turn/started'):
            row['turn'] = payload.get('turn_id') or payload.get('prompt_id') or uuid.uuid4().hex
            prompt = payload.get('prompt', '')
            if row.get('submission') and row['submission']['marker'] in prompt:
                row['submission']['state'] = 'acknowledged'
            elif kind != 'turn/started':
                row.pop('submission', None)
                row['attempts'] = 0
    update(change)


class CodexObserver:
    def __init__(self, token):
        self.token, self.sid, self.pending = token, '', {}
        self.transport_error = False

    def client(self, message):
        method, params = message.get('method'), message.get('params', {})
        if (method in ('thread/start', 'thread/resume') and not params.get('ephemeral')
                and params.get('threadSource') in (None, 'user')):
            self.pending[message['id']] = method
        if params.get('threadId') == self.sid and self.sid:
            if method == 'turn/interrupt':
                self.emit('interrupted')
            if method == 'turn/start':
                self.transport_error = False
                prompt = '\n'.join(p.get('text', '') for p in params.get('input', []) if p.get('type') == 'text')
                self.emit('UserPromptSubmit', prompt=prompt)

    def emit(self, kind, **values):
        if self.sid:
            event('codex', {'session_id': self.sid, 'event': kind, **values}, self.token)

    def server(self, message):
        if message.get('id') in self.pending and 'result' in message:
            self.pending.pop(message['id'])
            self.sid = message['result'].get('thread', {}).get('id', '')
            self.emit('SessionStart')
        params, method = message.get('params', {}), message.get('method', '')
        if not self.sid or params.get('threadId') != self.sid:
            return
        if method == 'turn/started':
            self.emit(method, turn_id=params.get('turn', {}).get('id')); self.emit('accepted')
        elif method == 'error':
            self.transport_error = helper('t_offline')['connection_failure'](params.get('error'))
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            status = turn.get('status', 'unknown')
            error = turn.get('error', {})
            phase = 'transport_failed' if status == 'failed' and helper('t_offline')['exhausted_connection'](error, self.transport_error) else status
            self.emit(phase)
        elif method == 'thread/status/changed':
            state = params.get('status', {})
            if any(flag in state.get('activeFlags', []) for flag in ('waitingOnApproval', 'waitingOnUserInput')):
                self.emit('waiting')
            elif state.get('type') == 'active':
                self.emit('PostToolUse')
        elif method.endswith(('/requestApproval', '/requestUserInput')):
            self.emit('waiting')


def hooks(enable):
    command = shlex.join([sys.executable, str(ROOT/'libexec/t_auto_recovery.py')])
    definitions = [
        (Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))/'settings.json', 'claude',
         ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure',
          'PermissionRequest', 'Elicitation', 'Stop', 'StopFailure', 'SessionEnd')),
        (Path.home()/'.cursor/hooks.json', 'cursor',
         ('sessionStart', 'beforeSubmitPrompt', 'preToolUse', 'postToolUse', 'postToolUseFailure', 'stop', 'sessionEnd'))]
    for path, agent, events in definitions:
        data = read(path)
        if not enable and not data:
            continue
        if agent == 'cursor':
            data.setdefault('version', 1)
        entries = data.setdefault('hooks', {})
        if not isinstance(entries, dict):
            raise ValueError('invalid hook configuration: ' + str(path))
        for name in events:
            rows = entries.get(name, [])
            if not isinstance(rows, list):
                raise ValueError('invalid hook list: ' + name)
            def owned(hook):
                if not isinstance(hook, dict):
                    return False
                try:
                    args = shlex.split(hook.get('command', ''))
                except ValueError:
                    return False
                return (len(args) == 5 and Path(args[1]).name == 't_auto_recovery.py'
                        and args[2:4] == ['event', agent])
            preserved = []
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get('hooks'), list):
                    remaining = [h for h in row['hooks'] if not owned(h)]
                    if remaining:
                        preserved.append({**row, 'hooks': remaining})
                elif not owned(row):
                    preserved.append(row)
            rows = preserved
            if enable:
                hook = {'command': command + ' event ' + agent + ' ' + name}
                if agent == 'claude':
                    hook.update(type='command', timeout=5)
                    hook = {'hooks': [hook]}
                rows.append(hook)
            if rows:
                entries[name] = rows
            else:
                entries.pop(name, None)
        write(path, data)


def service_path():
    return Path.home()/'Library/LaunchAgents'/ (LABEL + '.plist')


def service_app(enable):
    """Give the background item its own macOS identity instead of Python's."""
    app = root()/(DISPLAY_NAME + '.app')
    contents = app/'Contents'
    executable = contents/'MacOS/t-recovery'
    for path in (app, contents, executable.parent, executable, contents/'Info.plist'):
        if path.is_symlink():
            raise ValueError('refusing recovery application symlink: ' + str(path))
    if not enable:
        if app.exists():
            run([LSREGISTER, '-u', str(app)])
            shutil.rmtree(app)
        return executable
    executable.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = {'CFBundleIdentifier': LABEL, 'CFBundleName': DISPLAY_NAME,
            'CFBundleDisplayName': DISPLAY_NAME, 'CFBundleExecutable': 't-recovery',
            'CFBundlePackageType': 'APPL', 'CFBundleInfoDictionaryVersion': '6.0',
            'CFBundleVersion': '1', 'LSBackgroundOnly': True}
    (contents/'Info.plist').write_bytes(plistlib.dumps(info))
    executable.write_text('#!/bin/sh\nexec ' + shlex.join([
        sys.executable, str(ROOT/'libexec/t_auto_recovery.py'), 'watch']) + '\n')
    executable.chmod(0o700)
    result = run([LSREGISTER, '-f', str(app)], timeout=5)
    if result.returncode:
        raise ValueError('could not register t Recovery background identity: ' + result.stderr.strip())
    return executable


def activation_error():
    if sys.platform != 'darwin':
        return 'login recovery currently requires macOS'
    return ''


def setup_note():
    if (ROOT/'.git').is_file():
        return ('Run recovery from this worktree: ' + str(ROOT) +
                '. Keep it available while recovery is enabled; save Recovery from the installed version to switch back.')
    return ''


def configure(value, expected, *, enroll_live=True, launch=True):
    if read(settings_path()).get('enabled', False) != expected:
        raise ValueError('recovery configuration changed while editing; reopen t config')
    if value and activation_error():
        raise ValueError(activation_error())
    target = f'gui/{os.getuid()}/{LABEL}'
    if not value:
        write(settings_path(), {'enabled': False})
        run(['launchctl', 'bootout', target])
        service_path().unlink(missing_ok=True)
        hooks(False)
        service_app(False)
        return
    executable = service_app(True)
    hooks(True)
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {k: v for k, v in os.environ.items() if k in
                   ('HOME', 'PATH', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'T_LOCAL_RC')}
    environment['PATH'] = str(ROOT/'bin') + os.pathsep + os.environ.get('PATH', os.defpath)
    # Invoke the selected worker directly: the shared t selector must not redirect
    # a worktree preview to canonical code when launchd starts from HOME.
    document = {'Label': LABEL, 'ProgramArguments': [str(executable)],
                'AssociatedBundleIdentifiers': [LABEL],
                'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
                'EnvironmentVariables': environment, 'WorkingDirectory': str(Path.home()),
                'StandardErrorPath': str(root()/'service.log'), 'StandardOutPath': '/dev/null'}
    path = service_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('refusing recovery LaunchAgent symlink')
    path.write_bytes(plistlib.dumps(document)); path.chmod(0o600)
    if enroll_live:
        enroll(snapshot())
    write(settings_path(), {'enabled': True, 'source': str(ROOT)})
    run(['launchctl', 'bootout', target], timeout=5)
    if not launch:
        return
    result = run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], timeout=5)
    if result.returncode:
        raise ValueError('Recovery saved as enabled but service did not start; log in and save Recovery again. ' + result.stderr.strip())


def migrate():
    """Repoint an enabled service after installation without resetting its journal."""
    if sys.platform != 'darwin' or (ROOT/'.git').is_file() or not enabled():
        return 0
    current = read(settings_path())
    heartbeat = read(root()/'heartbeat.json')
    digest = source_digest()
    if (current.get('source') == str(ROOT) and heartbeat.get('source') == str(ROOT)
            and heartbeat.get('digest') == digest):
        if time.time() - heartbeat.get('at', 0) < 45:
            return 0
    target = f'gui/{os.getuid()}'
    files = [settings_path(), service_path(),
             root()/(DISPLAY_NAME + '.app')/'Contents/MacOS/t-recovery',
             root()/(DISPLAY_NAME + '.app')/'Contents/Info.plist',
             Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home()/'.claude'))/'settings.json',
             Path.home()/'.cursor/hooks.json']
    if any(path.is_symlink() for path in files):
        raise ValueError('recovery migration refuses a symlinked configuration file')
    backups = [(path, path.read_bytes(), path.stat().st_mode & 0o777)
               for path in files if path.exists()]
    started = time.time()
    try:
        # The worker reconciles live owners after it starts. Waiting on process
        # inventory here could exhaust the outer updater's migration deadline.
        if manually_stopped():
            configure(True, True, enroll_live=False, launch=False)
            return 0
        configure(True, True, enroll_live=False)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            heartbeat = read(root()/'heartbeat.json')
            if (heartbeat.get('source') == str(ROOT) and heartbeat.get('at', 0) >= started
                    and heartbeat.get('digest') == digest):
                return 0
            time.sleep(.2)
        raise ValueError('installed recovery worker did not confirm startup')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        run(['launchctl', 'bootout', target + '/' + LABEL], timeout=5)
        preserved = {p for p, _, _ in backups}
        for path in files:
            if path not in preserved:
                path.unlink(missing_ok=True)
        for path, content, mode in backups:
            fd, temp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    os.fchmod(stream.fileno(), mode)
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)
        if service_path() in preserved:
            result = run(['launchctl', 'bootstrap', target, str(service_path())], timeout=5)
            if result.returncode:
                raise ValueError('recovery migration failed and previous launch registration could not be restarted') from error
        raise ValueError('recovery migration rolled back: ' + str(error)) from error


def status():
    data = read(root()/'journal.json')
    heartbeat = read(root()/'heartbeat.json')
    source = read(settings_path()).get('source')
    return {**({'source': source} if source else {}),
            'enabled': enabled(), 'running': bool(enabled() and not manually_stopped() and heartbeat.get('at', 0) > time.time()-45),
            'sessions': list(data.get('sessions', {}).values())}


def detail(agent, sid):
    row = read(root()/'journal.json').get('sessions', {}).get(agent + ':' + sid)
    if not row:
        return None
    return {k: row.get(k) for k in ('phase', 'paused', 'open', 'reason', 'next_retry')}


def menu(ui, value):
    while True:
        state = status()
        label = 'Enabled' if value else 'Disabled'
        if value != state['enabled']:
            label += ' · starts on save' if value else ' · stops on save'
        elif value:
            label += ' · running' if state['running'] else ' · not running'
        picked = ui.pick('Recovery · this Mac',
            [('toggle', 'Automatic recovery'), ('back', 'Back')],
            values={'toggle': label},
            default=0)
        if picked != 'toggle':
            return value
        value = not value


def pause(identity, value):
    def change(data):
        row = data['sessions'].get(identity)
        if not row:
            raise ValueError('session is not enrolled in recovery')
        row.update(paused=value, attempts=0, failures=0, next_retry=0, reason='Paused by user' if value else '')
        if not value and row['phase'] == 'attention' and row.get('submission', {}).get('state') != 'prepared':
            row['phase'] = 'unknown'
    update(change)


def retire(agent, sid):
    if not sid or sid == '-' or agent not in AGENTS or not (root()/'journal.json').exists():
        return
    def change(data):
        row = data['sessions'].get(key(agent, sid))
        if row:
            updated = time.time()
            receipt = dict(agent=agent, sid=sid, generation=row.get('generation'),
                           updated=updated, reason=row.get('reason', '')) if row.get('open') else None
            row.update(open=False, reason='Explicit close or transfer', updated=updated)
            return receipt
    return update(change)


def restore_close(receipt):
    """Restore enrollment only if this exact provisional retirement is current."""
    def change(data):
        row = data['sessions'].get(key(receipt['agent'], receipt['sid']))
        if (row and row.get('open') is False and row.get('generation') == receipt['generation']
                and row.get('updated') == receipt['updated'] and row.get('reason') == 'Explicit close or transfer'):
            row.update(open=True, reason=receipt['reason'], updated=time.time())
    update(change)


def request_pass(timeout=30):
    """Ask the lock-owning worker for one fresh pass, without restarting it."""
    if not enabled():
        raise ValueError('Recovery disabled. Enable it in t config.')
    heartbeat = read(root()/'heartbeat.json')
    instance = heartbeat.get('instance')
    if not status()['running']:
        raise ValueError('Recovery agent unavailable. Run t agent background start recovery.')
    if not instance:
        raise ValueError('Recovery agent update required. Save Automatic recovery in t config.')
    request_id = uuid.uuid4().hex
    requests = root()/'requests'
    request = requests/(request_id+'.json')
    receipt = requests/(request_id+'.done')
    write(request, {'instance': instance, 'at': time.time()})
    deadline = time.monotonic()+timeout
    try:
        while time.monotonic() < deadline:
            if receipt.exists():
                result = read(receipt)
                if result.get('error'):
                    raise ValueError(result['error'])
                return result['sessions']
            if not enabled() or manually_stopped():
                raise ValueError('Recovery agent stopped before completing the check.')
            current = read(root()/'heartbeat.json')
            if current.get('instance') != instance:
                raise ValueError('Recovery agent changed. Run the check again.')
            time.sleep(.1)
        raise ValueError('Recovery check timed out. Run t system recovery status.')
    finally:
        request.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)


def requested_pass(engine, instance):
    requests = root()/'requests'
    pending = []
    for path in sorted(requests.glob('*.json')):
        value = read(path)
        if value.get('instance') == instance and 0 <= time.time()-value.get('at', 0) < 30:
            pending.append(path)
        else:
            path.unlink(missing_ok=True)
    if not pending:
        return False
    try:
        engine.tick(force=True)
        result = {'sessions': [row for row in status()['sessions'] if row.get('open')]}
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {'error': display_text(str(exc))}
    for path in pending:
        if path.exists():
            write(path.with_suffix('.done'), result)
            path.unlink(missing_ok=True)
    for path in requests.glob('*.done'):
        if time.time()-path.stat().st_mtime > 60:
            path.unlink(missing_ok=True)
    return True


def command(args, cfg=None, api=None):
    if args.scope == 'system' and args.action == 'agent':
        print('t agent background status\n'
              't agent background install recovery\n'
              't agent background start recovery\n'
              't agent background stop recovery\n'
              't agent background restart recovery')
        return 0
    if args.scope == 'system' and args.action == 'run':
        if not getattr(args, 'all', False):
            return helper('t_recovery_picker')['command'](cfg, api)
        rows = request_pass()
        if not rows:
            print('No sessions to recover.')
        for row in rows:
            name = row.get('session', '').removeprefix('dev-') or row['sid']
            reason = 'Paused' if row.get('paused') else display_text(row.get('reason')) or 'Status unknown'
            print(name + '  ' + reason)
        return 0
    state = status()
    rows = state['sessions']
    if args.scope == 'system' and not getattr(args, 'json', False):
        print('Automatic recovery  ' + ('Enabled' if state['enabled'] else 'Disabled'))
        print('Background agent    ' + ('Running' if state['running'] else 'Stopped'))
        return 0
    if args.scope == 'session':
        targets = [getattr(args, 'repo', None), getattr(args, 'slot', None)]
        if cfg and targets[0] and targets[0].isdigit() and not targets[1]:
            targets = [cfg.repo_of_dir(str(Path.cwd()))[0], targets[0]]
        if cfg and targets[0] and targets[1]:
            targets[0] = cfg.resolve_repo(targets[0], required=True)
        targets = [v for v in targets if v]
        selected = '-'.join(targets)
        rows = [r for r in rows if (selected and selected in (r['sid'], r.get('session', '').removeprefix('dev-'))) or
                (not selected and r.get('cwd') and Path.cwd().is_relative_to(Path(r['cwd'])))]
        if len(rows) > 1:
            current = [row for row in rows if row.get('open') and row.get('boot') == boot()]
            if len(current) == 1:
                rows = current
        if len(rows) != 1:
            raise ValueError('specify one enrolled session: t session recovery ' + args.action + ' REPO SLOT (or conversation ID)')
        if args.action != 'status':
            pause(key(rows[0]['agent'], rows[0]['sid']), args.action == 'pause')
            print('Recovery ' + ('paused' if args.action == 'pause' else 'eligible') + ' for ' + rows[0]['sid'])
            return 0
        state['sessions'] = rows
    panes = holding_panes() if rows and state['enabled'] else []
    def outcome(row):
        if not row.get('open'):
            return 'closed'
        if row.get('paused'):
            return 'paused'
        if find_holding(row, panes):
            return 'holding'
        if row.get('phase') == 'attention' or row.get('outcome') == 'blocked':
            return 'blocked'
        return 'ready' if row.get('safe') else 'holding required'
    if getattr(args, 'json', False):
        # Public status deliberately excludes command lines, drafts and transcript paths.
        print(json.dumps({**state, 'sessions': [{**{k: r.get(k) for k in
            ('agent', 'sid', 'session', 'cwd', 'phase', 'paused', 'open', 'reason', 'next_retry')},
            'outcome': outcome(r)} for r in rows]}))
    else:
        print('Automatic recovery  ' + ('Enabled' if state['enabled'] else 'Disabled'))
        print('Background agent    ' + ('Running' if state['running'] else 'Stopped'))
        for row in sorted(rows, key=lambda row: (not row.get('open'), row.get('session', ''))):
            slot = row.get('session', '').removeprefix('dev-') or row['sid']
            label = f"{slot}  {outcome(row)}  {row['agent']}  {row['sid']}"
            reason = display_text(row.get('reason'))
            print('  ' + label + ('  ' + reason if reason else ''))
        if not rows:
            print('  No enrolled sessions.')
    return 0


def resume_argv(row, prompt=''):
    executable = row.get('executable') or {'cursor': 'cursor-agent'}.get(row['agent'], row['agent'])
    options = row.get('options', [])
    if row['agent'] == 'codex':
        args = [executable, 'resume', row['sid'], *options]
        if '--no-daemon' not in args:
            args.append('--no-daemon')
    else:
        args = [executable, '--resume', row['sid'], *options]
    return args + (['--', prompt] if prompt else [])


def continuation(marker):
    return (f'[t recovery {marker}] This task was interrupted by a reboot or lost connection. '
            'Continue the previously authorized unfinished work in this conversation. Inspect current files and '
            'tool results first; preserve edits. Verify the outcome of any uncertain external action before '
            'repeating it. Do not override a user stop, pending approval, question, or completed task.')


def transcript_state(row):
    """Reconcile only native lifecycle records; conversation text is never a state signal."""
    path = row.get('transcript')
    if not path:
        return None
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('saved transcript is missing or unsafe')
    # A bounded tail is enough for terminal events. An incomplete tail is unknown.
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size-LIMIT))
        if size > LIMIT:
            stream.readline()
        lines = stream.read().decode('utf-8').splitlines()
    phase = None
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if row['agent'] == 'codex' and item.get('type') == 'event_msg':
            phase = {'task_started': 'running', 'task_complete': 'idle', 'turn_aborted': 'interrupted'}.get(
                item.get('payload', {}).get('type'), phase)
        elif row['agent'] == 'claude':
            message = item.get('message', {})
            if item.get('type') == 'system' and item.get('subtype') == 'turn_duration':
                phase = 'idle'
            if item.get('type') == 'assistant' and message.get('stop_reason') == 'end_turn':
                phase = 'idle'
            if item.get('type') == 'user' and (isinstance(message.get('content'), str) or
                    any(c.get('type') == 'text' for c in message.get('content', []) if isinstance(c, dict))):
                phase = 'running'
                content = message.get('content')
                text = content if isinstance(content, str) else '\n'.join(
                    part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text')
                # Native cancellation sentinel: this can only suppress recovery,
                # never authorize it. Claude's Stop hook does not cover Esc.
                if text.strip() in ('[Request interrupted by user]', '[Request interrupted by user for tool use]'):
                    phase = 'interrupted'
    return phase


class Runtime:
    def __init__(self):
        self.inventory = None
        self.observed = 0
        self.owners = []

    def live(self):
        result = run(['tmux', 'list-panes', '-a', '-F',
                      '#{session_name}|#{pane_id}|#{pane_pid}|#{pane_dead}|#{pane_current_command}'])
        inventory = (result.returncode, result.stdout)
        now = time.monotonic()
        if inventory != self.inventory or now - self.observed >= 60:
            self.owners = snapshot()
            self.inventory, self.observed = inventory, now
        return self.owners

    def available(self, row):
        host = {'codex': 'api.openai.com', 'claude': 'api.anthropic.com', 'cursor': 'api2.cursor.sh'}[row['agent']]
        try:
            with socket.create_connection((host, 443), timeout=3):
                return True
        except OSError:
            return False

    def stop(self, row, owner):
        recovery = helper('t_recovery')
        if not row.get('generation') or not recovery['current'](owner):
            raise ValueError('the failed client is no longer a verified recovery-owned runtime')
        info = recovery['process'](row.get('supervisor_pid', ''))
        if not info or info.get('start') != row.get('supervisor_start'):
            raise ValueError('the failed runtime supervisor cannot be verified')
        captured = recovery['tmux'](owner['socket'], 'capture-pane', '-p', '-J', '-S', '-', '-t', owner['pane'])
        if captured.returncode:
            raise ValueError('cannot preserve the visible draft')
        write(root()/('draft-'+row['generation']+'.json'), {'screen': captured.stdout})
        if not recovery['current'](owner):
            raise ValueError('client changed before recovery')
        latest = read(root()/'journal.json').get('sessions', {}).get(key(row['agent'], row['sid']), {})
        if (not enabled() or not latest.get('open') or latest.get('paused') or
                latest.get('phase') != 'outage' or latest.get('turn') != row.get('turn')):
            raise ValueError('session state changed before recovery')
        os.kill(int(owner['pid']), signal.SIGTERM)
        deadline = time.monotonic()+15
        while recovery['process'](row['supervisor_pid']).get('start') == row['supervisor_start']:
            if time.monotonic() >= deadline:
                raise ValueError('old runtime has not stopped; no replacement launched')
            time.sleep(.1)
        remaining = [p for p in recovery['panes'](owner['socket']) if p['session'] == owner['session']]
        if remaining:
            if len(remaining) != 1 or remaining[0]['pane_pid'] != owner['pane_pid'] or recovery['owner'](owner['socket'], remaining[0]).get('pid'):
                raise ValueError('pane changed while stopping; no replacement launched')
            result = recovery['tmux'](owner['socket'], 'kill-session', '-t', '='+owner['session'])
            if result.returncode:
                raise ValueError('could not release the stopped pane')

    def shell(self, row, task, launch, *extra):
        session = row.get('session') or ('t-cursor-'+row['sid'] if row['agent'] == 'cursor' else '')
        # A holding pane runs in the recovered workspace. Pin this invocation to
        # the worker's source so that workspace's older t checkout cannot replace
        # the shell helpers used by this worker.
        snippet = ('export T_DEV_CONTEXT=' + shlex.quote(str(ROOT)) + ' T_DEV_CONTEXT_OWNER=$$; '
                   'export PATH=' + shlex.quote(str(ROOT/'bin')) + ':"$PATH"; rehash; '
                   'source ' + shlex.quote(str(ROOT/'t.plugin.zsh')) + ' || exit $?; '
                   '[[ "$T_HOME" == ' + shlex.quote(str(ROOT)) + ' ]] || { '
                   'print -u2 "Recovery source changed while loading shell helpers"; exit 1; }; '
                   '_T_RESUME_ALLOW_UNAVAILABLE=0 _dev_with_launch_locks ' +
                   shlex.join([row['cwd'], row['sid'], task, session, row['cwd'],
                               row['sid'], row['agent'], launch, *extra]))
        # Login startup can source t from the pane's worktree. Keep the helper
        # selection tied to this controller for the lifetime of its child shell.
        env = {**os.environ, 'T_DEV_CONTEXT': str(ROOT), 'T_DEV_CONTEXT_OWNER': str(os.getpid())}
        result = run(['zsh', '-lic', snippet], env=env, timeout=120)
        if result.returncode:
            raise ValueError(result.stderr.strip()[-1500:] or 'guarded session restore failed')

    def hold(self, row):
        if not row.get('session', '').startswith('dev-'):
            raise ValueError('original numbered slot could not be verified')
        command = shlex.join([sys.executable, str(ROOT/'libexec/t_auto_recovery.py'),
                              'holding', row['agent'], row['sid'], row['generation']])
        self.shell(row, '_t_auto_hold_claimed', command)

    def restore(self, row, prompt, pane=None):
        if not workspace_matches(row):
            raise ValueError('workspace identity changed; restore manually')
        if row.get('safe') is False:
            raise ValueError('launch settings cannot be reproduced safely; restore manually')
        argv = resume_argv(row, prompt)
        launch = shlex.join(['env', 'T_RECOVERY_RESTORE_GENERATION='+row.get('generation', ''),
                            sys.executable, str(ROOT/'libexec/t_auto_recovery.py'), 'launch', row['agent'], *argv])
        if pane:
            self.shell(row, '_t_auto_resume_holding_claimed', launch, *pane)
        else:
            self.shell(row, '_t_auto_restore_claimed', launch)


class Engine:
    def __init__(self, runtime=None):
        self.runtime = runtime or Runtime()

    def save(self, identity, generation, **values):
        def change(data):
            row = data['sessions'].get(identity)
            if row and row.get('generation') == generation:
                row.update(values)
        update(change)

    def tick(self, force=False):
        if not enabled():
            return
        if force:
            self.runtime.inventory = None
        owners = self.runtime.live()
        enroll(owners)
        current_boot = boot()
        for identity, row in read(root()/'journal.json').get('sessions', {}).items():
            if not row.get('open') or row.get('paused'):
                continue
            matches = [o for o in owners if o['sid'] == row['sid'] and o['agent'] == row['agent']]
            if len(matches) > 1:
                self.save(identity, row.get('generation'), phase='attention', reason='Multiple live owners')
                continue
            owner = matches[0] if matches else None
            if owner:
                self.save(identity, row.get('generation'), **{k: owner[k] for k in
                    ('session', 'socket', 'pane', 'pane_pid', 'pid', 'start') if k in owner})
                if row['phase'] != 'outage':
                    continue
            # A supervisor can be between launch and SessionStart. Do not race it.
            elif row.get('boot') == current_boot and row.get('supervisor_pid'):
                info = helper('t_recovery')['process'](row['supervisor_pid'])
                if info.get('start') and info['start'] == row.get('supervisor_start'):
                    continue
            if row.get('next_retry', 0) > time.time() and not (
                    force and row.get('reason') == 'Waiting for connection'):
                continue
            with locked('session-'+hashlib.sha256(identity.encode()).hexdigest()+'.lock'):
                latest = read(root()/'journal.json')['sessions'][identity]
                if latest.get('generation') != row.get('generation') or latest.get('paused') or not latest.get('open'):
                    continue
                if owner and latest.get('phase') != 'outage':
                    continue
                if not owner and latest.get('safe') is False and not latest.get('submission'):
                    if latest.get('argv_verified') or latest.get('supervisor_pid'):
                        self.hold(identity, latest)
                    continue
                if latest.get('phase') == 'attention':
                    continue
                self.recover(identity, latest, owner, current_boot)

    def hold(self, identity, row):
        generation = row['generation']
        try:
            if not workspace_matches(row):
                raise ValueError('workspace is missing or has been replaced')
            if not find_holding(row):
                self.runtime.hold(row)
            self.save(identity, generation, outcome='holding', reason='Open pane')
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.save(identity, generation, outcome='blocked', reason=str(exc)[:500],
                      next_retry=time.time()+300)

    def recover(self, identity, row, owner, current_boot):
        generation = row.get('generation')
        try:
            if not workspace_matches(row):
                raise ValueError('workspace is missing or has been replaced')
            phase = transcript_state(row)
            if phase in ('idle', 'interrupted'):
                row['phase'] = phase
            submission = row.get('submission', {})
            if submission.get('state') == 'prepared':
                raise ValueError('continuation delivery is uncertain; inspect the saved conversation before retrying')
            continuing = row['phase'] in ('running', 'outage') and submission.get('state') != 'acknowledged'
            if continuing and not self.runtime.available(row):
                attempts = row.get('attempts', 0)+1
                self.save(identity, generation, attempts=attempts, next_retry=time.time()+retry_delay(attempts), reason='Waiting for connection')
                return
            if owner and not continuing:
                return
            marker = uuid.uuid4().hex
            # Write before launching. A crash before acknowledgement must never cause a replay.
            self.save(identity, generation, restoring=True, submission={'marker': marker, 'state': 'queued'} if continuing else submission,
                      attempts=row.get('attempts', 0)+1, next_retry=time.time()+30, reason='Restoring session')
            if owner:
                self.runtime.stop(row, owner)
            self.runtime.restore(row, continuation(marker) if continuing else '')
            self.save(identity, generation, boot=current_boot, phase='unknown', restoring=False,
                      reason='Restored; waiting for native lifecycle confirmation')
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            latest = read(root()/'journal.json').get('sessions', {}).get(identity, {})
            if latest.get('turn') != row.get('turn') or latest.get('phase') != row['phase']:
                return  # A user action or native completion supersedes this attempt.
            failures = row.get('failures', 0)+1
            self.save(identity, generation, failures=failures, restoring=False,
                      phase='attention' if failures >= 3 or 'uncertain' in str(exc) else row['phase'],
                      next_retry=time.time()+retry_delay(failures), reason=str(exc)[:1500])


def source_digest():
    return hashlib.sha256((ROOT/'libexec/t_auto_recovery.py').read_bytes()).hexdigest()


def stop_marker():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/background/recovery.stopped'


def manually_stopped():
    return stop_marker().is_file()


def watch():
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root()/'service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stop_marker().unlink(missing_ok=True)
        engine = Engine()
        instance = uuid.uuid4().hex
        stopped = threading.Event()
        digest = source_digest()
        def heartbeat():
            while not stopped.is_set():
                write(root()/'heartbeat.json', {'at': time.time(), 'pid': os.getpid(), 'instance': instance,
                                                'source': str(ROOT), 'digest': digest})
                stopped.wait(10)
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while enabled():
                try:
                    if not requested_pass(engine, instance):
                        engine.tick()
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    write(root()/'error.json', {'at': time.time(), 'reason': str(exc)[:1500]})
                time.sleep(5)
        finally:
            stopped.set()
            thread.join()
    return 0


def launch(agent, argv, chosen=None):
    if not enabled():
        if os.environ.get('T_RECOVERY_RESTORE_GENERATION'):
            raise ValueError('automatic recovery was disabled before launch')
        return subprocess.call(argv)
    restoring = bool(os.environ.get('T_RECOVERY_RESTORE_GENERATION'))
    if agent == 'codex' and chosen is None:
        try:
            helper('t_offline')['launch_options'](argv, os.getcwd(), recovery=True)
        except ValueError:
            if restoring:
                raise
            # Unsupported native modes remain usable; do not enroll an invocation
            # whose approval/provider configuration cannot be reproduced.
            print('t recovery: this invocation requires manual recovery.', file=sys.stderr)
            return subprocess.call(argv)
    token = begin(agent, argv, os.getcwd())
    os.environ.pop('T_RECOVERY_RESTORE_GENERATION', None)
    os.environ['T_RECOVERY_TOKEN'] = token
    os.environ['T_RECOVERY_AGENT'] = agent
    try:
        if agent == 'codex':
            result = helper('t_offline')['supervise'](argv, chosen, os.getcwd(), lifecycle_observer=CodexObserver(token))
        else:
            result = subprocess.call(argv)
    except KeyboardInterrupt:
        result = 130
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('t recovery: ' + display_text(error), file=sys.stderr)
        result = 1
    def finish(data):
        record = data['launches'].pop(token, {})
        row = data['sessions'].get(agent + ':' + record.get('sid', ''))
        if row and row.get('generation') == token:
            if result in (0, 130) and not row.get('restoring'):
                row.update(open=False, phase='interrupted' if result == 130 else 'idle')
            elif restoring and result and row.get('phase') not in ('outage', 'attention'):
                attempts = row.get('failures', 0) + 1
                row.update(failures=attempts, next_retry=time.time() + retry_delay(attempts),
                           phase='attention' if attempts >= 3 else 'unknown',
                           reason=f'Agent exited with status {result} during recovery')
            row['exit_code'] = result
    update(finish)
    return result


def prefix(agent):
    return [sys.executable, str(ROOT/'libexec/t_auto_recovery.py'), 'launch', agent] if enabled() else []


def guard(agent, sid, cwd):
    row = read(root()/'journal.json').get('sessions', {}).get(key(agent, sid))
    if not enabled() or not row or not row.get('open') or row.get('paused'):
        raise ValueError('session recovery is disabled or paused')
    if not workspace_matches(row, cwd):
        raise ValueError('workspace changed before recovery')
    if agent == 'codex':
        path = row.get('transcript')
        if not path:
            cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home()/'.cache'))/'claude-sessions/rollouts'/sid
            path = cache.read_text().strip()
        helper('t_codex_ownership')['assert_released']('Codex', sid, cwd, run,
            codex_home=str(Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))), rollout=path)
    if agent == 'cursor':
        recovery = helper('t_recovery')
        processes = run(['ps', '-Aww', '-o', 'pid=,args='])
        def native(line):
            parts = line.split(None, 2)
            if len(parts) < 2 or Path(parts[1]).name in ('zsh', 'bash', 'sh', 'python', 'python3'):
                return False
            return recovery['cursor_process']({'args': ' '.join(parts[1:])})
        if processes.returncode or any(native(line) for line in processes.stdout.splitlines()):
            raise ValueError('Cursor clients must release their conversations before automatic restore')


def identity(repo, slot):
    name = 'dev-' + repo + '-' + slot
    rows = [row for row in snapshot() if row['session'] == name]
    if len(rows) != 1:
        raise ValueError('the selected remote session is not a verified live owner')
    return {**{k: rows[0][k] for k in ('agent', 'sid', 'session', 'cwd')},
            'workspace': workspace(rows[0]['cwd']), 'socket': rows[0].get('socket', '')}


def fingerprint(row):
    # A reboot may change the tmux socket, but never the selected conversation
    # or workspace. Recheck this descriptor immediately before remote attach.
    descriptor = {k: v for k, v in row.items() if k != 'socket'}
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()


def ssh_probe(target, repo, slot):
    remote = 't __auto-recovery identity ' + shlex.join([repo, slot])
    return run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', target,
                'zsh -lic ' + shlex.quote(remote)], timeout=15)


def ssh_fatal(text):
    return any(value in text.lower() for value in
               ('permission denied', 'host key verification failed', 'remote host identification has changed',
                'too many authentication failures', 'no supported authentication methods'))


def ssh_once(target, expected, repo, slot):
    # Reserve 255 for OpenSSH transport failure, not a normal remote command exit.
    command = ('t __auto-recovery attach ' + shlex.join([expected['sid'], repo, slot, fingerprint(expected)]) +
               '; recovery_rc=$?; (( recovery_rc == 255 )) && recovery_rc=254; exit $recovery_rc')
    process = subprocess.Popen(['ssh', '-t', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                                target, 'zsh -lic ' + shlex.quote(command)], stderr=subprocess.PIPE)
    errors = bytearray()
    def drain():
        while True:
            chunk = process.stderr.read(1)
            if not chunk:
                break
            errors.extend(chunk)
            del errors[:-8192]
            sys.stderr.buffer.write(chunk); sys.stderr.buffer.flush()
    thread = threading.Thread(target=drain)
    thread.start()
    try:
        result = process.wait()
    except KeyboardInterrupt:
        process.terminate(); process.wait()
        result = 130
    finally:
        thread.join(); process.stderr.close()
    return result, errors.decode('utf-8', errors='replace')


def reconnect(target, repo, slot):
    result = ssh_probe(target, repo, slot)
    if result.returncode:
        raise ValueError('cannot pin the remote conversation for reconnect: ' + result.stderr.strip())
    expected = json.loads(result.stdout)
    attempt = 0
    while True:
        rc, diagnostic = ssh_once(target, expected, repo, slot)
        if rc != 255 or ssh_fatal(diagnostic) or not enabled():
            return rc
        while enabled():
            delay = retry_delay(attempt); attempt += 1
            print(f'\nConnection lost; retrying {target} in {int(delay)}s. Ctrl-C stops reconnecting.', file=sys.stderr)
            try:
                time.sleep(delay)
                probe = ssh_probe(target, repo, slot)
            except KeyboardInterrupt:
                return 130
            except subprocess.TimeoutExpired:
                continue
            if ssh_fatal(probe.stderr):
                print(probe.stderr, file=sys.stderr)
                return 255
            if probe.returncode:
                continue
            actual = json.loads(probe.stdout)
            if fingerprint(actual) != fingerprint(expected):
                raise ValueError('remote conversation or workspace changed; reconnect stopped')
            break
        else:
            return 255


def internal(args, api=None):
    if args == ['watch']:
        return watch()
    if args == ['migrate']:
        return migrate()
    if args == ['rows']:
        for row in holding_rows():
            print(row)
        return 0
    if len(args) == 3 and args[0] == 'identity':
        print(json.dumps(identity(*args[1:])))
        return 0
    if len(args) == 5 and args[0] == 'attach':
        row = identity(*args[2:4])
        if row['sid'] != args[1] or fingerprint(row) != args[4]:
            raise ValueError('remote slot belongs to another conversation')
        return subprocess.call(['tmux', *(['-S', row['socket']] if row.get('socket') else []),
                                'attach-session', '-t', '='+row['session']])
    raise ValueError('unknown internal recovery operation')


if __name__ == '__main__':
    try:
        action, *args = sys.argv[1:]
        if action == 'prefix':
            print('\n'.join(prefix(args[0])))
        elif action == 'watch':
            raise SystemExit(watch())
        elif action == 'launch':
            raise SystemExit(launch(args[0], args[1:]))
        elif action == 'event':
            payload = json.loads(sys.stdin.read(LIMIT))
            payload['hook_event_name'] = args[1]
            event(args[0], payload)
        elif action == 'register':
            event(args[0], {'session_id': args[1], 'cwd': args[2], 'transcript_path': args[3], 'event': 'SessionStart'})
        elif action == 'retire':
            retire(*args)
        elif action == 'prepare-close':
            receipt = retire(*args)
            if receipt is not None:
                print(json.dumps(receipt))
        elif action == 'restore-close':
            restore_close(json.loads(args[0]))
        elif action == 'guard':
            guard(*args)
        elif action == 'reconnect':
            raise SystemExit(reconnect(*args))
        elif action == 'holding':
            raise SystemExit(holding(*args))
        elif action == 'holding-rows':
            for row in holding_rows():
                print(row)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('t recovery: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
