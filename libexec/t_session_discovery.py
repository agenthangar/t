"""Adapters and collectors for the single session-cache API; no cache policy/I/O."""
import datetime
import json
import math
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
_CACHE = runpy.run_path(str(ROOT / 'libexec/t_session_cache.py'))
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']


def request(operation, payload=None, *, deadline=None):
    if deadline is not None:
        return _CACHE['request'](operation, payload, deadline=deadline)
    return _CACHE['request'](operation, payload)


def discover(cfg, *, local=False, host=None, fields=('rows', 'resume'), deadline=None):
    context = {key: getattr(cfg, key, None) for key in ('repos', 'worktree', 'worktree_root', 'worktree_default')}
    targets = {'local': 'local', **({} if local else cfg.hosts)}
    if host is not None:
        targets = {host: 'local' if host == 'local' else cfg.hosts.get(host, host)}
    options = {'deadline': deadline} if deadline is not None else {}
    return request('discover', {'fields': list(fields), 'hosts': [dict(host=host, target=target, context=context)
                                         for host, target in targets.items()]}, **options)


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
            latest = discover(cfg, local=local, fields=fields or ['rows'], deadline=deadline, **options)
            if latest.get('unavailable'):
                return dict(view, wait_timed_out=True) if time.monotonic() >= deadline else dict(
                    view, wait_unavailable=True)
            view = latest
    return view


def open_snapshot(cfg, choose, *, local=False, host=None, fields=None):
    """Finish pending discovery before freezing an interactive open snapshot.

    A settled remote failure is skipped under its existing cooldown. An empty
    partial inventory is never permission to start a fresh session.
    """
    options = dict(local=local, fields=fields or ['rows'])
    if host is not None:
        options['host'] = host
    while True:
        view = discover(cfg, **options)
        view = wait_for_refresh(cfg, view, **options)
        hosts = view.get('hosts', [])
        partial = (view.get('unavailable') or view.get('wait_unavailable') or
                   view.get('wait_timed_out') or view.get('loading') or
                   any(h.get('error') or h.get('value') is None for h in hosts))
        waiting = view.get('wait_timed_out') or view.get('loading')
        local_failed = any(h.get('target') == 'local' and h.get('error') for h in hosts)
        available = bool(rows(view).strip())
        if not partial:
            return view, ''
        notice = 'Older or incomplete results · new sessions may be missing'
        if not waiting and not local_failed and not view.get('unavailable') and not view.get('wait_unavailable') and available:
            return view, notice
        message = ('Session discovery is still refreshing' if waiting else
                   'Session discovery is incomplete')
        action = choose(message, available)
        if action == 'cached' and available:
            return view, notice
        if action != 'retry':
            raise KeyboardInterrupt
        # Explicit retry can recover a failed local scan. Remote failures keep
        # their cooldown until expiry or an explicit `t hosts reset HOST`.
        if local_failed and not waiting:
            result = request('reset-host', {'target': 'local'})
            if result.get('unavailable'):
                raise OSError('session discovery unavailable; retry shortly')


def reset_host(cfg, alias):
    if alias not in cfg.hosts:
        raise ValueError(f"unknown host {alias!r}; run `t hosts list` to list registered hosts")
    result = request('reset-host', {'target': cfg.hosts[alias]})
    if result.get('unavailable') or result.get('error'):
        raise ValueError(result.get('error') or 'session discovery unavailable; retry shortly')
    return result


def freshness(view):
    return {key: value for key, value in view.items() if key != 'hosts'} | {
        'hosts': [{k: v for k, v in host.items() if k != 'value'} for host in view.get('hosts', [])]}


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
                status += (' · unavailable; retaining last known sessions' if host.get('value') is not None
                           else ' · unavailable; retry after hourly expiry')
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
            if len(fields) != 12 or (not all_repos and fields[1] not in aliases and fields[10] not in aliases):
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
        history.pr_states([row])
        association = max(row['associations'], key=lambda a: a.get('observed', 0))
        updated = int(row.get('updated', 0))
        states = {p.get('state') or 'UNKNOWN' for p in row.get('prs', [])}
        state = next(iter(states)) if len(states) == 1 else 'MIXED' if states else 'NONE'
        title = helper['clean'](row['title'])
        if row.get('prs'):
            title += ' · ' + helper['pr_summary'](row['prs'])
        yield '\t'.join(map(str, [updated, alias, association.get('slot') or '-', row['sid'],
                                   association['workspace'], datetime.datetime.fromtimestamp(updated).strftime('%b %d %H:%M'),
                                   title, host, row['agent'], row.get('last_turn', '-'), row['repo'], state]))


def collect(spec, api):
    """Bounded worker; caller is the cache service, never the display path."""
    cfg = api['Config']()
    history_module = runpy.run_path(str(ROOT / 'libexec/t_history.py'))
    history = history_module['HistoryCommand'](cfg, api)
    try:
        if spec['target'] == 'local':
            catalog = history.local_catalog()
            shell = api['ZSH_PRELUDE'] + 'setopt pipefail; source ' + shlex.quote(str(ROOT / 't.plugin.zsh')) + '; _dev_session_rows --details'
            result = subprocess.run(['zsh', '-lic', shell], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30,
                                    env={**os.environ, 'T_HOME': str(ROOT), 'T_NO_AUTORELOAD': '1',
                                         'T_DISCOVERY_COLLECTOR': '1', 'T_REMOTE_QUERY_LOCAL': '1'})
            if result.returncode:
                raise ValueError(' '.join(result.stderr.split())[:512] or 'local session scan failed')
            row_text = result.stdout
            ctx = api['_mcp_context'](cfg, {}) if not spec.get('export') else dict(recs=[])
            # Include live conversations before their first transcript is indexed.
            known = {r.get('sid') for r in ctx['recs']}
            for sid, info in ctx.get('live', {}).items():
                if sid not in known:
                    ctx['recs'].append(dict(sid=sid, cwd=info.get('cwd'), prompts=[], n_prompts=0,
                                            mtime=ctx['now'], _no_transcript=True))
            # Resolve display sidecars once in the collector, not on each read.
            for rec in ctx['recs']:
                row = api['_session_row'](cfg, rec, ctx['live'], ctx['now'], ctx['cache_root'])
                if rec.get('_no_transcript'):
                    row['title'] = '(no transcript yet)'
                if row.get('slot') and row.get('cwd'):
                    row['url'] = api['_dev_url'](row['slot'], row['cwd'])
                rec['_cached_row'] = row
            scopes = {}
            for repository in catalog.get('repositories', []):
                path = repository.get('path')
                if path:
                    roots = [path, str(Path(cfg.worktree_root) / Path(path).name)]
                    scopes[repository['alias']] = scopes[repository['repo']] = roots
            return dict(scopes=scopes, rows=row_text, resume=list(_catalog_candidates(catalog, history, 'local')),
                        catalog=catalog, mcp=ctx)
        command = 'command t __cache-collect-local'
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', spec['target'],
                                 'zsh -lic ' + shlex.quote(command)], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=30)
        if result.returncode == 2 and any(marker in result.stderr for marker in ('invalid choice', 'unrecognized arguments')):
            command = ('setopt pipefail; rows=$(_dev_session_rows --details) || exit $?; '
                       'catalog=$(command t __history-catalog) || exit $?; '
                       'python3 -c ' + shlex.quote('import json,sys; print(json.dumps(dict(rows=sys.argv[1], catalog=json.loads(sys.argv[2]))))') +
                       ' "$rows" "$catalog"')
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', spec['target'],
                                     'zsh -lic ' + shlex.quote(command)], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            # Transport failure is final for this hourly attempt. No fallback SSH
            # calls to the same unavailable host and no false empty inventory.
            raise ValueError(' '.join(result.stderr.split())[:512] or 'remote scan failed; update t on the host')
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not isinstance(value.get('rows'), str):
            raise ValueError('invalid remote inventory')
        catalog = value['catalog']
        history.import_catalog(catalog, spec['target'])
        value['resume'] = list(_catalog_candidates(catalog, history, spec['target']))
        value.pop('mcp', None)  # Never import remote prompt bodies for live listing.
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
                    for field in ('sources', 'associations'):
                        row[field] += old.get(field, [])
                merged[key] = row
        self.values = list(merged.values())

    def records(self):
        return json.loads(json.dumps(self.values))

    def close(self):
        pass
