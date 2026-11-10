"""Remote command policies, argument boundaries and bounded SSH execution.

Public commands remain owned by t's normal registry and handlers. This module
adds host scope, never shell interpretation of user arguments.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shlex
import subprocess
import sys

PROTOCOL = 1
SHORTCUTS = {'open': 'open', 'resume': 'resume', 'move': 'move', 'kill': 'close'}
UNSUPPORTED = {
    'session cd': 'changes the calling shell; open a remote shell with t hosts run HOST',
    'repo cd': 'changes the calling shell; open a remote shell with t hosts run HOST',
    'session push': 'requires the current agent shell',
    'session pop': 'requires the current agent shell',
    'issue open': 'opens a browser; use t issue open locally',
    'hosts run': 'already delegates arbitrary commands; use t hosts run directly',
}
# Only finite, explicitly read-only modes may fan out. In particular, --json is
# presentation, not evidence that a command cannot mutate state.
QUERIES = frozenset({
    'session list', 'repo list', 'repo locate', 'issue list', 'issue status',
    'cursor list', 'hosts list', 'hosts show', 'workflow list', 'workflow context',
    'workflow instructions show', 'config show', 'config models list',
    'config models show', 'config models runtime status', 'config models requests list',
    'config models requests show', 'agent status', 'runners list', 'runners runs list',
    'runners runs status', 'profile show', 'policy show', 'policy check', 'mcp status',
})
ORCHESTRATED = frozenset({'session open', 'session resume', 'session move', 'session close'})
# Commands whose *existing* omitted-repository behavior uses the caller's cwd.
INFER = frozenset({
    'session open', 'session resume', 'session history', 'session move',
    'session close', 'session restart', 'session read', 'session view-plan',
    'session paste', 'session search', 'repo locate', 'issue list', 'issue status',
    'issue start', 'profile show', 'profile edit', 'profile init', 'policy check',
    'policy show', 'policy apply', 'runners runs start', 'workflow context',
})


def leaves(groups, prefix=()):
    for name, value in groups.items():
        if isinstance(value, dict):
            yield from leaves(value, (*prefix, name))
        else:
            yield (*prefix, name)


def extend_registry(groups):
    def mirror(node, prefix):
        return {name: mirror(value, (*prefix, name)) if isinstance(value, dict)
                else ('remote', *prefix, name) for name, value in node.items()}
    groups['remote'].update({name: mirror(value, (name,)) for name, value in groups.items()
                             if name != 'remote'})


def nested(words):
    return len(words) > 1 and words[0] == 'remote' and (
        words[1] == '--host' or words[1].startswith('--host=') or
        words[1] not in (*SHORTCUTS, '-h', '--help'))


def split(words, groups):
    """Resolve command words only; leave every child option and payload literal."""
    words = list(words)
    host = None
    if words and words[0] == '--host':
        if len(words) < 2 or not words[1] or words[1].startswith('-'):
            raise ValueError('remote --host requires a host before the command')
        host, words = words[1], words[2:]
    elif words and words[0].startswith('--host='):
        host, words = words[0][7:], words[1:]
        if not host:
            raise ValueError('remote --host requires a host')
    if words and words[0] in SHORTCUTS:
        words = ['session', SHORTCUTS[words[0]], *words[1:]]
    node, path = groups, []
    for word in words:
        if word == 'ls' and isinstance(node, dict) and 'list' in node:
            word = 'list'
        if not isinstance(node, dict) or word not in node or word == 'remote':
            break
        path.append(word)
        node = node[word]
    if not path or isinstance(node, dict):
        raise ValueError('remote requires a complete public command; use t remote --help')
    return tuple(path), words[len(path):], host


def policy(path, tail=()):
    name = ' '.join(path)
    options = list(tail[:tail.index('--')] if '--' in tail else tail)
    if name in UNSUPPORTED:
        return 'unsupported'
    if name in ORCHESTRATED:
        return 'session'
    if name in QUERIES:
        return 'query'
    if name == 'hosts default' and not options:
        return 'query'
    if name == 'system diagnose' and '--repair' not in options:
        return 'query'
    if name == 'system advisories' and (not options or options == ['status']):
        return 'query'
    if name == 'system update' and options == ['--check']:
        return 'query'
    if name in ('agent login', 'agent trust', 'agent install') and options == ['--status']:
        return 'query'
    if name == 'config local' and options == ['--show']:
        return 'query'
    if name == 'session history' and '--json' in options:
        return 'query'
    return 'single'


def select_host(path, tail, host, cfg):
    tail = list(tail)
    if not host and tail and tail[0] in cfg.hosts:
        if cfg.resolve_repo(tail[0]):
            raise ValueError(f'{tail[0]!r} names both a host and repository; use t remote --host HOST')
        host = tail.pop(0)
    if host and (host.startswith('-') or any(c.isspace() or ord(c) < 32 for c in host)):
        raise ValueError('invalid SSH host')
    if host == 'local':
        raise ValueError('remote host must be an SSH host; use --to local for a move destination')
    return tail, host


def child_args(api, path, tail):
    return api['build_parser']().parse_args([*path, *tail])


def repository_context(api, cfg, path, tail, parsed):
    """Carry verified repo identity, never local paths or an assumed remote alias.

    Replacement positions are found from the child's parser, not by replacing
    equal strings in arbitrary payloads (goals/runner commands may contain aliases).
    """
    name = ' '.join(path)
    selector = getattr(parsed, 'workflow_repo', None) or getattr(parsed, 'repo', None)
    if name == 'session history':
        targets = getattr(parsed, 'target', [])
        selector = targets[0] if targets and not targets[0].isdecimal() else None
    index = None
    words = list(tail)
    for i, word in enumerate(words):
        if word == '--':
            break
        if word == '--repo' and i + 1 < len(words):
            selector, index = words[i + 1], i + 1
            break
        if word.startswith('--repo='):
            selector, index = word[7:], i
            break
    # Repo positionals exist on these families; repo create/clone/add are names
    # and paths, not selectors. Issue parsing has its own variadic target list.
    positional_repo = (path[0] == 'session' or name in ('repo locate', 'profile apply'))
    if positional_repo and selector and index is None:
        parser = api['_help_parser'](api['build_parser'](), api['_canonical_argv'](list(path)))
        skip = False
        for i, word in enumerate(words):
            if skip:
                skip = False
                continue
            if word == '--':
                if i + 1 < len(words):
                    index = i + 1
                break
            if word.startswith('-'):
                option = parser._option_string_actions.get(word.split('=')[0])
                skip = bool(option and option.nargs != 0 and '=' not in word)
            else:
                index = i
                break
    if path[0] == 'issue' and index is None:
        targets = getattr(parsed, 'target', None) or []
        selector = getattr(parsed, 'repository', None) or selector
        if targets and not targets[0].isdecimal() and '://' not in targets[0]:
            selector = targets[0]
        if selector:
            # This is the first positional; options are parsed separately.
            index = words.index(selector) if selector in words else None
    if path[0] == 'session' and selector and selector.isdecimal():
        selector, index = None, None
    alias = cfg.resolve_repo(selector) if selector else None
    if not selector and name in INFER and not any(w in ('-a', '--all') for w in words):
        alias, _ = cfg.repo_of_dir(os.getcwd())
    if not alias:
        return None
    history = api['_remote_history'](cfg)
    try:
        identity = history.identity(alias)
    finally:
        history.store.close()
    return {'identity': identity, 'index': index,
            'option': index is not None and words[index].startswith('--repo=')}


def ssh_argv(target, worker, *, terminal=False, query=False):
    argv = ['ssh']
    if query:
        argv += ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3']
    if terminal:
        argv.append('-t')
    return [*argv, target, 'zsh -lic ' + shlex.quote(worker)]


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def run_host(host, target, path, tail, context, *, query, terminal):
    payload = encode({'version': PROTOCOL, 'command': list(path), 'args': list(tail),
                      'context': context})
    # Established finite lists can run on older peers without repository
    # translation. Mutations and mapped context require the validated worker.
    legacy_queries = {('session', 'list'), ('repo', 'list'), ('workflow', 'list'),
                      ('hosts', 'list'), ('agent', 'status'), ('config', 'show'), ('mcp', 'status')}
    legacy = query and path in legacy_queries and context is None and not any(
        word in ('-r', '--remote') for word in tail)
    fallback = ('T_NO_UPDATE_CHECK=1 t ' + shlex.join([*path, *tail])) if legacy else (
        'print -u2 ' + shlex.quote(f't remote: update t on {host}: t hosts run {host} t system update') + '; exit 2')
    worker = ('if [[ $(T_NO_UPDATE_CHECK=1 command t __remote-protocol 2>/dev/null) == 1 ]]; then '
              'T_NO_UPDATE_CHECK=1 command t __remote-exec ' + shlex.quote(payload) +
              '; else ' + fallback + '; fi')
    try:
        if query:
            result = subprocess.run(ssh_argv(target, worker, query=True), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            return host, result.returncode, result.stdout, result.stderr
        return subprocess.call(ssh_argv(target, worker, terminal=terminal))
    except subprocess.TimeoutExpired:
        return host, 124, '', 'remote query timed out\n'
    except OSError as error:
        if query:
            return host, 1, '', str(error) + '\n'
        print(f't remote: {error}', file=sys.stderr)
        return 1


def render_results(results, *, machine=False, single=False):
    failed = False
    values, errors = {}, {}
    for host, rc, out, err in results:
        failed |= bool(rc)
        if single:
            sys.stdout.write(out)
            sys.stderr.write(err)
            return rc
        if machine:
            if not rc:
                try:
                    values[host] = json.loads(out)
                except ValueError:
                    rc, err, failed = 1, 'host returned invalid JSON', True
            if rc:
                errors[host] = {'status': rc, 'message': err.strip() or f'exit {rc}'}
        else:
            print(f'\n{host}:')
            print(out.rstrip() or ('No results.' if not rc else 'Query failed.'))
        if err:
            print(f't remote [{host}]: {err.rstrip()}', file=sys.stderr)
    if machine:
        print(json.dumps({'results': values, 'errors': errors}))
    return int(failed)


def execute(cfg, args, api):
    try:
        path, tail, host = split(args.rest, api['COMMAND_GROUPS'])
        tail, host = select_host(path, tail, host, cfg)
        mode = policy(path, tail)
        if mode == 'unsupported':
            raise ValueError(f"{' '.join(path)} {UNSUPPORTED[' '.join(path)]}")
        if os.environ.get('T_REMOTE_EXEC_DEPTH') in (str(os.getpid()), str(os.getppid())):
            raise ValueError('nested remote prefixes are unsupported')
        # These are deliberately controller-owned: their established handlers
        # coordinate exact owner rechecks and transfer the caller's terminal.
        if mode == 'session':
            action = {'close': 'kill'}.get(path[1], path[1])
            if path[1] != 'close' or not host:
                argv = [action, *tail]
                if host:
                    argv += ['--from' if action == 'move' else '--host', host]
                worker = 'T_NO_UPDATE_CHECK=1 _t_remote ' + shlex.join(argv)
                return subprocess.call(['zsh', '-lic', worker])
        parsed = child_args(api, path, tail)
        context = repository_context(api, cfg, path, tail, parsed)
        if mode != 'query' and not host:
            if not cfg.hosts:
                raise ValueError('no remote hosts configured; use t hosts add ALIAS TARGET')
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise ValueError('name a host after the action or use t remote --host HOST')
            ui = api['_RailUI']()
            try:
                ui.raw()
                picked = ui.pick('Run this command on which host?',
                                 [(alias, alias) for alias in sorted(cfg.hosts)], values=cfg.hosts)
            except (KeyboardInterrupt, EOFError):
                return 130
            finally:
                ui.restore()
            if picked is None:
                return 130
            if picked not in cfg.hosts:
                raise ValueError('selected host is no longer registered')
            host = picked
        if host:
            targets = [(host, cfg.hosts.get(host, host))]
        else:
            seen, targets = set(), []
            for alias, target in sorted(cfg.hosts.items()):
                if target not in seen:
                    targets.append((alias, target))
                    seen.add(target)
        if not targets:
            if '--json' in tail:
                print(json.dumps({'results': {}, 'errors': {}}))
            else:
                print('No remote hosts configured. Use t hosts add ALIAS TARGET.')
            return 0
        if mode == 'query':
            with api['_spinner']('Checking remote hosts', machine='--json' in tail), \
                    ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
                futures = [pool.submit(run_host, alias, target, path, tail, context,
                                       query=True, terminal=False) for alias, target in targets]
                results = [future.result() for future in futures]
            return render_results(results, machine='--json' in tail, single=bool(host))
        return run_host(*targets[0], path, tail, context, query=False,
                        terminal=sys.stdin.isatty() and sys.stdout.isatty() and '--json' not in tail)
    except ValueError as error:
        print(f't remote: {error}', file=sys.stderr)
        return 2


def peer(payload, api):
    """Validate again on the peer before running its installed public handler."""
    try:
        data = json.loads(base64.b64decode(payload, altchars=b'-_', validate=True))
        if (not isinstance(data, dict) or data.get('version') != PROTOCOL or
                not isinstance(data.get('command'), list) or not isinstance(data.get('args'), list) or
                not all(isinstance(w, str) and '\0' not in w for w in data['command'] + data['args'])):
            raise ValueError('invalid remote request')
        path, tail, host = split(data['command'] + data['args'], api['COMMAND_GROUPS'])
        if list(path) != data['command'] or host or policy(path, tail) == 'unsupported':
            raise ValueError('unsupported remote command')
        cfg = api['Config']()
        context = data.get('context')
        cwd = None
        if context:
            if not isinstance(context, dict) or not isinstance(context.get('identity'), str):
                raise ValueError('invalid repository context')
            history = api['_remote_history'](cfg)
            try:
                alias = history.repository_alias(context['identity'])
            finally:
                history.store.close()
            if not alias:
                raise ValueError('the source repository is not registered on this host')
            index = context.get('index')
            if index is not None:
                if type(index) is not int or not 0 <= index < len(tail):
                    raise ValueError('invalid repository argument')
                tail[index] = ('--repo=' if context.get('option') else '') + alias
            cwd = cfg.repos[alias]
        child_args(api, path, tail)
        mode = policy(path, tail)
        # Finite queries are confined to this peer. Session lifecycle actions
        # keep cross-host ownership checks; closing explicitly stays here.
        query = mode == 'query'
        worker = ('export T_NO_UPDATE_CHECK=1 T_REMOTE_EXEC_DEPTH=$$; '
                  + ('export T_REMOTE_QUERY_LOCAL=1; REMOTE_HOSTS=(); ' if query and path[0] in ('session', 'issue') else '')
                  + ('cd -- ' + shlex.quote(cwd) + ' || exit $?; ' if cwd else ''))
        if path == ('session', 'close'):
            worker += 'export T_REMOTE_ACTION_LOCAL=1; REMOTE_HOSTS=(); '
        if path == ('session', 'resume'):
            worker += '_T_RESUME_REQUIRE_PICKER=1 _t_resume ' + shlex.join(tail)
        else:
            worker += 't ' + shlex.join([*path, *tail])
        return subprocess.call(['zsh', '-lic', worker])
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(f't remote: {error}', file=sys.stderr)
        return 2
