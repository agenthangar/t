"""Local MCP configuration, session overrides and explicit startup observations.

Inventory never connects to a server. Native client listing is configuration
inspection; transport credentials are deliberately absent from returned rows.
"""
import copy
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('codex', 'claude', 'cursor')
MAX_BYTES = 2 * 1024 * 1024
MAX_SERVERS = 256
CODEX_SYSTEM_PATHS = (Path('/etc/codex/config.toml'), Path('/etc/codex/requirements.toml'))


def _read(path):
    """Read a bounded regular file; absent inputs are distinct from invalid ones."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ValueError(f'MCP configuration unavailable: {path}')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, encoding='utf-8') as stream:
        text = stream.read(MAX_BYTES + 1)
    if len(text.encode()) > MAX_BYTES:
        raise ValueError(f'MCP configuration unavailable: {path}')
    return text


def _json(path):
    text = _read(path)
    if text is None:
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f'MCP configuration must be an object: {path}')
    return value


def _toml(path):
    text = _read(path)
    return {} if text is None else runpy.run_path(str(ROOT / 'libexec/t_toml.py'))['loads'](text)


def _home(agent):
    name, fallback = {'codex': ('CODEX_HOME', '.codex'), 'claude': ('CLAUDE_CONFIG_DIR', '.claude'),
                      'cursor': ('CURSOR_CONFIG_DIR', '.cursor')}[agent]
    return Path(os.environ.get(name) or Path.home() / fallback).expanduser()


def _claude_user():
    custom = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(custom).expanduser() / '.claude.json' if custom else Path.home() / '.claude.json'


def _root(cwd):
    directory = Path(cwd or os.getcwd()).absolute()
    for candidate in (directory, *directory.parents):
        if (candidate / '.git').exists():
            return candidate
    return directory


def _project_paths(agent, cwd):
    directory, root = Path(cwd or os.getcwd()).absolute(), _root(cwd)
    parents = [directory]
    while parents[-1] != root and parents[-1].parent != parents[-1]:
        parents.append(parents[-1].parent)
    relative = {'codex': '.codex/config.toml', 'claude': '.mcp.json', 'cursor': '.cursor/mcp.json'}[agent]
    return [path / relative for path in reversed(parents)]


def _plugin_manifests(agent):
    cache = _home(agent) / 'plugins/cache'
    return sorted(cache.glob('*/*/*/.codex-plugin/plugin.json'))[:MAX_SERVERS] if agent == 'codex' else []


def _plugin_files(manifest):
    root = manifest.parent.parent
    value = _json(manifest).get('mcpServers', '.mcp.json')
    paths = [root / '.mcp.json', root / 'mcp.json']
    if isinstance(value, str):
        candidate = (root / value).resolve()
        if candidate.is_relative_to(root.resolve()):
            paths.append(candidate)
    return list(dict.fromkeys(paths))


def dependency_paths(agent, cwd=None):
    """Lightweight local dependencies for the launch-policy cache; no clients run."""
    if agent not in TOOLS:
        return []
    user = (_home(agent) / 'config.toml' if agent == 'codex' else
            _claude_user() if agent == 'claude' else _home(agent) / 'mcp.json')
    paths = [*(CODEX_SYSTEM_PATHS if agent == 'codex' else ()), user, *_project_paths(agent, cwd)]
    if agent == 'codex':
        cache = _home(agent) / 'plugins/cache'
        paths.append(cache)
        paths.extend(sorted(cache.glob('*'))[:MAX_SERVERS])
        paths.extend(sorted(cache.glob('*/*'))[:MAX_SERVERS])
        for manifest in _plugin_manifests(agent):
            paths.extend([manifest, *_plugin_files(manifest)])
    return list(dict.fromkeys(paths))


def configuration_fingerprint(agent, cwd=None):
    workspace, canonical = runpy.run_path(str(ROOT / 'libexec/t_trust_state.py'))['roots'](cwd or os.getcwd())
    project = {str(path): 'project:' + str(path.relative_to(workspace)) for path in _project_paths(agent, cwd)
               if path.is_relative_to(workspace)}
    entries = [['client', agent], ['repository', str(canonical)]]
    for path in dependency_paths(agent, cwd):
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                identity = [info.st_mode, hashlib.sha256(_read(path).encode()).hexdigest()]
            elif stat.S_ISDIR(info.st_mode):
                identity = [info.st_mode, sorted(child.name for child in path.iterdir())[:MAX_SERVERS]]
            else:
                raise ValueError(f'MCP configuration unavailable: {path}')
        except FileNotFoundError:
            if str(path) in project:
                continue  # Equivalent worktrees share absent inherited project layers.
            identity = None
        entries.append([project.get(str(path), str(path)), identity])
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def _latest_configuration_change(agent, cwd):
    latest = 0
    for path in dependency_paths(agent, cwd):
        try:
            info = path.lstat()
            latest = max(latest, info.st_mtime, info.st_ctime)
        except FileNotFoundError:
            pass
    return latest


def validate(value):
    if not isinstance(value, dict) or set(value) - {'servers', 'plugins'}:
        raise ValueError('MCP choices require servers and/or plugins mappings')
    count = 0
    def servers(mapping):
        nonlocal count
        if not isinstance(mapping, dict):
            raise ValueError('MCP server choices must be an object')
        for name, enabled in mapping.items():
            count += 1
            if not isinstance(name, str) or not name or len(name) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or type(enabled) is not bool:
                raise ValueError('MCP server choices require names and boolean enabled values')
    servers(value.get('servers', {}))
    plugins = value.get('plugins', {})
    if not isinstance(plugins, dict):
        raise ValueError('MCP plugin choices must be an object')
    for plugin, mapping in plugins.items():
        if not isinstance(plugin, str) or not plugin or len(plugin) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in plugin):
            raise ValueError('Invalid MCP plugin name')
        servers(mapping)
    if count > MAX_SERVERS:
        raise ValueError('Too many MCP server choices')
    return value


def choices(policy):
    return validate(policy.get('mcp', {}))


def choice(policy, server):
    selected = choices(policy)
    return (selected.get('plugins', {}).get(server.get('plugin'), {}).get(server['name'])
            if server.get('plugin') else selected.get('servers', {}).get(server['name']))


def update(policy, server, enabled):
    result = copy.deepcopy(policy)
    selected = result.setdefault('mcp', {})
    mapping = (selected.setdefault('plugins', {}).setdefault(server['plugin'], {})
               if server.get('plugin') else selected.setdefault('servers', {}))
    if enabled is None:
        mapping.pop(server['name'], None)
    elif type(enabled) is bool:
        mapping[server['name']] = enabled
    else:
        raise ValueError('MCP choice must inherit, enable or disable')
    selected['plugins'] = {key: value for key, value in selected.get('plugins', {}).items() if value}
    for field in ('servers', 'plugins'):
        if not selected.get(field):
            selected.pop(field, None)
    if not selected:
        result.pop('mcp', None)
    validate(result.get('mcp', {}))
    return result


def summary(policy):
    if policy.get('inference', {}).get('mode') == 'local-only':
        return 'Disabled · local-only runtime'
    selected = choices(policy)
    values = [*selected.get('servers', {}).values(),
              *(enabled for mapping in selected.get('plugins', {}).values() for enabled in mapping.values())]
    return (f'{sum(values)} enabled · {len(values) - sum(values)} disabled' if values else 'Inherit client settings')


def launch_args(policy, agent, cwd=None):
    selected = choices(policy or {})
    if not selected or not policy or not policy.get('enabled'):
        return []
    if policy.get('inference', {}).get('mode') == 'local-only':
        return []
    if agent != 'codex':
        raise ValueError(f'Workflow MCP controls are not supported for {agent}; use inherited client settings')
    output = []
    direct = selected.get('servers', {})
    configured, _, _ = _codex_configuration(cwd) if direct else ({}, {}, {})
    available = set(configured)
    if set(direct) - available:
        available = {entry['name'] for entry in _native_entries(cwd, configured)}
    for name, enabled in sorted(selected.get('servers', {}).items()):
        if name not in available:
            if enabled:
                raise ValueError(f'MCP server {name!r} is not configured on this machine; configure it in the client or inherit its setting')
            continue  # A disabled absent server needs no invalid transport-less table.
        output.extend(['-c', 'mcp_servers.' + json.dumps(name, ensure_ascii=False) + '.enabled=' + str(enabled).lower()])
    for plugin, mapping in sorted(selected.get('plugins', {}).items()):
        for name, enabled in sorted(mapping.items()):
            output.extend(['-c', 'plugins.' + json.dumps(plugin, ensure_ascii=False) + '.mcp_servers.' +
                           json.dumps(name, ensure_ascii=False) + '.enabled=' + str(enabled).lower()])
    return output


def cacheable(policy, agent, cwd=None):
    """Native-only managed definitions have no local invalidation evidence."""
    if agent != 'codex' or not policy or not policy.get('enabled') or policy.get('inference', {}).get('mode') == 'local-only':
        return True
    selected = choices(policy).get('servers', {})
    return not selected or set(selected).issubset(_codex_configuration(cwd)[0])


def _row(agent, name, source, enabled=None, plugin='', transport='unknown', sessions=False):
    if not isinstance(name, str) or not name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError('Invalid configured MCP server name')
    return dict(id=json.dumps([plugin, name], separators=(',', ':')), client=agent, name=name, plugin=plugin,
                source=str(source), enabled=enabled, transport=transport, controls=agent == 'codex',
                sessions=sessions, observed_status='Not measured', startup_seconds=None, observed_at=None)


def _session_server(value):
    """Only an actual t executable with its stdio arguments owns this label."""
    command = value.get('command')
    if not isinstance(command, str) or value.get('args') != ['mcp'] or value.get('url'):
        return False
    executable = command if os.path.isabs(command) else shutil.which(command)
    if not executable:
        return False
    candidates = [ROOT / 'bin/t', Path.home() / 'bin/t']
    return any(Path(executable).resolve() == path.resolve() for path in candidates)


def _codex_configuration(cwd):
    user_path = _home('codex') / 'config.toml'
    user = _toml(user_path)
    sources, configured, plugin_config = {}, {}, {}
    trusted = runpy.run_path(str(ROOT / 'libexec/t_trust_state.py'))['trusted']('codex', cwd or os.getcwd())
    for path in [CODEX_SYSTEM_PATHS[0], user_path, *(_project_paths('codex', cwd) if trusted else [])]:
        document = user if path == user_path else _toml(path)
        for name, value in _mapping(document, 'mcp_servers').items():
            if not isinstance(value, dict):
                raise ValueError('Invalid Codex MCP server configuration')
            configured[name] = {**configured.get(name, {}), **value}
            sources[name] = path
        for name, value in _mapping(document, 'plugins').items():
            if isinstance(value, dict):
                plugin_config[name] = {**plugin_config.get(name, {}), **value}
    return configured, sources, plugin_config


def _native_entries(cwd, configured):
    binary = shutil.which('codex')
    if binary:
        try:
            result = subprocess.run([binary, 'mcp', 'list', '--json'], cwd=cwd or None, capture_output=True,
                                    text=True, stdin=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('Codex MCP configuration is unavailable') from exc
        entries = json.loads(result.stdout) if not result.returncode and len(result.stdout) <= MAX_BYTES else None
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or not isinstance(entry.get('name'), str) for entry in entries):
            raise ValueError('Codex MCP configuration is unavailable')
    else:
        entries = [dict(name=name, enabled=value.get('enabled', True), transport=value) for name, value in configured.items()]
    return entries


def _codex_inventory(cwd):
    configured, sources, plugin_config = _codex_configuration(cwd)
    entries = _native_entries(cwd, configured)
    user_path = _home('codex') / 'config.toml'
    rows = []
    for entry in entries[:MAX_SERVERS]:
        transport = entry.get('transport', {})
        if not isinstance(transport, dict):
            transport = {}
        enabled = entry.get('enabled')
        if entry.get('disabled_reason'):
            enabled = False
        rows.append(_row('codex', entry['name'], sources.get(entry['name'], 'Codex client configuration'),
                         enabled if type(enabled) is bool else None, transport=transport.get('type', 'http' if transport.get('url') else 'stdio'),
                         sessions=entry['name'] == 'sessions' and _session_server(transport)))
    plugins = {}
    for manifest in _plugin_manifests('codex'):
        root = manifest.parent.parent
        name = _json(manifest).get('name', root.parent.name)
        marketplace = root.parent.parent.name
        candidates = [key for key in plugin_config if key == name or key.startswith(str(name) + '@')]
        plugin = candidates[0] if len(candidates) == 1 else str(name) + '@' + marketplace
        version = lambda path: tuple(int(part) for part in re.findall(r'\d+', path.parent.parent.name))
        if plugin not in plugins or version(manifest) > version(plugins[plugin]):
            plugins[plugin] = manifest
    for plugin, manifest in plugins.items():
        servers = {}
        declaration = _json(manifest).get('mcpServers')
        if isinstance(declaration, dict):
            servers.update(declaration)
        for path in _plugin_files(manifest):
            servers.update(_mapping(_json(path), 'mcpServers'))
        for name, value in servers.items():
            overrides = plugin_config.get(plugin, {})
            selected = _mapping(overrides, 'mcp_servers').get(name, {})
            enabled = selected.get('enabled') if isinstance(selected, dict) else None
            if overrides.get('enabled') is False:
                enabled = False
            value = value if isinstance(value, dict) else {}
            row = _row('codex', name, manifest, enabled if type(enabled) is bool else None, plugin,
                       'http' if value.get('url') else 'stdio')
            row['plugin_enabled'] = overrides.get('enabled')
            rows.append(row)
    # Explicit plugin choices remain inspectable even when its local manifest is unavailable.
    known = {row['id'] for row in rows}
    for plugin, value in plugin_config.items():
        for name, selected in _mapping(value, 'mcp_servers').items():
            row = _row('codex', name, user_path, selected.get('enabled') if isinstance(selected, dict) else None, plugin)
            if row['id'] not in known:
                rows.append(row)
    return rows


def _json_inventory(agent, cwd):
    user = _claude_user() if agent == 'claude' else _home(agent) / 'mcp.json'
    document = _json(user)
    configured = {}
    sources = {}
    disabled = set(_disabled(document))
    documents = [(user, document), *((path, _json(path)) for path in _project_paths(agent, cwd))]
    if agent == 'claude':
        project = _mapping(document, 'projects').get(str(_root(cwd)), {})
        if not isinstance(project, dict):
            raise ValueError('Invalid Claude project configuration')
        documents.append((user, project))
        disabled.update(_disabled(project))
    for path, value in documents:
        for name, server in _mapping(value, 'mcpServers').items():
            if not isinstance(server, dict):
                raise ValueError('Invalid MCP server configuration')
            configured[name], sources[name] = server, path
    return [_row(agent, name, sources[name], name not in disabled, transport=server.get('type', 'http' if server.get('url') else 'stdio'),
                 sessions=name == 'sessions' and _session_server(server)) for name, server in configured.items()]


def observations_path():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/mcp-observations-v1.json'


def _mapping(value, key):
    result = value.get(key, {})
    if not isinstance(result, dict):
        raise ValueError('Invalid MCP configuration field: ' + key)
    return result


def _disabled(value):
    names = value.get('disabledMcpServers', [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError('Invalid MCP disabled-server configuration')
    return names


def _observations():
    try:
        return _mapping(_json(observations_path()), 'servers')
    except (OSError, ValueError):
        return {}


def inventory(agent=None, cwd=None):
    rows = []
    for tool in TOOLS if agent is None else (agent,):
        if tool not in TOOLS:
            raise ValueError('Unsupported MCP client')
        found = _codex_inventory(cwd) if tool == 'codex' else _json_inventory(tool, cwd)
        recorded = _observations()
        fingerprint = configuration_fingerprint(tool, cwd)
        for row in found:
            observation = recorded.get(tool + ':' + row['id'], {})
            duration = observation.get('startup_seconds') if isinstance(observation, dict) else None
            if (isinstance(observation, dict) and observation.get('fingerprint') == fingerprint and
                    observation.get('observed_status') in ('Starting', 'Ready', 'Failed', 'Cancelled') and
                    (duration is None or isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0) and
                    _timestamp(observation.get('observed_at')) is not None and
                    isinstance(observation.get('provenance'), dict) and isinstance(observation['provenance'].get('path'), str)):
                row.update({key: observation.get(key) for key in ('observed_status', 'startup_seconds', 'observed_at', 'provenance')})
                row['observed_workspace'] = observation.get('workspace')
            rows.append(row)
    return sorted(rows, key=lambda row: (row['client'], row['plugin'], row['name']))


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value):
            try:
                datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
                return float(value)
            except (ValueError, OSError, OverflowError):
                pass
        return None
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return None


def _event_text(path):
    """Bounded startup evidence from the beginning of even a large transcript."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, encoding='utf-8', errors='replace') as stream:
        text = stream.read(MAX_BYTES)
    return text


def collect_observations(agent, workspace, sid, transcript_path='', log_path=''):
    """Consume explicit existing native events; never poll or start an MCP server."""
    if agent != 'codex' or not isinstance(sid, str) or not sid:
        return 0
    events = {}
    changed = _latest_configuration_change(agent, workspace)
    fingerprint = configuration_fingerprint(agent, workspace)
    for source in dict.fromkeys(path for path in (transcript_path, log_path) if path):
        text = _event_text(source)
        if text is None:
            continue
        verified = False
        staged = []
        for line_number, line in enumerate(text.splitlines(), 1):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            payload = value.get('payload', value.get('params', value))
            if not isinstance(payload, dict):
                continue
            if value.get('type') == 'session_meta':
                if (payload.get('id') != sid or not isinstance(payload.get('cwd'), str) or
                        Path(payload['cwd']).resolve() != Path(workspace).resolve()):
                    staged = []
                    break
                verified = True
                continue
            event = value.get('method', payload.get('type'))
            if event not in ('mcp_startup_update', 'mcpServer/startupStatus/updated'):
                continue
            if not verified:
                if (payload.get('thread_id', payload.get('threadId', payload.get('session_id'))) != sid or
                        not isinstance(payload.get('cwd'), str) or Path(payload['cwd']).resolve() != Path(workspace).resolve()):
                    continue
            payload = payload.get('params', payload)
            server = payload.get('server', payload.get('name'))
            status = payload.get('status')
            if isinstance(status, dict):
                status = status.get('state', status.get('type'))
            stamp = _timestamp(value.get('timestamp', payload.get('timestamp')))
            if not isinstance(server, str) or status not in ('starting', 'ready', 'failed', 'cancelled') or stamp is None:
                continue
            plugin = payload.get('plugin', '')
            if not isinstance(plugin, str):
                continue
            staged.append((server, plugin, status, stamp, line_number))
        starts = {}
        for server, plugin, status, stamp, line_number in staged:
            if stamp < changed:
                continue  # An older launch cannot attest to today's configuration.
            key = json.dumps([plugin, server], separators=(',', ':'))
            if status == 'starting':
                starts[key] = stamp
            duration = stamp - starts[key] if status != 'starting' and key in starts and stamp >= starts[key] else None
            if stamp >= events.get(agent + ':' + key, {}).get('observed_at', 0):
                events[agent + ':' + key] = dict(observed_status=status.capitalize(), startup_seconds=duration, observed_at=stamp,
                    sid=sid, fingerprint=fingerprint, workspace=str(Path(workspace).resolve()),
                    provenance=dict(path=str(source), line=line_number))
    if not events:
        return 0
    path = observations_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = _observations()
        stored = {key: value for key, value in stored.items() if isinstance(value, dict)}
        for key, value in events.items():
            if value['observed_at'] >= stored.get(key, {}).get('observed_at', 0):
                stored[key] = value
        stored = dict(sorted(stored.items(), key=lambda item: item[1].get('observed_at', 0), reverse=True)[:MAX_SERVERS])
        fd, temporary = tempfile.mkstemp(prefix='.mcp-observations-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(dict(version=1, servers=stored), stream)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return len(events)
