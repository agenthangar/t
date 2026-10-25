"""Explicit, noninteractive session controls for the sessions MCP server."""
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import uuid
from contextlib import redirect_stdout, redirect_stderr
import io

ROOT = Path(__file__).resolve().parent.parent
READ_TOOLS = ('find_sessions', 'list_sessions', 'session_detail')
SESSION_VERBS = ('open', 'list', 'close', 'restart', 'push', 'pop', 'resume', 'history',
                 'cd', 'move', 'read', 'view-plan', 'paste', 'search', 'open-app', 'goal')
DESCRIPTIONS = {
    'open_session': 'Open or create an explicit local CLI slot, detached. Reuses t session open ownership and worktree checks. Does not open a terminal window.',
    'resume_session': 'Resume the newest saved conversation in a stopped local slot, detached. For a live or paused agent, use send_instructions to ask it to continue.',
    'kill_session': 'Close an explicit local slot using t session close safety checks; preserves history and worktree. Requires confirm=true. Refuses the calling agent.',
    'restart_session': 'Restart a stuck local CLI in the same conversation using t session restart recovery checks. Refuses the calling agent.',
    'send_instructions': 'Submit instructions to a verified local Claude or Codex tmux agent. Requires its full session_id. Reports terminal delivery, not agent acknowledgement; inspect the session afterwards. Uses the current composer, so inspect unsent drafts/dialogs first.',
    'pause_session': 'Request interruption of the current turn by sending Escape to a verified local Claude or Codex tmux agent. Keeps the session available; queued work may still run. Default keybindings required. Reports request delivery, not a confirmed paused state.',
    'set_session_goal': 'Submit a new objective as an instruction to a verified local Claude or Codex tmux agent. This is a conversational goal change, not a native goal API or process suspension. Does not interrupt first; use pause_session separately if needed.',
}


def tool_specs():
    specs = []
    for name, description in DESCRIPTIONS.items():
        props = {'repo': {'type': 'string', 'description': 'Registered repository alias on this machine'},
                 'slot': {'type': 'integer', 'minimum': 1}}
        required = ['repo', 'slot']
        if name in ('send_instructions', 'pause_session', 'set_session_goal'):
            props['session_id'] = {'type': 'string', 'description': 'Exact current conversation UUID; stale targets are refused'}
            required.append('session_id')
        if name in ('send_instructions', 'set_session_goal'):
            key = 'instructions' if name == 'send_instructions' else 'goal'
            props[key] = {'type': 'string', 'minLength': 1, 'maxLength': 32000}
            required.append(key)
        if name == 'open_session':
            props['agent'] = {'type': 'string', 'enum': ['claude', 'codex']}
        if name == 'kill_session':
            props['confirm'] = {'type': 'boolean', 'description': 'Explicit authorization to close this slot, including a live agent'}
            required.append('confirm')
        specs.append({'name': name, 'description': description,
                      'inputSchema': {'type': 'object', 'properties': props,
                                      'required': required, 'additionalProperties': False},
                      'annotations': {'readOnlyHint': False, 'destructiveHint': name in ('kill_session', 'restart_session'),
                                      'idempotentHint': False, 'openWorldHint': True}})
    specs.append({'name': 'session_command',
                  'description': 'Run any public t session command with its normal CLI arguments. Read/list/history modes return output; open/resume stay detached. Interactive-only modes return terminal_required and an exact command, without starting work. Use command=goal to change the current objective. Never interprets arguments as shell code.',
                  'inputSchema': {'type': 'object', 'properties': {
                      'command': {'type': 'string', 'enum': list(SESSION_VERBS)},
                      'arguments': {'type': 'array', 'items': {'type': 'string'}, 'default': []}},
                      'required': ['command'], 'additionalProperties': False},
                  'annotations': {'readOnlyHint': False, 'destructiveHint': True,
                                  'idempotentHint': False, 'openWorldHint': True}})
    return specs


def validate(cfg, name, args):
    schema = next(spec['inputSchema'] for spec in tool_specs() if spec['name'] == name)
    if not isinstance(args, dict) or set(args) - schema['properties'].keys() or set(schema['required']) - args.keys():
        raise ValueError('provide the required tool arguments; unknown arguments are not accepted')
    repo, slot = args['repo'], args['slot']
    if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', repo) or repo not in cfg.repos:
        raise ValueError('repo must name a registered repository')
    if type(slot) is not int or slot < 1:
        raise ValueError('slot must be a positive integer')
    if 'agent' in args and args['agent'] not in ('claude', 'codex'):
        raise ValueError('agent must be claude or codex')
    if name == 'kill_session' and args['confirm'] is not True:
        raise ValueError('closing a session requires confirm=true')
    if 'session_id' in args and (not isinstance(args['session_id'], str) or not re.fullmatch(
            r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', args['session_id'])):
        raise ValueError('session_id must be a complete conversation UUID')
    for key in ('instructions', 'goal'):
        if key in args:
            value = args[key]
            if (not isinstance(value, str) or not value.strip() or len(value) > 32000
                    or any(ord(c) < 32 and c not in '\n\t' or 127 <= ord(c) < 160 for c in value)):
                raise ValueError(key + ' must be nonempty text, at most 32000 characters, without terminal control characters')
    return repo, str(slot)


def checked(result):
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or 'session operation failed').strip())
    return result.stdout


def target_owner(session, expected, recovery):
    socket = checked(recovery['run'](['tmux', 'display-message', '-p', '-t', '=' + session + ':', '#{socket_path}'])).strip()
    if not socket.startswith('/'):
        raise ValueError('could not verify the tmux socket')
    rows = [p for p in recovery['panes'](socket) if p['session'] == session]
    target = recovery['owner'](socket, rows[0]) if len(rows) == 1 else {}
    if (not target or target.get('pane_dead') == '1' or target.get('agent') not in ('claude', 'codex')
            or target.get('sid') != expected):
        raise ValueError('target is not the expected live CLI conversation; desktop, foreground, and ambiguous panes are unsupported')
    if (expected in {os.environ.get(k) for k in ('CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID')}
            or recovery['descendant'](os.getpid(), target['pid'])):
        raise ValueError('cannot control the calling agent; choose another session')
    return target


def deliver(name, args, session, recovery):
    target = target_owner(session, args['session_id'], recovery)
    def send(*words):
        if not recovery['current'](target):
            raise ValueError('session owner changed; no further input was sent')
        return checked(recovery['tmux'](target['socket'], *words))
    if name == 'pause_session':
        send('send-keys', '-t', target['pane'], 'Escape')
        return {'status': 'interrupt_requested', 'session_id': target['sid'], 'acknowledged': False}
    message = ('New goal: ' + args['goal'] if name == 'set_session_goal'
               else 'Instruction: ' + args['instructions'])
    buffer = 't-instruction-' + uuid.uuid4().hex
    # Private tmux buffer, literal UTF-8 via stdin, bracketed paste; never a shell
    # command or send-keys key name. Do not overwrite the user's default buffer.
    checked(subprocess.run(['tmux', '-S', target['socket'], 'load-buffer', '-b', buffer, '-'],
                           input=message, text=True, capture_output=True, timeout=15))
    try:
        send('paste-buffer', '-p', '-r', '-d', '-b', buffer, '-t', target['pane'])
        send('send-keys', '-t', target['pane'], 'Enter')
    finally:
        recovery['tmux'](target['socket'], 'delete-buffer', '-b', buffer)
    return {'status': 'submitted', 'session_id': target['sid'], 'acknowledged': False}


def shell_run(words, *, report_cwd=False):
    snippet = ('setopt no_monitor no_notify; source ' + shlex.quote(str(ROOT / 't.plugin.zsh'))
               + ' || exit $?; unset T_MCP_DETACHED; typeset T_MCP_DETACHED=1; ' + shlex.join(words))
    if report_cwd:
        snippet += ' && pwd -P'
    return checked(subprocess.run(['zsh', '-lic', snippet], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, start_new_session=True))


def session_command(cfg, args, api):
    if (not isinstance(args, dict) or set(args) - {'command', 'arguments'}
            or args.get('command') not in SESSION_VERBS
            or not isinstance(args.get('arguments', []), list)
            or any(not isinstance(s, str) or '\x00' in s for s in args.get('arguments', []))):
        raise ValueError('provide a session command and an array of literal CLI arguments')
    verb, arguments = args['command'], args.get('arguments', [])
    argv = ['session', verb, *arguments]
    command = shlex.join(['t', *argv])
    # Validate through the public parser before dispatch; capture argparse output
    # so even invalid/help arguments cannot corrupt JSON-RPC stdout.
    output = io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            parsed = api['build_parser']().parse_args(api['_canonical_argv'](argv))
    except SystemExit as error:
        if error.code:
            raise ValueError(output.getvalue().strip()) from None
        return {'status': 'completed', 'output': output.getvalue()}
    if verb in ('push', 'pop', 'move', 'search', 'paste') or any(
            option in arguments for option in ('--fg', '-f', '--pick', '-p', '--host', '-r', '--remote')) and verb in ('open', 'resume', 'close'):
        return {'status': 'terminal_required', 'command': command,
                'reason': 'This mode needs terminal interaction or a foreground/remote handoff; no action was started.'}
    if verb in ('open', 'resume', 'close', 'restart'):
        if not getattr(parsed, 'repo', None) or not getattr(parsed, 'slot', None):
            raise ValueError('MCP session actions require an explicit repository and slot')
        if not str(parsed.slot).isdigit():
            raise ValueError('MCP session actions require one numeric slot')
        params = {'repo': parsed.repo, 'slot': int(parsed.slot)}
        name = {'open': 'open_session', 'resume': 'resume_session', 'close': 'kill_session', 'restart': 'restart_session'}[verb]
        if verb == 'close':
            params['confirm'] = parsed.yes
        if verb == 'open':
            if getattr(parsed, 'codex', False): params['agent'] = 'codex'
            if getattr(parsed, 'claude', False): params['agent'] = 'claude'
        # Preserve additional modes instead of silently dropping their flags.
        allowed = {'--cli', '--codex', '--claude'} if verb == 'open' else {'--cli'} if verb == 'resume' else {'-y', '--yes'} if verb == 'close' else set()
        if any(s.startswith('-') and s not in allowed for s in arguments):
            return {'status': 'terminal_required', 'command': command,
                    'reason': 'Run this conditional mode in a terminal; no action was started.'}
        return call(cfg, name, params, api)
    if verb == 'cd':
        # A subprocess cannot change the caller's directory. Return the resolved
        # path as data, using the same resolver as the shell entry point.
        if not parsed.repo or not parsed.slot:
            raise ValueError('MCP session cd requires an explicit repository and slot')
        path = shell_run(['_t_cd', *arguments], report_cwd=True).strip()
        return {'status': 'completed', 'cwd': path, 'command': command}
    if verb == 'list':
        scope, wt_scope = api['_ls_scope'](cfg, parsed.repo, parsed.all, os.getcwd())
        snippet = ['_dev_rows_all' if parsed.remote else '_dev_session_rows', '--details']
        rows = api['_parse_rows'](shell_run(snippet), host_prefixed=parsed.remote)
        return {'status': 'completed', 'sessions': api['_scope_filter'](rows, scope, wt_scope)}
    if verb in ('read', 'view-plan', 'open-app') and (not getattr(parsed, 'repo', None)
                                                  or not getattr(parsed, 'slot', None)):
        raise ValueError('MCP session actions require an explicit repository and slot')
    if verb == 'history' and '--json' not in arguments:
        argv.append('--json')
    if verb == 'read' and not any(s in arguments for s in ('--dump', '-d', '-n')):
        argv.append('--dump')
    result = subprocess.run([str(ROOT / 'bin/t'), *argv], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, start_new_session=True)
    return {'status': 'completed', 'output': checked(result), 'stderr': result.stderr}


def call(cfg, name, args, api=None):
    if name == 'session_command':
        return session_command(cfg, args, api)
    if name not in DESCRIPTIONS:
        raise ValueError('unknown session control: ' + str(name))
    repo, slot = validate(cfg, name, args)
    session = f'dev-{repo}-{slot}'
    if name in ('send_instructions', 'pause_session', 'set_session_goal'):
        recovery = runpy.run_path(str(ROOT / 'libexec/t_recovery.py'))
        result = deliver(name, args, session, recovery)
    else:
        words = {'open_session': ['_t_open'], 'resume_session': ['_t_resume'],
                 'kill_session': ['_t_dev', 'kill'],
                 'restart_session': [str(ROOT / 'bin/t'), 'session', 'restart']}[name] + [repo, slot]
        if name in ('open_session', 'resume_session'):
            words += ['--cli']
        if name == 'resume_session':
            words += ['--days', 'all']
        if name == 'open_session' and args.get('agent'):
            words += ['--' + args['agent']]
        if name == 'kill_session':
            words += ['-y']
        # Login shell contract + explicit standalone integration, no TTY, no
        # picker, no inherited stdout on the JSON-RPC wire. Keep caller identity
        # for existing self-kill/restart guards.
        output = shell_run(words)
        result = {'status': 'completed', 'output': output.strip(),
                  'open_command': f't session open {shlex.quote(repo)} {slot}'}
    return dict(result, repo=repo, slot=int(slot), host='local')
