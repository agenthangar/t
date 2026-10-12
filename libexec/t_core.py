"""Shared CLI/desktop operations. Shell adapters retain ownership and TTY contracts."""
import fcntl
from concurrent.futures import ThreadPoolExecutor
import runpy
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

PROTOCOL_VERSION = 1


def load_cli(root=None):
    path = Path(root or Path(__file__).resolve().parent.parent) / 'bin' / 't'
    loader = SourceFileLoader('t_shared_cli', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def dispatch(api, cfg, args):
    """The CLI and desktop use the same handler selection, without a second CLI."""
    handler = api['IMPLEMENTED'].get(args.verb)
    if handler:
        return handler(cfg, args)
    args._verb = args.verb
    return api['cmd_pending'](cfg, args)


def persist_config(api, cfg, before, initial):
    changes = api['_config_diff'](initial, api['_config_state'](cfg))
    if changes:
        api['_config_write'](api['_current_local_rc'](), before,
                             api['_config_text'](before, changes, initial))
        api['_host_refresh_config']()
    return changes


def restart_target(api, cfg, repo, slot, cwd):
    row = api['_slot_select'](cfg, api['_parse_rows'](api['zsh_capture']('_dev_session_rows')),
                              repo, slot, cwd, 'restart')
    if row['agent'] not in ('codex', 'claude') or not re.fullmatch(
            r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', row['sid']):
        raise ValueError('the slot has no verified conversation; check t doctor')
    if not os.path.isdir(row['cwd']):
        raise ValueError("the session's worktree is missing: " + row['cwd'])
    return row


class CoreError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Core:
    def __init__(self, cli=None, state_root=None):
        self.cli = cli or load_cli()
        self.api = vars(self.cli)
        self.root = Path(self.cli._t_source_root())
        self.state_root = Path(state_root or Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 't' / 'desktop-operations')
        self.index = {}
        self.progress = lambda operation_id, stage: None
        self.remote_status = []

    def config(self):
        self.cli._host_refresh_config()
        return self.cli.Config()

    def capabilities(self):
        typed = {'session.list': 'sessions.list', 'session.search': 'sessions.search',
                 'session.open': 'sessions.open', 'session.resume': 'sessions.resume',
                 'session.close': 'sessions.close', 'session.restart': 'sessions.restart',
                 'issue.list': 'issues.list', 'issue.status': 'issues.status',
                 'issue.start': 'issues.start', 'repo.list': 'repositories.list',
                 'hosts.list': 'hosts.list', 'config.show': 'config.get'}
        catalog = [{'method': group + '.' + action, 'command': [group, action],
                 'kind': 'core' if group + '.' + action in typed else 'terminal',
                 'coreMethod': typed.get(group + '.' + action),
                 'argumentSpecs': [], 'acceptsArguments': True}
                for group, actions in self.cli.COMMAND_GROUPS.items() for action in actions]
        if 'share' in self.cli.IMPLEMENTED:
            catalog.extend({'method': 'share.' + action, 'command': ['share'] + ([] if action == 'send' else [action]),
                            'kind': 'terminal', 'coreMethod': None, 'argumentSpecs': [], 'acceptsArguments': True}
                           for action in ('send', 'targets', 'inbox', 'read'))
        return catalog

    def shell(self, snippet, cwd=None):
        runtime_path = str(self.root / 'bin') + os.pathsep + str(Path(sys.executable).parent)
        fallback_path = os.pathsep.join(('/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local/bin'), str(Path.home() / 'bin')))
        prelude = ('setopt no_monitor no_notify; export T_NO_UPDATE_CHECK=1 T_NO_AUTORELOAD=1; '
                   'export PATH=' + shlex.quote(runtime_path) + ':$PATH:' + shlex.quote(fallback_path) + '; ')
        script = prelude + 'source ' + shlex.quote(str(self.root / 't.plugin.zsh')) + '; ' + prelude + snippet
        return ['zsh', '-lic', script]

    def attachment(self, session, cwd=None, host='local'):
        if not re.fullmatch(r'dev-[A-Za-z0-9_.-]+-[0-9]+', session):
            raise CoreError('invalid_target', 'select a numbered tmux session')
        cfg = self.config()
        identity, target = self._attachment_identity(cfg, session, host)
        expected = {'session': session, 'identity': identity, 'host': host, 'target': target}
        session_id = identity.split('\t')[1]
        query = shlex.join(['tmux', 'display-message', '-p', '-t', '=' + session + ':',
                            '#{pid}\t#{session_id}\t#{pane_id}\t#{pane_pid}\t#{session_path}'])
        guard = ('[[ "$(' + query + ')" == ' + shlex.quote(identity) + ' ]] || '
                 '{ print -u2 -- "t: attachment changed; refresh the selected session"; exit 1; }; '
                 'exec ' + shlex.join(['tmux', 'attach-session', '-t', session_id]))
        argv = self.shell(guard) if host == 'local' else ['ssh', '-t', target, 'zsh -lic ' + shlex.quote(guard)]
        return {'kind': 'terminal', 'argv': argv, 'cwd': cwd or str(Path.home()), 'env': {},
                'detachKeepsSession': True, 'validation': expected}

    def _attachment_identity(self, cfg, session, host):
        argv = ['tmux', 'display-message', '-p', '-t', '=' + session + ':',
                '#{pid}\t#{session_id}\t#{pane_id}\t#{pane_pid}\t#{session_path}']
        target = None
        if host != 'local':
            target = cfg.hosts.get(host)
            if not target or not self.cli._BARE_HOST.fullmatch(target) or target.startswith('-'):
                raise CoreError('invalid_target', 'select a registered SSH host')
            argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', target,
                    'zsh -lic ' + shlex.quote(shlex.join(argv))]
        result = self.cli._run(argv, timeout=10)
        identity = getattr(result, 'stdout', '').strip()
        if result.returncode or not re.fullmatch(r'[0-9]+\t\$[0-9]+\t%[0-9]+\t[0-9]+\t/[^\n]*', identity):
            raise CoreError('stale_attachment', 'session is unavailable; refresh the selected session')
        return identity, target

    def validate_attachment(self, expected):
        if not isinstance(expected, dict) or not re.fullmatch(r'dev-[A-Za-z0-9_.-]+-[0-9]+', expected.get('session', '')):
            raise CoreError('invalid_target', 'invalid attachment identity')
        identity, target = self._attachment_identity(self.config(), expected['session'], expected.get('host', 'local'))
        if identity != expected.get('identity') or target != expected.get('target'):
            raise CoreError('stale_attachment', 'attachment changed; refresh the selected session')
        return {'valid': True}

    def _record(self, record):
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state_root / (record['operationId'] + '.json')
        with tempfile.NamedTemporaryFile(mode='w', dir=self.state_root, delete=False) as stream:
            pending = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(pending, path)
            finally:
                pending.unlink(missing_ok=True)
        return record

    def _read(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', operation_id):
            raise CoreError('invalid_operation', 'invalid operation identifier')
        try:
            return json.loads((self.state_root / (operation_id + '.json')).read_text())
        except (OSError, ValueError):
            raise CoreError('unknown_operation', 'operation is unavailable') from None

    def capture_owner(self, cfg, arguments, expected=None):
        repo, slot, session = self._slot(cfg, arguments)
        host = arguments.get('host', 'local')
        identity, target = self._attachment_identity(cfg, session, host)
        query = shlex.join(['_dev_core_owner_metadata', session])
        argv = self.shell(query) if host == 'local' else ['ssh', '-o', 'BatchMode=yes', target,
                'zsh -lic ' + shlex.quote('source "${commands[t]:A:h:h}/t.plugin.zsh"; ' + query)]
        result = self.cli._run(argv, timeout=15)
        fields = getattr(result, 'stdout', '').strip('\n').split('\t')
        if result.returncode or len(fields) != 3:
            raise CoreError('invalid_target', 'could not verify the selected owner')
        sid, agent, client_pid = fields
        owner = {'session': session, 'identity': identity, 'host': host, 'target': target,
                 'sid': sid, 'agent': agent, 'clientPid': client_pid}
        if (arguments.get('sid') not in (None, '') and ('' if arguments['sid'] == '-' else arguments['sid']) != sid or
                expected and any(owner.get(key) != expected.get(key) for key in owner)):
            raise CoreError('stale_target', 'selected owner changed; refresh before reviewing the action')
        if self._attachment_identity(cfg, session, host)[0] != identity:
            raise CoreError('stale_target', 'selected owner changed during review')
        return owner

    def prepare(self, params):
        method = params.get('method')
        arguments = params.get('arguments', {})
        command = params.get('command')
        if command is not None:
            if (not isinstance(command, list) or not command or len(command) < 2 and command[0] != 'share' or
                    not all(isinstance(word, str) and '\x00' not in word for word in command) or
                    not (command[0] in self.cli.COMMAND_GROUPS and len(command) > 1 and command[1] in self.cli.COMMAND_GROUPS[command[0]]
                         or command in (['mcp', '--install'], ['mcp', '--list-tools'], ['mcp', '--reindex'])
                         or command[0] == 'share' and 'share' in self.cli.IMPLEMENTED)):
                raise CoreError('invalid_command', 'choose a supported command family and action')
            if command[0] == 'share':
                # Preserve the reviewed standalone exception and its actual parser.
                try:
                    self.cli.build_parser().parse_args(command)
                except SystemExit:
                    raise CoreError('invalid_command', 'invalid share command arguments') from None
            method = 'terminal'
            arguments = {'command': command, 'cwd': params.get('cwd')}
        if method not in ('sessions.open', 'sessions.resume', 'sessions.close', 'sessions.restart',
                          'hosts.change', 'config.set', 'repositories.register', 'issues.start', 'terminal'):
            raise CoreError('invalid_method', 'unsupported mutation')
        if not isinstance(arguments, dict):
            raise CoreError('invalid_params', 'arguments must be an object')
        arguments = dict(arguments)
        arguments.pop('_owner', None)
        owner = self.capture_owner(self.config(), arguments, params.get('expectedOwner')) if method in ('sessions.close', 'sessions.restart') else None
        record = {'operationId': str(uuid.uuid4()), 'method': method, 'arguments': arguments,
                  'state': 'prepared', 'summary': ' '.join(command) if command else method,
                  'command': command, 'fingerprint': hashlib.sha256(json.dumps([method, arguments], sort_keys=True).encode()).hexdigest()}
        if owner is not None:
            record['owner'] = owner
            if not arguments.get('sid'):
                arguments['sid'] = owner['sid'] or '-'
        if method == 'terminal':
            cwd = arguments.get('cwd') or str(Path.home())
            if not os.path.isdir(cwd):
                raise CoreError('invalid_target', 'working directory does not exist')
            snippet = 't ' + shlex.join(command)
            if command[:2] in (['repo', 'cd'], ['session', 'cd']):
                snippet += ' && exec zsh -l'
            record['attachment'] = {'kind': 'terminal', 'argv': self.shell(snippet),
                                    'cwd': cwd, 'env': {}, 'detachKeepsSession': True}
        return self._record(record)

    def _slot(self, cfg, args):
        repo, slot = args.get('repo'), str(args.get('slot', ''))
        if (repo not in cfg.repos and (args.get('host', 'local') == 'local' or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', repo or ''))) or not re.fullmatch(r'[0-9]+', slot):
            raise CoreError('invalid_target', 'select a registered repository and numbered slot')
        return repo, slot, 'dev-' + repo + '-' + slot

    def _run_shell(self, snippet, cwd=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
        env.pop('T_CORE_DETACH', None)
        env.pop('T_CORE_RESULT', None)
        with tempfile.TemporaryDirectory(prefix='t-core-') as directory:
            result_path = Path(directory) / 'result'
            controls = 'typeset T_CORE_DETACH=1 T_CORE_RESULT=' + shlex.quote(str(result_path)) + '; typeset +x T_CORE_DETACH T_CORE_RESULT; '
            result = subprocess.run(self.shell(controls + snippet), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, start_new_session=True, timeout=90)
            if result.returncode == 75:
                raise CoreError('stale_target', 'selected owner changed; refresh before reviewing the action')
            if result.returncode:
                raise CoreError('operation_failed', 'shared shell operation failed; inspect current state before retrying')
            outcome = {'exitCode': result.returncode}
            if result_path.exists():
                outcome['session'] = result_path.read_text().strip()
            return outcome

    def _remote_mutate(self, cfg, method, args):
        host = args['host']
        target = cfg.hosts.get(host)
        if not target or not self.cli._BARE_HOST.fullmatch(target) or target.startswith('-'):
            raise CoreError('invalid_target', 'select a registered SSH host')
        arguments = dict(args)
        arguments.pop('host')
        expected = arguments.pop('_owner', None)
        if expected:
            if expected.get('target') != target:
                raise CoreError('stale_target', 'SSH destination changed; refresh before reviewing the action')
            expected = dict(expected, host='local', target=None)
        # Execute the same installed core on the host, inside its login shell.
        # Arguments are JSON argv data; no renderer strings become Python code.
        program = ('import json,os,shutil,sys; '
                   'root=os.path.dirname(os.path.dirname(os.path.realpath(shutil.which("t")))); '
                   'sys.path.insert(0,os.path.join(root,"libexec")); '
                   'from t_core import Core; c=Core(); '
                   'r=c.prepare(dict(method=sys.argv[1],arguments=json.loads(sys.argv[2]),expectedOwner=json.loads(sys.argv[3]))); '
                   'print(json.dumps(c.apply(r["operationId"])))')
        remote = 'zsh -lic ' + shlex.quote(shlex.join(['python3', '-c', program, method, json.dumps(arguments), json.dumps(expected)]))
        result = self.cli._run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', target, remote], timeout=100)
        if result.returncode:
            raise CoreError('unknown_outcome', 'remote operation could not be verified; inspect the host before retrying')
        try:
            record = json.loads(result.stdout)
            outcome = record['result']
        except (ValueError, KeyError, TypeError):
            raise CoreError('unknown_outcome', 'remote operation outcome is unavailable; inspect the host before retrying') from None
        if outcome.get('attachment'):
            descriptor = dict(outcome['attachment'])
            descriptor['argv'] = ['ssh', '-t', target, 'zsh -lic ' + shlex.quote(shlex.join(descriptor['argv']))]
            descriptor['cwd'] = str(Path.home())
            descriptor['validation']['host'] = host
            descriptor['validation']['target'] = target
            outcome['attachment'] = descriptor
        outcome['host'] = host
        return outcome

    def _mutate(self, method, args):
        cfg = self.config()
        if method.startswith('sessions.') and args.get('host', 'local') != 'local':
            return self._remote_mutate(cfg, method, args)
        if method.startswith('sessions.'):
            if method == 'sessions.open' and args.get('new'):
                repo = args.get('repo')
                if repo not in cfg.repos:
                    raise CoreError('invalid_target', 'select a registered repository')
                slot, session = 'new', None
            else:
                repo, slot, session = self._slot(cfg, args)
            action = method.split('.')[1]
            if action in ('open', 'resume'):
                suffix = ['--cli', '--local'] if action == 'open' else []
                if args.get('agent') in ('codex', 'claude'):
                    suffix.append('--' + args['agent'])
                snippet = shlex.join(['_t_' + action, repo, slot, *suffix])
                if action == 'resume' and args.get('sid'):
                    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', args['sid']):
                        raise CoreError('invalid_target', 'provide an exact conversation identifier')
                    snippet = 'T_CORE_RESUME_SID=' + shlex.quote(args['sid']) + ' ' + snippet
                if action == 'open':
                    agent = cfg.agent_for(repo, args.get('agent'))
                    for name, array in (('model', 'DEV_MODEL'), ('effort', 'DEV_EFFORT'), ('fast', 'DEV_FAST')):
                        if name in args:
                            value = args[name]
                            if not isinstance(value, str) or '\x00' in value or '\n' in value:
                                raise CoreError('invalid_params', 'invalid agent setting')
                            snippet = array + '[' + agent + ']=' + shlex.quote(value) + '; ' + snippet
                outcome = self._run_shell(snippet)
                session = outcome.get('session') or session
                if not session:
                    raise CoreError('unknown_outcome', 'session allocation could not be verified')
            elif action == 'close':
                owner = args['_owner']
                try:
                    self.validate_attachment(owner)
                except CoreError:
                    raise CoreError('stale_target', 'selected owner changed; refresh before reviewing the action') from None
                cwd = owner['identity'].split('\t', 4)[4]
                self._run_shell(shlex.join(['_dev_with_launch_locks', cwd, owner['sid'],
                    '_dev_kill_one', session, '1', owner['identity'], owner['sid'], owner['agent'], owner['clientPid']]))
            else:
                owner = args['_owner']
                try:
                    self.validate_attachment(owner)
                except CoreError:
                    raise CoreError('stale_target', 'selected owner changed; refresh before reviewing the action') from None
                try:
                    row = {'cwd': owner['identity'].split('\t', 4)[4], 'sid': owner['sid'], 'agent': owner['agent']}
                    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', row['sid']):
                        raise CoreError('invalid_target', 'restart requires a verified conversation')
                except ValueError:
                    raise CoreError('invalid_target', 'restart requires an available verified conversation and worktree') from None
                self._run_shell(shlex.join(['_dev_with_launch_locks', row['cwd'], row['sid'], '_t_restart_slot', session, row['cwd'], row['sid'], row['agent'], 'restart', owner['clientPid'], '', owner['identity']]))
            return {'session': session, 'attachment': self.attachment(session) if action != 'close' else None}
        if method in ('hosts.change', 'config.set', 'repositories.register'):
            before = self.cli._config_read(self.cli._current_local_rc())
            initial = self.cli._config_state(cfg)
            if method == 'hosts.change':
                self.cli._host_change(cfg, args.get('action'), args.get('alias'), args.get('target'),
                                      args.get('default', False), args.get('clear', False))
            elif method == 'repositories.register':
                alias, path = args.get('alias'), args.get('path')
                if not self.cli._CONFIG_ALIAS.fullmatch(alias or '') or not isinstance(path, str) or not os.path.isdir(path):
                    raise CoreError('invalid_target', 'provide a valid alias and existing repository directory')
                if self.cli._run(['git', '-C', path, 'rev-parse', '--git-dir'], timeout=10).returncode:
                    raise CoreError('invalid_target', 'directory is not a Git repository')
                cfg.repos[alias] = os.path.realpath(path)
            else:
                changes = args.get('changes', {})
                if not isinstance(changes, dict):
                    raise CoreError('invalid_params', 'changes must be an object')
                for key, value in changes.items():
                    array = re.fullmatch(r'([A-Z_]+)\[([A-Za-z0-9_][A-Za-z0-9_.-]*)\]', key)
                    if (key not in self.cli._CONFIG_SCALARS and (not array or array[1] not in self.cli._CONFIG_ARRAYS)
                            or not isinstance(value, str) or '\n' in value or '\x00' in value):
                        raise CoreError('invalid_params', 'unsupported configuration setting')
                    setting = array[1] if array else key
                    choices = {'DEV_AGENT_DEFAULT': ('claude', 'codex'), 'DEV_AGENT': ('claude', 'codex'),
                               'DEV_OPEN_MODE_DEFAULT': ('cli', 'app'), 'DEV_OPEN_MODE': ('cli', 'app'),
                               'T_AUTO_TRUST': ('0', '1'), 'DEV_WORKTREE_DEFAULT': ('0', '1'),
                               'DEV_WORKTREE': ('0', '1')}
                    if setting in choices and value not in choices[setting]:
                        raise CoreError('invalid_params', 'invalid configuration value')
                    if array:
                        getattr(cfg, self.cli._CONFIG_ARRAYS[array[1]])[array[2]] = value
                    else:
                        setattr(cfg, self.cli._CONFIG_SCALARS[key], value)
            return {'changes': persist_config(self.api, cfg, before, initial)}
        if method == 'issues.start':
            helper = runpy.run_path(str(self.root / 'libexec' / 't_issue.py'))
            owner = helper['IssueCommand'](cfg, self.api)
            words = args.get('targets') or []
            if not isinstance(words, list) or not words or not all(isinstance(word, str) for word in words):
                raise CoreError('invalid_target', 'provide explicit issue targets')
            namespace = self.cli.build_parser().parse_args(['issue', 'start', *words])
            # Explicit headless selection; existing lock-protected launch rechecks ownership.
            def choose(report):
                matches = report['matches']
                if not matches:
                    return None
                if (len(matches) == 1 and matches[0].get('verified') and matches[0].get('session')
                        and not matches[0].get('app') and not matches[0].get('resume')):
                    return matches[0]
                raise CoreError('review_required', 'existing issue work requires explicit terminal review')
            owner.choose = choose
            repo, targets = helper['split_targets'](words)
            if not targets:
                raise CoreError('invalid_target', 'provide explicit issue targets')
            rows, errors = [], []
            for target in targets:
                try:
                    row = owner.run_one(namespace, [repo, target] if repo else [target], attach=False)
                    if row:
                        rows.append(row)
                except (ValueError, OSError, KeyError, TypeError):
                    errors.append({'target': target, 'code': 'review_required',
                                   'message': 'issue needs review; inspect its ownership before retrying', 'retryable': False})
            attachments = [self.attachment(row['session'], host=row.get('host', 'local')) for row in rows]
            return {'sessions': rows, 'attachments': attachments, 'errors': errors,
                    'state': 'partial' if errors else 'completed'}
        raise CoreError('invalid_method', 'unsupported mutation')

    def apply(self, operation_id):
        self._read(operation_id)  # Validate before constructing a lock path.
        with (self.state_root / (operation_id + '.lock')).open('a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._apply_locked(operation_id)

    def _apply_locked(self, operation_id):
        record = self._read(operation_id)
        if record['state'] != 'prepared':
            raise CoreError('already_applied', 'operation cannot be replayed; reconcile current state')
        # Persist uncertainty before performing any external effect.
        record['state'] = 'applying'
        self._record(record)
        self.progress(operation_id, 'applying')
        try:
            if record['method'] == 'terminal':
                record['state'] = 'ready'
            else:
                arguments = dict(record['arguments'])
                if 'owner' in record:
                    arguments['_owner'] = record['owner']
                record['result'] = self._mutate(record['method'], arguments)
                record['attachment'] = record['result'].get('attachment')
                record['state'] = record['result'].get('state', 'completed')
        except CoreError as error:
            if error.code in ('invalid_params', 'invalid_target', 'review_required', 'invalid_method', 'stale_target', 'stale_attachment'):
                record['state'] = 'rejected'
                record['error'] = {'code': error.code, 'message': str(error), 'retryable': False}
                self._record(record)
                raise
            record['state'] = 'unknown'
            self._record(record)
            raise CoreError('unknown_outcome', 'operation may have changed state; reconcile before starting new work') from None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            record['state'] = 'unknown'
            self._record(record)
            raise CoreError('unknown_outcome', 'operation may have changed state; reconcile before starting new work') from None
        self.progress(operation_id, record['state'])
        return self._record(record)

    def sessions(self, cfg, all_hosts=False):
        result = self.cli._run(self.shell('_dev_session_rows --details'), timeout=45)
        if result.returncode:
            raise CoreError('discovery_failed', 'session discovery failed')
        rows = self.cli._parse_rows(result.stdout)
        self.remote_status = [{'host': host, 'status': 'not_scanned'} for host in sorted(cfg.hosts)]
        if all_hosts and cfg.hosts:
            def scan(item):
                host, target = item
                if not self.cli._BARE_HOST.fullmatch(target) or target.startswith('-'):
                    return [], {'host': host, 'status': 'incomplete'}
                snippet = 'source "${commands[t]:A:h:h}/t.plugin.zsh"; _dev_session_rows --details'
                remote = 'zsh -lic ' + shlex.quote(snippet)
                result = self.cli._run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', target, remote], timeout=15)
                if result.returncode:
                    return [], {'host': host, 'status': 'incomplete'}
                remote_rows = self.cli._parse_rows(result.stdout)
                for row in remote_rows:
                    row['host'] = host
                return remote_rows, {'host': host, 'status': 'scanned'}
            self.remote_status = []
            with ThreadPoolExecutor(max_workers=min(4, len(cfg.hosts))) as executor:
                for remote_rows, status in executor.map(scan, sorted(cfg.hosts.items())):
                    rows.extend(remote_rows)
                    self.remote_status.append(status)
        for row in rows:
            alias = row['slot'].rpartition('-')[0]
            if row['host'] != 'local':
                row['repo'] = alias or None
            else:
                row['repo'] = cfg.repo_of_dir(row['cwd'])[0] if row.get('cwd') else None
                if not row['repo']:
                    row['repo'] = alias if alias in cfg.repos else None
            row['title'] = row['summary']
            row['live'] = row['state'] not in ('dead', 'closed')
        return rows

    def operation_state(self, method, params):
        operation_id = params.get('operationId')
        self._read(operation_id)
        with (self.state_root / (operation_id + '.lock')).open('a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._operation_state_locked(operation_id, method, params)

    def _operation_state_locked(self, operation_id, method, params):
        record = self._read(operation_id)
        if method == 'operations.started':
            if record['state'] != 'ready':
                raise CoreError('already_applied', 'terminal operation cannot be replayed')
            record['state'] = 'running'
        elif method == 'operations.finished':
            if record['state'] != 'running':
                raise CoreError('invalid_operation', 'terminal operation is not running')
            record['state'] = 'completed' if params.get('exitCode') == 0 else 'partial'
            record['exitCode'] = params.get('exitCode')
        elif record['state'] in ('applying', 'ready', 'running'):
            record['state'] = 'unknown'
        if record['state'] == 'unknown':
            record.pop('attachment', None)
            if isinstance(record.get('result'), dict):
                record['result'].pop('attachment', None)
                record['result'].pop('attachments', None)
        return self._record(record)

    def call(self, method, params):
        if method == 'attachments.validate':
            return self.validate_attachment(params.get('validation'))
        if method == 'mcp.capabilities':
            return {'tools': sorted(self.cli._MCP_HANDLERS), 'transport': 'shared-read', 'installCommand': ['mcp', '--install']}
        if method == 'initialize':
            return {'protocolVersion': 1, 'version': self.cli._t_version(), 'capabilities': self.capabilities()}
        if method == 'operations.prepare':
            return self.prepare(params)
        if method == 'operations.apply':
            return self.apply(params.get('operationId'))
        if method in ('operations.reconcile', 'operations.started', 'operations.finished'):
            return self.operation_state(method, params)
        if method in ('sessions.open', 'sessions.resume', 'sessions.close', 'sessions.restart',
                      'hosts.change', 'config.set', 'repositories.register', 'issues.start'):
            return self.prepare({'method': method, 'arguments': params})
        cfg = self.config()
        if method == 'sessions.attach':
            repo, slot, session = self._slot(cfg, params)
            return {'attachment': self.attachment(session, host=params.get('host', 'local'))}
        if method == 'sessions.list':
            return {'sessions': self.sessions(cfg, params.get('all_hosts', False)), 'remoteStatus': self.remote_status}
        if method in ('sessions.search', 'sessions.detail', 'mcp.call'):
            name = {'sessions.list': 'list_sessions', 'sessions.search': 'find_sessions', 'sessions.detail': 'session_detail'}.get(method, params.get('name'))
            if name not in self.cli._MCP_HANDLERS:
                raise CoreError('invalid_method', 'unsupported shared read')
            return self.cli._mcp_call(cfg, self.index, name, params.get('arguments', params))
        repositories = [{'alias': alias, 'path': path} for alias, path in sorted(cfg.repos.items())]
        hosts = [{'alias': alias, 'target': target, 'default': cfg.beam_host in (alias, target)} for alias, target in sorted(cfg.hosts.items())]
        if method == 'repositories.list':
            return {'repositories': repositories}
        if method == 'hosts.list':
            return {'hosts': hosts}
        if method == 'config.get':
            return {'config': self.cli._config_state(cfg)}
        if method == 'agents.status':
            return {'agents': self.cli._install_probe()}
        if method == 'system.diagnostics':
            return {'dependencies': [{'name': name, 'available': bool(shutil.which(name))}
                                     for name in ('git', 'tmux', 'zsh', 'ssh', 'gh', 'codex', 'claude')],
                    'runtime': str(self.root), 'protocolVersion': PROTOCOL_VERSION}
        if method in ('issues.list', 'issues.status'):
            helper = runpy.run_path(str(self.root / 'libexec' / 't_issue.py'))
            owner = helper['IssueCommand'](cfg, self.api)
            if method == 'issues.list':
                state = params.get('state', 'open')
                if state not in ('open', 'closed', 'all'):
                    raise CoreError('invalid_params', 'issue state must be open, closed, or all')
                identities, scope, scope_errors = owner.list_repositories(params.get('repo'), params.get('all', False))
                errors = [{'repo': None, 'code': 'unavailable', 'message': 'registered repository could not be resolved'} for _ in scope_errors]
                rows = []
                for identity in identities:
                    try:
                        rows.extend(owner.issues(identity, state))
                    except (ValueError, OSError):
                        errors.append({'repo': identity, 'code': 'unavailable', 'message': 'repository issues could not be read'})
                return {'issues': rows, 'scope': scope, 'state': state, 'errors': errors, 'incomplete': bool(errors)}
            issue = owner.issue(params.get('targets') or [])
            return owner.scan(issue)
        if method == 'workspace.snapshot':
            sessions = self.sessions(cfg, params.get('all_hosts', False))
            return {'sessions': sessions, 'repositories': repositories, 'hosts': hosts,
                    'config': self.cli._config_state(cfg), 'capabilities': self.capabilities(),
                    'remoteStatus': self.remote_status}
        raise CoreError('method_not_found', 'unknown bridge method')
