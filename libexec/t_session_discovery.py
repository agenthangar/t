"""Adapters and collectors for the single session-cache API; no cache policy/I/O."""
import errno
import json
import math
import os
from pathlib import Path
import runpy
import shlex
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
_CACHE = runpy.run_path(str(ROOT / 'libexec/t_session_cache.py'))
_verified_title = runpy.run_path(str(ROOT / 'libexec/t_resume_source.py'))['verified_title']
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']


def request(operation, payload=None, *, deadline=None):
    if deadline is not None:
        return _CACHE['request'](operation, payload, deadline=deadline)
    return _CACHE['request'](operation, payload)


def discover(cfg, *, local=False, host=None, fields=('rows', 'resume'), deadline=None, refresh=False, observe=False):
    context = {key: getattr(cfg, key, None) for key in ('repos', 'worktree', 'worktree_root', 'worktree_default')}
    targets = {'local': 'local', **({} if local else cfg.hosts)}
    if host is not None:
        targets = {host: 'local' if host == 'local' else cfg.hosts.get(host, host)}
    options = {'deadline': deadline} if deadline is not None else {}
    payload = {'fields': list(fields), 'hosts': [dict(host=host, target=target, context=context)
                                         for host, target in targets.items()]}
    if refresh:
        payload['refresh'] = True
    if observe:
        payload['observe'] = True
    return request('discover', payload, **options)


def wait_for_refresh(cfg, view, *, local=False, timeout=30, host=None, fields=None):
    """Observe the shared refresh without changing expiry, ownership or workers."""
    deadline = time.monotonic() + timeout
    with _spinner('Refreshing sessions'):
        while not view.get('unavailable') and (view.get('loading') or any(
                h.get('refreshing') for h in view.get('hosts', []))):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(view, wait_timed_out=True)
            time.sleep(min(.25, remaining))
            if time.monotonic() >= deadline:
                return dict(view, wait_timed_out=True)
            options = {'host': host} if host is not None else {}
            latest = discover(cfg, local=local, fields=fields or ['rows'], deadline=deadline, observe=True, **options)
            if latest.get('unavailable'):
                return dict(view, wait_timed_out=True) if time.monotonic() >= deadline else dict(
                    view, wait_unavailable=True)
            view = latest
    return view


def _usable(view, fields=None):
    for host in view.get('hosts', []):
        value = host.get('value') or {}
        if fields is not None:
            useful = ['catalog'] if 'catalog' in fields else ['resume'] if 'resume' in fields else fields
            value = {key: value.get(key) for key in useful}
        if value.get('rows') or value.get('resume') or (value.get('catalog') or {}).get('conversations'):
            return True
    return False


def require_open_network(cfg, *, local=False, host=None):
    """Reject a known OS network failure, without DNS, packets or a timed probe.

    UDP connect only checks local routing/policy. Success is not evidence of
    internet access: unknown failures and either usable IP family leave normal
    discovery in charge. Never persist this caller's sandbox state in the cache.
    """
    if host == 'local' or (host is None and (local or not getattr(cfg, 'hosts', {}))):
        return
    unavailable = {errno.EPERM, errno.EACCES, errno.ENETDOWN, errno.ENETUNREACH,
                   errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT,
                   errno.EPROTONOSUPPORT}
    for family, address in ((socket.AF_INET, '192.0.2.1'), (socket.AF_INET6, '2001:db8::1')):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as connection:
                connection.setblocking(False)
                connection.connect((address, 9))
            return
        except OSError as error:
            if error.errno not in unavailable:
                return
    raise OSError('network unavailable or blocked; use t open --local for local sessions')


def _partial(view):
    return bool(view.get('unavailable') or view.get('wait_unavailable') or view.get('loading') or
                view.get('wait_skipped') or view.get('wait_timed_out') or
                any(h.get('refreshing') or h.get('stale') or h.get('error') or h.get('value') is None
                    for h in view.get('hosts', [])))


def _browse_snapshot(cfg, choose, wait, options, *, preflight=None):
    if preflight:
        preflight()
    view = discover(cfg, **options)
    while True:
        # Retained results are useful immediately, even when a different host
        # is cold or broken. Selection still performs live ownership checks.
        if _usable(view, options.get('fields')) or not _partial(view):
            return view, 'Older or incomplete results · new sessions may be missing' if _partial(view) else ''
        first, failures = True, 0

        def refresh():
            nonlocal view, first, failures
            if first:
                first = False
                return False
            latest = discover(cfg, deadline=time.monotonic() + 1, **options)
            if latest.get('unavailable'):
                failures += 1
                view = dict(view, unavailable=True, wait_unavailable=True)
            else:
                failures = 0
                view = latest
            pending = view.get('loading') or any(h.get('refreshing') for h in view.get('hosts', []))
            return _usable(view, options.get('fields')) or failures >= 5 or (not view.get('unavailable') and not pending)

        pending = view.get('unavailable') or view.get('loading') or any(h.get('refreshing') for h in view.get('hosts', []))
        if pending:
            if wait is not None:
                if wait(refresh) == 'skip':
                    view = dict(view, wait_skipped=True)
            else:
                view = wait_for_refresh(cfg, view, **options)
        if _usable(view, options.get('fields')) or not _partial(view):
            return view, 'Older or incomplete results · new sessions may be missing' if _partial(view) else ''
        if choose is None:
            return view, 'Older or incomplete results · new sessions may be missing'
        action = choose('No sessions available yet' if view.get('wait_skipped') else 'Could not refresh sessions', _usable(view, options.get('fields')))
        if action == 'cached' and _usable(view, options.get('fields')):
            return view, 'Older or incomplete results · new sessions may be missing'
        if action != 'retry':
            raise KeyboardInterrupt
        if preflight:
            preflight()
        # Explicit retry is scoped to failed targets, not a global reset. It
        # never touches agents, held ownership locks or saved conversations.
        for host in view.get('hosts', []):
            if host.get('error'):
                request('reset-host', {'target': host['target']})
        view = discover(cfg, **options)


def open_snapshot(cfg, choose, *, local=False, host=None, fields=None, wait=None):
    options = dict(local=local, fields=fields or ['rows'])
    if host is not None:
        options['host'] = host
    return _browse_snapshot(cfg, choose, wait, options, preflight=lambda:
                            require_open_network(cfg, local=local, host=host))


def reset_host(cfg, alias):
    if alias not in cfg.hosts:
        raise ValueError(f"unknown host {alias!r}; run `t hosts list` to list registered hosts")
    result = request('reset-host', {'target': cfg.hosts[alias]})
    if result.get('unavailable') or result.get('error'):
        raise ValueError(result.get('error') or 'session discovery unavailable; retry shortly')
    return result


def resume_snapshot(cfg, wait, *, fields=None, choose=None):
    """Warm browsing is immediate; cold discovery and recovery stay skippable."""
    return _browse_snapshot(cfg, choose, wait, {'fields': fields or ['rows', 'resume']})


def freshness(view):
    return {key: value for key, value in view.items() if key != 'hosts'} | {
        'hosts': [{k: v for k, v in host.items() if k != 'value'} for host in view.get('hosts', [])]}


def resume_health(view):
    hosts, auth_hosts = [], []
    for host in view.get('hosts', []):
        error = host.get('error', '')
        kind = host.get('error_kind')
        if host.get('resume_pr_auth_failed'):
            auth_hosts.append(host['host'])
        if (host.get('refreshing') or view.get('loading')) and view.get('wait_timed_out'):
            status = 'timed out'
        elif host.get('refreshing') or view.get('loading'):
            status = 'refreshing'
        elif kind == 'timeout' or 'timed out' in error.lower():
            status = 'timed out'
        elif error:
            status = {'transport': 'unreachable', 'authentication': 'SSH auth failed'}.get(kind, 'refresh failed')
        elif not host.get('checked'):
            status = 'not checked'
        elif host.get('stale'):
            status = 'retained'
        else:
            status = 'current'
        hosts.append(dict(host=host['host'], status=status, checked=host.get('checked'), observed=host.get('observed')))
    return dict(hosts=hosts, auth_hosts=auth_hosts,
                unavailable=bool(view.get('unavailable') or view.get('wait_unavailable')))


def announce(view):
    if view.get('wait_timed_out'):
        print('t: Refresh wait timed out; showing available sessions.', file=sys.stderr)
    if view.get('wait_unavailable'):
        print('t: Session discovery unavailable; showing the last available snapshot.', file=sys.stderr)
    if view.get('unavailable'):
        print('t: Session discovery unavailable; retry shortly.', file=sys.stderr)
    elif view.get('loading') or (not any(h.get('value') is not None for h in view.get('hosts', []))
                                 and any(h.get('refreshing') for h in view.get('hosts', []))):
        print('t: Session discovery is refreshing; run again shortly.', file=sys.stderr)
    else:
        for host in view['hosts']:
            age = max(0, int((time.time() - host['checked']) / 60)) if host['checked'] else None
            status = ('checked ' + str(age) + 'm ago') if age is not None else 'not yet checked'
            if host.get('refreshing'):
                status += ' · refreshing in background'
            if host.get('error'):
                kind = host.get('error_kind')
                reason = 'unreachable' if kind == 'transport' else 'authentication failed' if kind == 'authentication' else 'refresh failed'
                status += ' · ' + reason
                if host.get('value') is not None:
                    status += '; retaining last known sessions'
                if host.get('target') != 'local':
                    remaining = max(0, math.ceil((host.get('retry_at', 0) - time.time()) / 60))
                    status += f' · retry in {remaining}m; reset: t hosts reset {shlex.quote(host["host"])}'
            print(f't: Cached {host["host"]} sessions · {status}', file=sys.stderr)


def rows(view, *, prefixed=True, repo=None, cli_only=False):
    result = []
    for host in view.get('hosts', []):
        value = host.get('value') or {}
        roots = value.get('scopes', {}).get(repo, []) if repo else []
        if repo and not roots:
            catalog = value.get('catalog', {})
            identities = {r['repo'] for r in catalog.get('repositories', []) if repo in (r['alias'], r['repo'])}
            roots = [r['path'] for r in catalog.get('repositories', []) if r['repo'] in identities and r.get('path')]
            roots += [a['workspace'] for r in catalog.get('conversations', []) if r['repo'] in identities for a in r.get('associations', [])]
        for row in value.get('rows', '').splitlines():
            fields = row.split('\t')
            if len(fields) >= 7:
                if cli_only and fields[3] == 'app':
                    continue
                if repo and not any(fields[1] == root or fields[1].startswith(root.rstrip('/') + '/') for root in roots):
                    continue
                result.append((host['host'] + '\t' if prefixed else '') + row)
    return '\n'.join(result) + ('\n' if result else '')


def candidates(view, aliases, slot=None):
    seen = set()
    all_repos = not aliases or aliases == ['--all']
    for host in view.get('hosts', []):
        for raw in (host.get('value') or {}).get('resume', []):
            fields = raw.split('\t')
            if len(fields) not in (12, 13, 14, 15, 16) or (not all_repos and fields[1] not in aliases and fields[10] not in aliases):
                continue
            if slot is not None and fields[2] != slot:
                continue
            fields[7] = host['host']
            key = (fields[10], fields[8], fields[3])
            if key not in seen:
                seen.add(key)
                yield '\t'.join(fields)


def _catalog_candidates(catalog, history, host):
    """Project validated host records without invoking discovery again."""
    helper = runpy.run_path(str(ROOT / 'libexec/t_history.py'))
    records = catalog.get('conversations', [])
    for row in records:
        helper['validate'](row)
        alias = history.repository_alias(row['repo']) or row['repo']
        if not history.worktree_enabled(alias if alias in history.cfg.repos else None):
            continue
        if not row.get('title') or not row.get('sources') or not row.get('associations'):
            continue
        if host != 'local':
            history.pr_states([row])
        association = max(row['associations'], key=lambda a: a.get('observed', 0))
        yield helper['resume_candidate'](row, alias, association, host)


def export_local(cfg, payload):
    context = {key: getattr(cfg, key, None) for key in ('repos', 'worktree', 'worktree_root', 'worktree_default')}
    return request('export', {'spec': dict(host='local', target='local', context=context),
                              'generation': payload.get('generation'), 'refresh': payload.get('refresh') is True})


def apply_remote(wire, previous):
    """Merge wire deltas without dropping records on a partial/failed scan."""
    if 'full' in wire:
        return wire['full'], None
    if not previous.get('_remote_generation'):
        raise _CACHE['DiscoveryError']('Remote delta has no local baseline', 'protocol')
    value = dict(previous)
    delta = wire.get('delta')
    if delta is None:
        return value, []
    records = {_CACHE['record_key'](r): r for r in previous['catalog']['conversations']}
    for key in delta['removed']:
        records.pop(key, None)
    for row in delta['changed']:
        records[_CACHE['record_key'](row)] = row
    value.update(delta['fields'])
    value['catalog'] = {**delta['catalog'], 'conversations': list(records.values())}
    return value, delta['changed']


def active_resume_owners(rows):
    """Only the inventory's verified live targeting identities can prove ownership."""
    owners = []
    for line in rows.splitlines():
        fields = line.split('\t')
        if len(fields) < 7:
            continue
        sid, cwd, _, state, context, _, agent = fields[:7]
        if (sid not in ('', '-') and cwd.startswith('/') and context == 'active'
                and state in ('attached', 'detached', 'foreground', 'app') and agent in ('codex', 'claude')):
            owners.append(dict(sid=sid, cwd=cwd, agent=agent))
    return owners


def collect(spec, api):
    """Bounded worker; caller is the cache service, never the display path."""
    started = time.monotonic()
    timings = {}
    cfg = api['Config']()
    history_module = runpy.run_path(str(ROOT / 'libexec/t_history.py'))
    history = history_module['HistoryCommand'](cfg, api)
    try:
        if spec['target'] == 'local':
            history.cache_discovery = True
            shell = api['ZSH_PRELUDE'] + 'setopt pipefail; source ' + shlex.quote(str(ROOT / 't.plugin.zsh')) + '; _dev_session_rows --details'
            result = subprocess.run(['zsh', '-lic', shell], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30,
                                    env={**os.environ, 'T_HOME': str(ROOT), 'T_NO_AUTORELOAD': '1',
                                         'T_DISCOVERY_COLLECTOR': '1', 'T_REMOTE_QUERY_LOCAL': '1'})
            if result.returncode:
                error = _CACHE['DiscoveryError'](result.stderr.splitlines()[-1] if result.stderr else 'local session scan failed', 'collector')
                error.detail = result.stderr
                raise error
            row_text = result.stdout
            timings['live_seconds'] = round(time.monotonic() - started, 4)
            if spec.get('live_only'):
                return dict(rows=row_text, resume=[], _timings=timings)
            stage = time.monotonic()
            catalog = history.local_catalog()
            auth = request('artifact', {'key': 'resume-pr-auth'}).get('value') or []
            if spec.get('reconcile') and any(row.get('prs') for row in catalog.get('conversations', [])):
                history.diagnostics_enabled = True
                history.check_auth()
                auth = history.diagnostic_rows()
                request('artifact', {'key': 'resume-pr-auth', 'value': auth})
            catalog['diagnostics'] = auth
            timings['history_seconds'] = round(time.monotonic() - stage, 4)
            stage = time.monotonic()
            cache_key = 'display-v6:' + _CACHE['_digest'](spec.get('context', {}))
            previous = request('artifact', {'key': cache_key}).get('value') or {}
            state = {}
            old_context = previous.get('mcp', {})
            if hasattr(history, 'catalog_paths'):
                changed_paths = (history.catalog_paths if not old_context or spec.get('reconcile')
                                 else history.catalog_changed_paths)
                records = [rec for rec in old_context.get('recs', []) if rec.get('path') in history.catalog_paths
                           and rec.get('path') not in changed_paths
                           and not rec.get('_no_transcript')]
                for path in changed_paths:
                    record = api['_index_update'](path, api['_index_dir']())
                    if record and not record.get('subagent') and not record.get('invalid_identity'):
                        records.append(record)
                state = dict(recs=api['_index_dedupe'](records), at=time.time())
            ctx = api['_mcp_context'](cfg, state) if not spec.get('export') else dict(recs=[])
            # Include live conversations before their first transcript is indexed.
            known = {r.get('sid') for r in ctx['recs']}
            for sid, info in ctx.get('live', {}).items():
                if sid not in known:
                    ctx['recs'].append(dict(sid=sid, cwd=info.get('cwd'), prompts=[], n_prompts=0,
                                            mtime=ctx['now'], _no_transcript=True))
            # Resolve display sidecars once in the collector, not on each read.
            for rec in ctx['recs']:
                if old_context.get('live', {}).get(rec.get('sid')) != ctx['live'].get(rec.get('sid')) or spec.get('reconcile'):
                    rec.pop('_cached_row', None)
                cached = rec.get('_cached_row')
                row = api['_session_row'](cfg, rec, ctx['live'], ctx['now'], ctx['cache_root'])
                if rec.get('_no_transcript'):
                    row['title'] = '(no transcript yet)'
                if not cached and row.get('slot') and row.get('cwd'):
                    row['url'] = api['_dev_url'](row['slot'], row['cwd'])
                rec['_cached_row'] = row
            timings['metadata_seconds'] = round(time.monotonic() - stage, 4)
            stage = time.monotonic()
            scopes = {}
            for repository in catalog.get('repositories', []):
                path = repository.get('path')
                if path:
                    roots = [path, str(Path(cfg.worktree_root) / Path(path).name)]
                    scopes[repository['alias']] = scopes[repository['repo']] = roots
            records = catalog.get('conversations', [])
            if records:
                history.pr_states(records)
                history.annotate_resume_work(records, active_resume_owners(row_text))
            projections, dirty = {}, []
            for record in records:
                key = _CACHE['record_key'](record)
                stamp = _CACHE['_digest'](record)
                old = previous.get('projections', {}).get(key, {})
                lines = old.get('lines') if old.get('stamp') == stamp and not spec.get('reconcile') else None
                projections[key] = dict(stamp=stamp, lines=lines if lines is not None else [])
                if lines is None:
                    dirty.append(record)
            for line in _catalog_candidates({**catalog, 'conversations': dirty}, history, 'local'):
                fields = line.split('\t')
                key = _CACHE['record_key'](dict(repo=fields[10], agent=fields[8], sid=fields[3]))
                projections[key]['lines'].append(line)
            resume = [line for projection in projections.values() for line in projection['lines']]
            request('artifact', {'key': cache_key, 'value': dict(mcp=ctx, projections=projections)})
            timings['projection_seconds'] = round(time.monotonic() - stage, 4)
            return dict(scopes=scopes, rows=row_text, resume=resume, _timings=timings,
                        catalog=catalog, mcp=ctx)
        previous = spec.get('previous') or {}
        # Reprobe the modern endpoint on each legacy (hourly) observation so a
        # peer upgrade takes effect without deleting its retained inventory.
        legacy = False
        command = 'command t __cache-export'
        def remote(command, payload=None):
            options = dict(capture_output=True, text=True, timeout=30)
            if payload is None:
                options['stdin'] = subprocess.DEVNULL
            else:
                options['input'] = json.dumps(payload)
            try:
                return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', spec['target'],
                                       'zsh -lic ' + shlex.quote(command)], **options)
            except subprocess.TimeoutExpired as exc:
                raise _CACHE['DiscoveryError']('Remote discovery timed out', 'timeout') from exc
        payload = {'generation': previous.get('_remote_generation')}
        if spec.get('refresh'):
            payload['refresh'] = True
        result = remote(command, payload)
        unsupported = lambda r: r.returncode == 2 and any(marker in r.stderr for marker in ('invalid choice', 'unrecognized arguments', 'unknown command'))
        if not legacy and unsupported(result):
            legacy = True
            result = remote('command t __cache-collect-local')
        if legacy and unsupported(result):
            command = ('setopt pipefail; rows=$(_dev_session_rows --details) || exit $?; '
                       'catalog=$(command t __history-catalog) || exit $?; '
                       'python3 -c ' + shlex.quote('import json,sys; print(json.dumps(dict(rows=sys.argv[1], catalog=json.loads(sys.argv[2]))))') +
                       ' "$rows" "$catalog"')
            result = remote(command)
        if result.returncode:
            detail = result.stderr or 'remote scan failed; update t on the host'
            kind = ('authentication' if any(x in detail.lower() for x in ('permission denied', 'host key verification failed'))
                    else 'transport' if result.returncode == 255 else 'collector')
            error = _CACHE['DiscoveryError'](detail.splitlines()[-1], kind)
            error.detail = detail
            raise error
        try:
            wire = json.loads(result.stdout)
        except ValueError as exc:
            raise _CACHE['DiscoveryError']('Invalid remote discovery response', 'protocol') from exc
        timings['remote_seconds'] = round(time.monotonic() - started, 4)
        stage = time.monotonic()
        changed = None
        if legacy:
            value = wire
        else:
            if not isinstance(wire, dict) or wire.get('protocol') != 2:
                raise _CACHE['DiscoveryError']('invalid remote inventory', 'protocol')
            health = wire.get('health') or {}
            if wire.get('pending') or health.get('refreshing'):
                if health.get('error') and not health.get('refreshing'):
                    raise _CACHE['DiscoveryError'](health['error'], health.get('error_kind') or 'collector')
                return {'_pending': True}
            value, changed = apply_remote(wire, previous)
            value['_remote_generation'] = wire['generation']
            value['_remote_health'] = wire.get('health', {})
        if not isinstance(value, dict) or not isinstance(value.get('rows'), str):
            raise ValueError('invalid remote inventory')
        catalog = value['catalog']
        if changed is None or changed:
            history.cache_discovery = True
            history.import_catalog(catalog if changed is None else {**catalog, 'conversations': changed}, spec['target'])
        if changed is None:
            value['resume'] = list(_catalog_candidates(catalog, history, spec['target']))
        elif 'delta' in wire:
            delta = wire['delta']
            removed = set(delta['removed']) | {_CACHE['record_key'](r) for r in changed}
            kept = []
            for candidate in previous.get('resume', []):
                f = candidate.split('\t')
                if len(f) in (12, 13, 14, 15, 16) and _CACHE['record_key']({'repo': f[10], 'agent': f[8], 'sid': f[3]}) not in removed:
                    kept.append(candidate)
            value['resume'] = kept + list(_catalog_candidates({**catalog, 'conversations': changed}, history, spec['target']))
        timings['import_seconds'] = round(time.monotonic() - stage, 4)
        value['_timings'] = timings
        value.pop('mcp', None)
        if legacy:
            value['_legacy_remote'] = True
        return value

    finally:
        history.store.close()


def event(targets=None, *, event='changed', row=None):
    payload = dict(targets=targets or ['local'], event=event)
    if row:
        payload['row'] = row
    return request('event', payload)


def invalidate(targets=None):
    return _CACHE['invalidate'](targets)


class SnapshotStore:
    """Read-only view returned by the service; no persistence or cache policy."""
    def __init__(self, view):
        self.directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/history/v1'
        merged = {}
        self.identities = {}
        for host in view.get('hosts', []):
            if host['host'] == 'local':
                self.identities.update({r['alias']: r['repo'] for r in (host.get('value') or {}).get('catalog', {}).get('repositories', [])})
            for raw in (host.get('value') or {}).get('catalog', {}).get('conversations', []):
                row = json.loads(json.dumps(raw))
                for field in ('sources', 'associations'):
                    row[field] = [{**v, 'host': host['host']} for v in row.get(field, [])]
                key = (row['repo'], row['agent'], row['sid'])
                if key in merged:
                    old = merged[key]
                    if row.get('updated', 0) < old.get('updated', 0):
                        old, row = row, old
                    row.update(_verified_title(row, old))
                    for field in ('sources', 'associations'):
                        row[field] += old.get(field, [])
                merged[key] = row
        self.values = list(merged.values())

    def records(self):
        return json.loads(json.dumps(self.values))

    def close(self):
        pass
