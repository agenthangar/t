"""Deterministic, bounded evidence from retained runner logs; never infer status."""
import os
from pathlib import Path
import re
import runpy
import stat

SCAN_LIMIT = 8 * 1024 * 1024
EVIDENCE_BUDGET = 4800  # Leaves ordinary text room for identities and notices.
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
SUMMARY = re.compile(r'(?:\b\d+ (?:passed|failed|errors?|skipped|warnings?|deselected|xfailed|xpassed)\b|^FAILED |^ERROR |^Tests:|^Test Suites:|^Ran \d+ tests|^OK$|^FAILURES!$|^TOTAL\s+\d+|Required test coverage|Total coverage:)')
DIAGNOSTIC = re.compile(r'^(?:E\s+|_{3,}.+_{3,}|Traceback\b)|\b(?:AssertionError|[A-Za-z]+Error|fatal error|FAILED|ERROR)\b|\berror:')


def clean(value):
    return ''.join(c for c in ANSI.sub('', str(value)) if c in '\n\t' or 32 <= ord(c) < 127 or ord(c) >= 160)


def extract(output, budget=EVIDENCE_BUDGET):
    """Line references refer to retained output, before presentation sanitization."""
    lines = output.splitlines()
    summaries = [i for i, line in enumerate(lines) if SUMMARY.search(clean(line))]
    failures = [i for i, line in enumerate(lines) if DIAGNOSTIC.search(clean(line))]
    selected = []
    seen = set()
    used = 0
    # Summaries first, then diagnostic neighbourhoods; fallback is explicitly unknown.
    aggregates = [i for i in summaries if not clean(lines[i]).startswith(('FAILED ', 'ERROR '))]
    failures_summary = [i for i in summaries if clean(lines[i]).startswith(('FAILED ', 'ERROR '))]
    candidates = [(i, 'summary') for i in aggregates[-5:] + failures_summary[-5:]]
    for i in failures[:40]:
        candidates.extend((j, 'diagnostic') for j in range(max(0, i - 2), min(len(lines), i + 5)))
    if not candidates:
        candidates = [(i, 'unrecognized output') for i in range(min(12, len(lines)))]
        candidates += [(i, 'unrecognized output') for i in range(max(12, len(lines) - 8), len(lines))]
    clipped = False
    for i, kind in candidates:
        if i in seen:
            continue
        text = clean(lines[i])
        available = min(400, budget - used - 30)
        if available < 16:
            break
        short = len(text) > available
        shown = text[:available - 1] + '…' if short else text
        selected.append(dict(line=i + 1, kind=kind, text=shown, shortened=short))
        seen.add(i)
        used += len(shown) + 30
        clipped |= short
    return dict(evidence=selected, recognized=bool(summaries or failures),
                retained_lines_scanned=len(lines), response_content_omitted=clipped or len(seen) < len(lines))


def inspect_run(job_id, target=None, state_dir=None):
    runners = runpy.run_path(str(Path(__file__).with_name('t_runners.py')))
    # Invalid IDs/targets remain usage errors, rather than evidence about a job.
    directory = runners['_job_dir'](job_id, state_dir)
    if target is not None and not runners['NAME'].fullmatch(target):
        raise ValueError('invalid runner target name')
    try:
        receipt = runners['status'](job_id, state_dir)
    except (OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
        return dict(id=job_id, status='unavailable', exit_code=None, available=False,
                    error=clean(exc), targets=[], snapshot=None)
    targets = receipt.get('targets', {})
    if target is not None and target not in targets:
        raise ValueError(f'unknown target in job: {target}')
    names = [target] if target is not None else list(targets)
    snapshot = receipt.get('snapshot') or {}
    result = dict(id=job_id, available=True, status=receipt.get('status', 'unknown'),
                  exit_code=receipt.get('exit_code'),
                  snapshot={k: snapshot[k] for k in ('id', 'source_head', 'archive_sha256') if k in snapshot},
                  targets=[])
    for name in names:
        if not runners['NAME'].fullmatch(name):
            raise ValueError('invalid target name in local receipt')
        record = targets[name]
        details = record.get('result') or {}
        lost = details.get('log_truncated')
        item = dict(name=name, status=record.get('status', 'unknown'), exit_code=record.get('exit_code'),
                    snapshot_id=record.get('snapshot_id'), source_head=details.get('source_head'),
                    log_state='available', storage_content_lost=lost if type(lost) is bool else None,
                    response_content_omitted=False, scan_limited=False, evidence=[], recognized=False,
                    retrieval=f't runners runs logs {job_id} --target {name}')
        path = directory / name / 'output.log'
        item['log_path'] = str(path)
        try:
            if path.parent.is_symlink():
                raise OSError('target log directory is a symlink')
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError('retained log is not a regular file')
                raw = stream.read(SCAN_LIMIT + 1)
            item['scan_limited'] = len(raw) > SCAN_LIMIT
            item.update(extract(raw[:SCAN_LIMIT].decode('utf-8', errors='replace'),
                                budget=max(0, EVIDENCE_BUDGET // max(1, len(names)))))
            item['response_content_omitted'] |= item['scan_limited']
            if not raw:
                item['log_state'] = 'empty'
        except FileNotFoundError:
            item['log_state'] = 'pending' if item['status'] in ('queued', 'running', 'pending') else 'missing'
        except OSError as exc:
            item.update(log_state='unavailable', error=clean(exc))
        result['targets'].append(item)
    return result


def render(result):
    """Metadata/notices always precede excerpts, even for unusually large fanout."""
    lines = [f"Run       {result['id']}", f"Status    {clean(result['status'])} · exit {result.get('exit_code') if result.get('exit_code') is not None else 'unavailable'}"]
    if not result['available']:
        return '\n'.join(lines + ['Evidence unavailable: ' + result['error'], 'No outcome inferred.'])
    for key, value in result['snapshot'].items():
        lines.append(f'{key}  {clean(value)}')
    for item in result['targets']:
        lost = {True: 'yes', False: 'no', None: 'unknown'}[item['storage_content_lost']]
        lines.extend(['', f"Target    {item['name']} · {clean(item['status'])} · exit {item['exit_code'] if item['exit_code'] is not None else 'unavailable'}",
                      f"Snapshot  {clean(item['snapshot_id'] or 'unavailable')} · revision {clean(item['source_head'] or 'unavailable')}",
                      'Log       ' + item['log_state'],
                      'Omitted from this response  ' + ('yes' if item['response_content_omitted'] else 'no'),
                      'Lost at storage limit      ' + lost,
                      'Full retained log  ' + item['retrieval']])
        if item['scan_limited']:
            lines.append('Scan limited to first 8 MiB; inspect retained log for additional evidence.')
        if item.get('error'):
            lines.append(item['error'])
    for item in result['targets']:
        lines.extend(['', 'Evidence · ' + item['name']])
        if not item['recognized']:
            lines.append('No recognized test summary or diagnostic; no outcome inferred from log content.')
        lines.extend(f"{e['line']} | [{e['kind']}] {e['text']}" for e in item['evidence'])
    return '\n'.join(lines)
