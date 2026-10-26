"""Opt-in, local installed-tool advisories; no remediation or repository code execution."""
import contextlib
import fcntl
import http.client
import hashlib
import json
import math
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

DAY = 86400
RETRY = 3600
API = 'https://api.osv.dev/v1/query'
SCHEMA = 2
CACHE_LIMIT = 1048576
MAX_QUERIES = 50


def inventory():
    return runpy.run_path(str(Path(__file__).with_name('t_tool_inventory.py')))['inventory']()


def cache_path():
    base = os.environ.get('XDG_CACHE_HOME', '')
    if not os.path.isabs(base):
        base = os.path.expanduser('~/.cache')
    return Path(base) / 't' / 'vulnerabilities.json'


def read(path):
    try:
        # Never wait on a FIFO/device, follow a cache symlink, or read beyond
        # the limit if another writer replaces/grows the file after opening.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > CACHE_LIMIT:
                return {}
            raw = stream.read(CACHE_LIMIT + 1)
        if len(raw) > CACHE_LIMIT:
            return {}
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('schema') != SCHEMA
                or not isinstance(data.get('lines'), list)
                or not all(isinstance(x, str) and len(x) < 2048 for x in data['lines'])
                or not isinstance(data.get('last_success', []), list)
                or not all(isinstance(x, str) and len(x) < 2048 for x in data.get('last_success', []))
                or not all(type(data.get(k)) in (int, float) and math.isfinite(data[k])
                           for k in ('checked', 'expires'))):
            return {}
        return data
    except (OSError, ValueError, UnicodeError):
        return {}


def write(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.vulnerabilities-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.replace(name, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def fetch(identity):
    # Only normalized public package identity/version leaves the machine.
    ecosystem, name, version = identity
    request = urllib.request.Request(API, data=json.dumps({
        'package': {'ecosystem': ecosystem, 'name': name}, 'version': version}).encode(),
        headers={'User-Agent': 't-advisory-check', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('advisory response too large')
    data = json.loads(raw)
    if (not isinstance(data, dict) or set(data) - {'vulns', 'next_page_token'} or data.get('next_page_token')
            or not isinstance(data.get('vulns', []), list)):
        raise ValueError('advisory response invalid/incomplete')
    return data.get('vulns', [])


def normalize(name, ecosystem):
    return re.sub(r'[-_.]+', '-', name).lower() if ecosystem in ('PyPI', 'crates.io') else name


def evaluate(identity, advisories):
    """OSV performs ecosystem version matching; verify exact returned package identity.

    Report source ranges/fixes verbatim, not an invented cross-ecosystem comparison.
    OSV matches are candidates: platform/build conditions still need vendor review.
    """
    ecosystem, name, version = identity
    lines = []
    for advisory in advisories:
        if not isinstance(advisory, dict):
            raise ValueError('invalid advisory')
        identifier = advisory.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9:_.-]{1,100}', identifier):
            raise ValueError('invalid advisory identifier')
        if advisory.get('withdrawn'):
            continue
        affected = advisory.get('affected')
        if not isinstance(affected, list):
            raise ValueError('advisory identity unavailable')
        matching = []
        for item in affected:
            package = item.get('package', {}) if isinstance(item, dict) else {}
            candidate = package.get('name') if isinstance(package, dict) else None
            if (isinstance(candidate, str) and package.get('ecosystem') == ecosystem
                    and normalize(candidate, ecosystem) == normalize(name, ecosystem)):
                matching.append(item)
        if not matching:
            raise ValueError('advisory package identity mismatch')
        ranges = []
        conditions = []
        for item in matching:
            if item.get('ranges'):
                ranges.append(json.dumps(item['ranges'], ensure_ascii=True, separators=(',', ':')))
            elif item.get('versions'):
                ranges.append('listed versions ' + json.dumps(item['versions']))
            if item.get('ecosystem_specific'):
                conditions.append(json.dumps(item['ecosystem_specific'], ensure_ascii=True))
        detail = '; '.join(ranges) or 'affected/fixed range unknown'
        if len(detail) > 450:
            detail = detail[:450] + ' [truncated; see source]'
        condition = '; '.join(conditions)
        if len(condition) > 150:
            condition = condition[:150] + ' [truncated; see source]'
        condition = ('; conditions ' + condition) if conditions else ''
        lines.append(f'{identifier}: OSV version match (review platform/build applicability); '
                     f'source ranges {detail}{condition}; review/update: https://osv.dev/vulnerability/{identifier}')
    return lines or ['No known findings in OSV for this package/version; not a security verdict.']


def collect(cancelled=lambda: False):
    rows, notes = inventory()
    lines = list(notes)
    results = {}
    error = False
    deadline = time.monotonic() + 120
    for item in rows:
        label = f"{item['binary'][:150]} ({', '.join(item['provenance'])[:180]})"
        if item['status'] != 'mapped':
            lines.append(f"{label}: unsupported/unknown — {item['reason']}; {item['path']}")
            continue
        identity = (item['ecosystem'], item['name'], item['version'])
        if cancelled():
            lines.append("Check stopped; remaining coverage unknown.")
            error = True
            break
        if identity not in results:
            if len(results) >= MAX_QUERIES or time.monotonic() >= deadline:
                result = ['Unavailable: request/time budget exceeded; coverage incomplete.']
                error = True
            else:
                try:
                    result = evaluate(identity, fetch(identity))
                except (OSError, ValueError, http.client.HTTPException):
                    result = ['OSV unavailable or incomplete; coverage unknown (retry in one hour).']
                    error = True
            results[identity] = result
        prefix = f"{label}: {identity[0]}:{identity[1]} installed {identity[2]}"
        lines.extend(f'{prefix} — {result}' for result in results[identity])
    lines.append('Coverage: supported repo metadata and mapped installed binaries only; other tools remain unknown.')
    # Bound cached reports and terminal content even with unusually large metadata.
    if len(lines) > 300:
        lines = lines[:299] + ['Report truncated; additional results unavailable in this view.']
        error = True
    bounded = []
    for line in lines:
        line = ''.join(c for c in line if c.isprintable()).encode('ascii', 'backslashreplace').decode()
        if len(line) > 1400:
            link = re.search(r'https://osv.dev/vulnerability/[A-Za-z0-9:_.-]+$', line)
            line = line[:1100] + ' [truncated; see source] ' + (link[0] if link else '')
        bounded.append(line)
    return bounded, error


def check(force=False, interval=DAY, cancelled=None, authorized=False):
    if not authorized and os.environ.get('T_VULNERABILITY_CHECK') != '1':
        return
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix('.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = time.time()
        old = read(path)
        if not force and now <= old.get('expires', 0) <= now + 30 * DAY:
            return old
        try:
            lines, error = collect(cancelled) if cancelled is not None else collect()
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            lines, error = ['Check failed; installed-tool coverage unknown (retry in one hour).'], True
        if error and old:
            previous = old.get('last_success', old['lines'] if not old.get('error') else [])
            if previous:
                lines += ['Previous results (stale):'] + previous[:20]
        else:
            previous = []

        data = {'schema': SCHEMA, 'checked': now, 'expires': now + (min(RETRY, interval) if error else interval),
                'lines': lines, 'error': error,
                'last_success': previous[:20] if error else lines[:20]}
        write(path, data)
        return data


def startup():
    """Foreground: bounded local cache read and detached worker; no probes/network."""
    if os.environ.get('T_VULNERABILITY_CHECK') != '1':
        return
    try:
        data = read(cache_path())
        now = time.time()
        fresh = now <= data.get('expires', 0) <= now + DAY
        label = 'cached' if fresh else 'stale' if data else 'pending'
        print(f't advisories ({label}; repo-related installed tools):', file=sys.stderr)
        if data:
            print(f'  Last attempt: {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(data["checked"]))}', file=sys.stderr)
        report = data.get('lines', ['No results yet; background check requested.'])
        fingerprint = hashlib.sha256(json.dumps(sorted(report)).encode()).hexdigest()
        seen_path = cache_path().with_suffix('.displayed.json')
        changed = not data or read(seen_path).get('lines') != [fingerprint]
        if changed:
            for line in report[:10]:
                # Remote/cache text must never inject terminal control sequences.
                print('  ' + ''.join(c for c in line if c.isprintable()), file=sys.stderr)
            if len(report) > 10:
                print('  Full report: t system advisories status', file=sys.stderr)
            if data:
                write(seen_path, {'schema': SCHEMA, 'checked': now, 'expires': now + DAY,
                                  'lines': [fingerprint]})
        else:
            print('  No changed advisory results; full report: t system advisories status', file=sys.stderr)
        if not fresh:
            env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
            subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, cwd='/', env=env)
    except (OSError, OverflowError, ValueError):
        print('t advisories: unavailable (cache or worker error); coverage unknown.', file=sys.stderr)


if __name__ == '__main__':
    with contextlib.suppress(OSError):
        check()
