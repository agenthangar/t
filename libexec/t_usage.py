"""Read-only usage projection. Configuration is never evidence of inference.

Only the SessionStart hook writes launch snapshots; reporting never creates state.
Token counts include input and output, with cached input a subset of input.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 64 * 1024 * 1024


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def state_root():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't'


def read(path, default=None):
    try:
        if Path(path).stat().st_size > LIMIT:
            return default
        value = json.loads(Path(path).read_text())
        return value if default is None or isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def events(path, *, limit=LIMIT, needles=()):
    """Bound individual lines and total bytes, tolerate torn records, never execute content."""
    if not path:
        return
    try:
        with Path(path).open('rb') as stream:
            while limit is None or stream.tell() < limit:
                offset = stream.tell()
                line = stream.readline(2 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if len(line) < 2 * 1024 * 1024:
                        break
                    # Skip an oversized tool/output record, not the remainder
                    # of the session. It cannot be a normal usage event.
                    while line and not line.endswith(b'\n'):
                        line = stream.readline(2 * 1024 * 1024)
                    continue
                if needles and not any(needle in line for needle in needles):
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        yield offset, item
                except ValueError:
                    continue
    except OSError:
        return


def location(provider):
    if provider in ('lmstudio', 'ollama', 'oss'):
        return 'local'
    if provider in ('openai', 'anthropic', 'frontier', 'cloud'):
        return 'cloud'
    return 'unknown'


def tokens(value):
    if not isinstance(value, dict):
        return None
    incoming = value.get('input_tokens', value.get('prompt_tokens'))
    output = value.get('output_tokens', value.get('completion_tokens'))
    details = value.get('input_tokens_details')
    cached = value.get('cached_input_tokens', details.get('cached_tokens', 0) if isinstance(details, dict) else 0)
    if any(type(x) is not int or x < 0 for x in (incoming, output, cached)) or cached > incoming:
        return None
    return {'input_tokens': incoming, 'output_tokens': output, 'cached_input_tokens': cached}


def summarize(requests):
    result = {'observed_records': len(requests), 'measured_records': 0, 'tokens': {}, 'models': [],
              'observed_tokens_per_second': {}, 'estimated_cloud_cost_usd': None, 'savings_usd': None}
    for place in ('local', 'cloud', 'unknown'):
        rows = [r for r in requests if r['location'] == place]
        measured = [r for r in rows if r.get('usage') is not None]
        result['measured_records'] += len(measured)
        # No observed requests is absence of evidence, not evidence of zero usage.
        result['tokens'][place] = sum(r['usage']['input_tokens'] + r['usage']['output_tokens'] for r in measured) if measured else None
        timed = [r for r in measured if type(r.get('seconds')) in (int, float) and r['seconds'] > 0]
        result['observed_tokens_per_second'][place] = sum(r['usage']['output_tokens'] for r in timed) / sum(r['seconds'] for r in timed) if timed else None
    for row in requests:
        identity = {'model': row.get('model') or 'unknown', 'provider': row.get('provider') or 'unknown', 'location': row['location']}
        if identity not in result['models']:
            result['models'].append(identity)
    result['coverage'] = 'observed records only; missing historical requests are not counted'
    local, cloud = (result['tokens'][p] or 0 for p in ('local', 'cloud'))
    result['local_cloud_percent'] = [round(100 * local / (local + cloud)), 100 - round(100 * local / (local + cloud))] if local + cloud and not result['tokens']['unknown'] else None
    cloud_rows = [r for r in requests if r['location'] == 'cloud']
    if cloud_rows and all(r.get('cost_usd') is not None for r in cloud_rows):
        result['estimated_cloud_cost_usd'] = sum(r['cost_usd'] for r in cloud_rows)
    result['known_cloud_cost_subtotal_usd'] = sum(r['cost_usd'] for r in cloud_rows if r.get('cost_usd') is not None) if any(r.get('cost_usd') is not None for r in cloud_rows) else None
    return result


def transcript(path, agent):
    requests, meta, previous, model, provider = [], {}, None, None, None
    seen = {}
    pending_models, pending_providers = set(), set()
    needles = (b'"session_meta"', b'"turn_context"', b'"token_count"') if agent == 'codex' else (b'"assistant"',)
    for offset, row in events(path, limit=None, needles=needles):
        kind = row.get('type')
        payload = row.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        if kind == 'session_meta':
            meta = payload
            provider = payload.get('model_provider')
        elif kind == 'turn_context':
            model = payload.get('model') or model
            provider = payload.get('model_provider') or provider
            if model:
                pending_models.add(model)
            pending_providers.add(provider)
        elif kind == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info')
            usage = tokens(info.get('total_token_usage') if isinstance(info, dict) else None)
            if usage is None or usage == previous:
                continue
            # Cumulative decreases indicate a reset or incomplete history. Do not
            # add an entire new cumulative total or attribute it to the last model.
            if previous and any(usage[k] < previous[k] for k in usage):
                previous = usage
                requests.append(dict(id=str(offset), offset=offset, model=model, provider=provider, location=location(provider), usage=None))
                continue
            observed_model = model if len(pending_models) <= 1 else None
            observed_provider = provider if len(pending_providers) <= 1 else None
            pending_models.clear(); pending_providers.clear()
            delta = {k: usage[k] - (previous[k] if previous else 0) for k in usage}
            previous = usage
            requests.append(dict(id=str(offset), offset=offset, model=observed_model, provider=observed_provider, location=location(observed_provider), usage=delta))
        elif agent == 'claude' and kind == 'assistant':
            message = row.get('message') or {}
            if not isinstance(message, dict):
                continue
            ident = message.get('id')
            if not isinstance(ident, str) or message.get('model') == '<synthetic>':
                continue
            meta.update({k:v for k,v in {'id':row.get('sessionId'), 'cwd':row.get('cwd')}.items() if v})
            model = message.get('model') or model
            raw = message.get('usage') or {}
            usage = tokens(raw)
            if usage:
                # Anthropic reports cache read/create separately from input.
                cache = raw.get('cache_read_input_tokens', 0)
                created = raw.get('cache_creation_input_tokens', 0)
                if any(type(v) is not int or v < 0 for v in (cache, created)):
                    usage = None
                else:
                    usage['input_tokens'] += cache + created
                    usage['cached_input_tokens'] = cache
            request = dict(id=ident, offset=offset, model=model, provider='anthropic', location='cloud', usage=usage)
            if ident in seen:
                previous_message = requests[seen[ident]]
                request['offset'] = previous_message['offset']
                if usage is None:
                    request['usage'] = previous_message['usage']
                elif previous_message['usage']:
                    request['usage'] = {k: max(usage[k], previous_message['usage'][k]) for k in usage}
                if request['model'] != previous_message['model']:
                    request['model'] = None
                requests[seen[ident]] = request
            else:
                seen[ident] = len(requests)
                requests.append(request)
    return meta, requests, model


def policy_view(policy):
    # Record model/routing choices, never custom Markdown or prompt instructions.
    return {k: policy[k] for k in ('enabled', 'inference', 'session_defaults', 'planning', 'implementation', 'review', 'model_stages') if k in policy}


def launch_context(agent, cwd):
    workflow = helper('t_workflow')
    document = workflow['tool_document'](workflow['load']()[0], agent)
    # Resolve worktrees through Git metadata without running Git or a shell.
    repository = str(Path(cwd).resolve())
    for parent in (Path(repository), *Path(repository).parents):
        marker = parent / '.git'
        if marker.is_dir():
            repository = str(parent); break
        if marker.is_file():
            text = marker.read_text().strip()
            if text.startswith('gitdir: '):
                directory = (parent / text[8:]).resolve()
                common = directory / 'commondir'
                if common.is_file():
                    repository = str((directory / common.read_text().strip()).resolve().parent)
            break
    policy = workflow['effective'](document, repository)
    name = workflow['source'](document, repository)
    fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:12]
    return {'workflow': name, 'revision': fingerprint, 'policy': policy_view(policy), 'repository': repository}


def local_launch(home, cwd):
    """Capture the local lead's policy before its isolated native process starts."""
    record = dict(launch_context('codex', cwd), time=time.time(), offset=0)
    path = Path(home) / 'usage-context.json'
    path.write_text(json.dumps(record)); path.chmod(0o600)


def snapshot_start(sid, agent, cwd, path):
    """Private hook action: freeze policy per transcript offset, never at report time."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', sid) or agent not in ('codex', 'claude'):
        return
    context = launch_context(agent, cwd)
    target = state_root() / 'usage' / (agent + '-' + sid + '.json')
    data = read(target, {'sid': sid, 'agent': agent, 'cwd': cwd, 'transcript': path, 'launches': []})
    offset = Path(path).stat().st_size if path and Path(path).exists() else 0
    data['transcript'] = path or data.get('transcript', '')
    data['launches'] = launch_records(data)
    data['launches'].append(dict(context, offset=offset, time=time.time()))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix('.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(data)); temporary.chmod(0o600); temporary.replace(target)


def launch_records(saved):
    values = (saved or {}).get('launches', [])
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, dict) and type(v.get('offset')) is int and v['offset'] >= 0
            and type(v.get('time')) in (int, float) and isinstance(v.get('workflow'), str)
            and isinstance(v.get('revision'), str) and isinstance(v.get('policy'), dict)]


def session(path, agent, saved=None):
    meta, requests, lead = transcript(path, agent)
    sid = meta.get('id') or (saved or {}).get('sid')
    if not isinstance(sid, str) or not sid or (isinstance(meta.get('source'), dict) and 'subagent' in meta['source']):
        return None
    launches = launch_records(saved)
    for request in requests:
        launch = next((v for v in reversed(launches) if v['offset'] <= request['offset']), None)
        request['workflow'] = (agent + ':' + launch['workflow'] + ':' + launch['revision']) if launch else None
    return dict(id='session:' + agent + ':' + sid, kind='session', sid=sid, agent=agent,
                cwd=meta.get('cwd') or (saved or {}).get('cwd'),
                label=Path(meta.get('cwd') or (saved or {}).get('cwd') or '.').name + ' · ' + sid[:8], lead_model=lead,
                transcript=str(path), requests=requests, launches=launches, **summarize(requests))


def session_paths():
    codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    for folder in ('sessions', 'archived_sessions'):
        yield from ((p, 'codex') for p in (codex / folder).glob('**/rollout-*.jsonl'))
    yield from ((p, 'codex') for p in (state_root() / 'local-worker').glob('lead-*/.codex/sessions/**/rollout-*.jsonl'))
    yield from ((p, 'claude') for p in (Path.home() / '.claude/projects').glob('*/*.jsonl'))


def session_rows(paths=None):
    saved = {p.stem: read(p, {}) for p in (state_root() / 'usage').glob('*.json')}
    unique = {}
    for path, agent in paths if paths is not None else session_paths():
        # The header establishes identity; filenames and cwd alone do not.
        row = session(path, agent)
        if not row:
            continue
        launch = saved.get(agent + '-' + row['sid'], {})
        row['launches'] = launch_records(launch)
        if not row['launches'] and Path(path).is_relative_to(state_root() / 'local-worker'):
            for parent in Path(path).parents:
                if parent.name.startswith('lead-'):
                    row['launches'] = launch_records({'launches': [read(parent / 'usage-context.json', {})]})
                    break
        for request in row['requests']:
            match = next((v for v in reversed(row['launches']) if v['offset'] <= request['offset']), None)
            request['workflow'] = agent + ':' + match['workflow'] + ':' + match['revision'] if match else None
        old = unique.get(row['id'])
        if old is None or len(row['requests']) > len(old['requests']):
            unique[row['id']] = row
    result = list(unique.values())
    attach_workers(result)
    return result


def worker_rows():
    rows = []
    root = state_root() / 'local-worker'
    for directory in root.glob('*'):
        path = directory / 'result.json'
        final = path.is_file()
        record = read(path if final else directory / 'usage.json', {})
        if not record.get('task_id') or (final and not record.get('identity_verified')):
            continue
        status = record.get('status', 'unknown')
        if not final and not 0 <= time.time() - record.get('updated_at', 0) <= 12:
            status = 'unknown (stale activity)'
        requests = []
        for offset, event in events(path.parent / 'events.jsonl') if record.get('identity_verified') else ():
            if event.get('type') != 'turn.completed':
                continue
            requests.append(dict(id=record['task_id'] + ':' + str(offset), model=record.get('resolved_model'),
                                 provider=record.get('provider'), location='local', usage=tokens(event.get('usage')),
                                 mechanism='MCP local worker', outcome=status,
                                 task_id=record['task_id']))
        rows.append(dict(id='worker:' + record['task_id'], label=record['task_id'], kind='worker', status=status,
                         requested_model=record.get('model'), workflow_snapshot=record.get('workflow_snapshot'),
                         parent=record.get('parent_session_id') if record.get('parent_identity_verified') else None,
                         started_at=record.get('started_at', 0), requests=requests, **summarize(requests)))
    return rows


def attach_workers(sessions):
    by_id = {r['sid']: r for r in sessions if r['agent'] == 'codex'}
    if not by_id:
        return
    for worker in worker_rows():
        row = by_id.get(worker['parent'])
        if row is None:
            continue
        row.setdefault('delegations', []).append({k:worker.get(k) for k in ('id', 'status', 'requested_model', 'workflow_snapshot')})
        launch = worker.get('workflow_snapshot')
        if not isinstance(launch, dict) or not all(k in launch for k in ('workflow', 'revision', 'policy')):
            launch = None
        for request in worker['requests']:
            request['workflow'] = 'codex:' + launch['workflow'] + ':' + launch['revision'] if launch else None
        row['requests'].extend(worker['requests'])
        row.update(summarize(row['requests']))


def benchmark_savings(run):
    """Paired successful attempts in one immutable run; never a token-price proxy."""
    config = run['manifest']['config']
    expected = helper('t_eval')['PROFILES'][config['profile']]['tasks'] * config['repetitions']
    def population(mode):
        rows = [v for v in run['trials'] if v['mode'] == mode]
        identities = {(v['task'], v['repetition']) for v in rows}
        if len(rows) != expected or len(identities) != expected or any(
            v.get('status') != 'completed' or v.get('resolved') is not True or v.get('workflow_valid') is not True
            or type(v.get('cost_usd')) not in (int, float) for v in rows):
            return None
        return identities, sum(v['cost_usd'] for v in rows)
    baseline = population('frontier-only')
    values = {}
    for mode in config['modes']:
        if mode == 'frontier-only':
            continue
        candidate = population(mode)
        values[mode] = baseline[1] - candidate[1] if baseline and candidate and baseline[0] == candidate[0] else None
    return values


def eval_rows():
    module = helper('t_eval')
    rows = []
    for run in module['list_runs']():
        config = run['manifest']['config']; requests = []
        for offset, event in events(module['run_directory'](run['id']) / 'events.jsonl'):
            if event.get('kind') != 'model_request_completed':
                continue
            local = event.get('provider') == 'local'
            actual = (event.get('response_metadata') or {}).get('model')
            requests.append(dict(event, id=str(offset), model=config['local_model'] if local else actual or config['frontier_model'],
                                 location='local' if local else 'cloud', usage=tokens(event.get('usage'))))
        rows.append(dict(id=run['id'], label=config['name'], kind='eval', status=run['status'], requests=requests,
                         benchmark=module['summarize'](run), baseline_savings_usd=benchmark_savings(run), pricing=config.get('pricing'), **summarize(requests)))
    return rows


def workflow_rows(sessions):
    grouped = {}
    for row in sessions:
        worker_policies = [v.get('workflow_snapshot') for v in row.get('delegations', [])]
        for launch in [*row['launches'], *worker_policies]:
            if not isinstance(launch, dict) or not all(k in launch for k in ('workflow', 'revision', 'policy')):
                continue
            key = row['agent'] + ':' + launch['workflow'] + ':' + launch['revision']
            grouped.setdefault(key, dict(id='workflow:' + key, label=launch['workflow'], kind='workflow',
                                        revision=launch['revision'], policy=launch['policy'], requests=[], sessions=[]))
        for request in row['requests']:
            key = request.get('workflow')
            if key in grouped:
                grouped[key]['requests'].append(request)
                if row['id'] not in grouped[key]['sessions']:
                    grouped[key]['sessions'].append(row['id'])
    workflow = helper('t_workflow')
    document = workflow['load']()[0]
    for agent in ('codex', 'claude'):
        selected = workflow['tool_document'](document, agent)
        for name, policy in selected['workflows'].items():
            fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:12]
            key = agent + ':' + name + ':' + fingerprint
            grouped.setdefault(key, dict(id='workflow:' + key, label=name, kind='workflow', revision=fingerprint,
                                         policy=policy_view(policy), requests=[], sessions=[]))
    return [dict(row, **summarize(row['requests'])) for row in grouped.values()]


def snapshot():
    sessions = session_rows()
    ids = {s['sid'] for s in sessions if s['agent'] == 'codex'}
    unassigned = [w for w in worker_rows() if w['parent'] not in ids]
    return sessions + workflow_rows(sessions) + unassigned + eval_rows()


def percent(row):
    value = row.get('local_cloud_percent')
    return '/'.join(map(str, value)) if value is not None else '—'


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def detail(row):
    value = lambda x: str(x) if x is not None else 'unavailable'
    lines = [row['id'], 'Kind: ' + row['kind'], 'Models: ' + ', '.join(v['model'] for v in row['models']),
             'Tokens local/cloud: ' + '/'.join(value(row['tokens'][p]) for p in ('local', 'cloud')),
             'Local/cloud token %: ' + percent(row),
             f"Measured usage records: {row['measured_records']}/{len(row['requests'])}", row['coverage'],
             'Observed output tok/s (request time incl. prefill): ' + '/'.join(value(row['observed_tokens_per_second'][p]) for p in ('local', 'cloud')),
             'Estimated cloud USD: ' + value(row['estimated_cloud_cost_usd']),
             'Known cloud USD subtotal: ' + value(row['known_cloud_cost_subtotal_usd']),
             ('Savings: paired benchmark comparisons below.' if any(v is not None for v in row.get('baseline_savings_usd', {}).values()) else 'Savings: unavailable without a compatible frontier-only baseline.'),
             'Costs are API estimates, not subscription invoices; local power/hardware excluded.']
    if row.get('status'):
        lines.append('Status: ' + row['status'])
    for delegation in row.get('delegations', []):
        lines.append('MCP worker: ' + delegation['id'] + ' · ' + delegation['status'] + ' · requested ' + str(delegation['requested_model']))
    if row.get('pricing'):
        lines.append('Pricing snapshot: ' + json.dumps(row['pricing'], sort_keys=True))
    for mode, savings in row.get('baseline_savings_usd', {}).items():
        lines.append('Paired frontier-only baseline savings USD · ' + mode + ': ' + value(savings))
    if row.get('benchmark'):
        for mode in row['benchmark']['modes'].values():
            lines.append(f"{mode['label']}: {mode['resolved']}/{mode['expected']} resolved; {mode['graded']} graded; {mode['workflow_failures']} workflow failures")
    mechanisms = sorted({r['mechanism'] + ': ' + str(r.get('outcome', 'unknown')) for r in row['requests'] if r.get('mechanism')})
    if mechanisms:
        lines.append('Delegation: ' + ', '.join(mechanisms))
    if row['kind'] == 'session':
        lines += ['Workflow attribution: ' + (', '.join(sorted({v['workflow'] for v in row['launches']})) or 'unknown (not recorded at launch)'),
                  'Unlinked worker results remain separate; configuration alone does not establish local inference.']
    return [clean(s) for s in lines]


def command(args, spinner=None):
    ui = helper('t_eval_cli')
    try:
        if spinner:
            with spinner('Reading recorded usage'):
                rows = snapshot()
        else:
            rows = snapshot()
        machine = getattr(args, 'json', False)
        ident = getattr(args, 'usage_id', None)
        if args.usage_action == 'show':
            if not ident:
                if machine:
                    raise ValueError('specify ID with --json')
                ident = ui['choose']([{'key': r['id'], 'label': r['kind'] + ' · ' + r['label'] + ' · ' + percent(r),
                                      'preview': '\n'.join(detail(r))} for r in rows], 'Workflow, session and evaluation usage', 'show', prompt='usage')
                if ident is None:
                    return 130
            rows = [r for r in rows if r['id'] == ident]
            if not rows:
                raise ValueError('unknown usage ID: ' + ident)
        if machine:
            print(json.dumps(rows[0] if args.usage_action == 'show' else rows, indent=2))
        elif args.usage_action == 'show':
            print('\n'.join(detail(rows[0])))
        else:
            print('Usage · this machine · observed records (workflow rows aggregate sessions)')
            print('  KIND      LOC/CLD TOKEN%  ID')
            for row in rows:
                print(f"  {row['kind']:<9} {percent(row):<15} {clean(row['id'])}")
            if not rows:
                print('  No recorded usage.')
        return 0
    except (ValueError, OSError) as error:
        print('t system usage: ' + str(error), file=sys.stderr); return 1
    except ui['EmptySelection']:
        return 0


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == 'record':
        snapshot_start(*sys.argv[2:])
