"""Opt-in, local installed-tool advisories; no remediation or repository inspection."""
import contextlib
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

DAY = 86400
RETRY = 3600
API = 'https://api.github.com/repos/cloudflare/cloudflared/security-advisories?per_page=100'
CERT = 'https://blog.cloudflare.com/emudhra-certificate-compromise/'
VERSION = r'\d{1,4}\.\d{1,3}\.\d{1,3}'


def version(value):
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise ValueError('unsupported version')
    return tuple(map(int, value.split('.')))


def matches(installed, expression):
    """Only numeric AND ranges are supported; never guess at unknown syntax."""
    result = True
    for part in expression.split(','):
        match = re.fullmatch(r'\s*(<=|>=|<|>|=)\s*(' + VERSION + r')\s*', part)
        if not match:
            raise ValueError('unsupported affected range')
        left, right = version(installed), version(match[2])
        result &= {'<': left < right, '<=': left <= right, '>': left > right,
                   '>=': left >= right, '=': left == right}[match[1]]
    return result


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
            if not stat.S_ISREG(info.st_mode) or info.st_size > 131072:
                return {}
            raw = stream.read(131073)
        if len(raw) > 131072:
            return {}
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('schema') != 1
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


def fetch():
    request = urllib.request.Request(API, headers={'User-Agent': 't-advisory-check',
                                                  'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576 or response.headers.get('Link'):
            raise ValueError('advisory response incomplete')
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise ValueError('invalid advisory response')
    return data


def evaluate(installed, system, advisories):
    lines = []
    # Vendor remediation recommendation, not a claim that every older build is exploitable.
    if system in ('Linux', 'Darwin') and version(installed) < version('2026.10.2'):
        lines.append(f'cloudflared {installed}: vendor recommends 2026.10.2 or newer for '
                     f'the eMudhra certificate incident (older builds); review/update: {CERT}')
    for advisory in advisories:
        ghsa = advisory.get('ghsa_id', '')
        if not isinstance(ghsa, str) or not re.fullmatch(r'GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}', ghsa):
            raise ValueError('invalid advisory identifier')
        url = 'https://github.com/cloudflare/cloudflared/security/advisories/' + ghsa
        if advisory.get('withdrawn_at'):
            continue
        vulnerabilities = advisory.get('vulnerabilities')
        if not isinstance(vulnerabilities, list) or not vulnerabilities:
            lines.append(f'cloudflared {installed}: {ghsa}: coverage unknown; review {url}')
            continue
        for item in vulnerabilities:
            if not isinstance(item, dict):
                raise ValueError('invalid advisory package')
            package = item.get('package') or {}
            # Preserve upstream platform labels exactly. Do not reinterpret an
            # empty ecosystem as all operating systems (older cloudflared GHSA).
            if not isinstance(package, dict):
                raise ValueError('invalid advisory package')
            ecosystem = package.get('ecosystem')
            if ecosystem == 'windows 32-bit' and system in ('Linux', 'Darwin'):
                continue
            affected = item.get('vulnerable_version_range')
            fixed = item.get('patched_versions')
            if ecosystem not in ('Linux', 'macOS') or package.get('name') != 'cloudflared':
                lines.append(f'cloudflared {installed}: {ghsa}: platform/package coverage unknown; review {url}')
                continue
            if ecosystem != {'Linux': 'Linux', 'Darwin': 'macOS'}.get(system):
                continue
            try:
                hit = matches(installed, affected)
                if fixed is not None:
                    version(fixed)
            except (ValueError, TypeError, AttributeError):
                lines.append(f'cloudflared {installed}: {ghsa}: version coverage unknown; review {url}')
                continue
            if hit:
                lines.append(f'cloudflared {installed}: {ghsa}, affected {affected}; '
                             f'fixed {fixed or "unknown"}; review/update: {url}')
    return lines or [f'cloudflared {installed}: no known findings in the checked sources; not a system security verdict.']


def collect():
    system = platform.system()
    if system not in ('Linux', 'Darwin'):
        return ['Unsupported platform; installed tools not checked.'], False
    executable = shutil.which('cloudflared')
    if not executable:
        return ['cloudflared not found on PATH; other tools are not covered.'], False
    result = subprocess.run([executable, '--version'], capture_output=True, text=True,
                            timeout=3, check=False, cwd='/')
    match = re.fullmatch(r'cloudflared version (' + VERSION + r')(?: \(built [^\r\n]*\))?\s*', result.stdout)
    if result.returncode or not match:
        return ['cloudflared version unknown; unable to match advisories.'], True
    installed = match[1]
    # Keep the source-reviewed incident recommendation even when offline.
    try:
        advisories = fetch()
    except (OSError, ValueError, http.client.HTTPException):
        lines = evaluate(installed, system, [])
        lines = [line for line in lines if 'no known findings' not in line]
        return lines + [f'cloudflared {installed}: GitHub advisories unavailable; coverage incomplete (retry in one hour).'], True
    return evaluate(installed, system, advisories), False


def check():
    if os.environ.get('T_VULNERABILITY_CHECK') != '1':
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
        if now <= old.get('expires', 0) <= now + DAY:
            return
        try:
            lines, error = collect()
        except (OSError, ValueError, subprocess.SubprocessError):
            lines, error = ['Check failed; installed-tool coverage unknown (retry in one hour).'], True
        if error and old:
            previous = old.get('last_success', old['lines'] if not old.get('error') else [])
            if previous:
                lines += ['Previous results (stale):'] + previous[:20]
        else:
            previous = []

        write(path, {'schema': 1, 'checked': now, 'expires': now + (RETRY if error else DAY),
                     'lines': lines, 'error': error,
                     'last_success': previous[:20] if error else lines[:20]})


def startup():
    """Foreground: bounded local cache read and detached worker; no probes/network."""
    if os.environ.get('T_VULNERABILITY_CHECK') != '1':
        return
    try:
        data = read(cache_path())
        now = time.time()
        fresh = now <= data.get('expires', 0) <= now + DAY
        label = 'cached' if fresh else 'stale' if data else 'pending'
        print(f't advisories ({label}; local cloudflared only):', file=sys.stderr)
        if data:
            print(f'  Last attempt: {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(data["checked"]))}', file=sys.stderr)
        for line in data.get('lines', ['No results yet; background check requested.']):
            # Remote/cache text must never inject terminal control sequences.
            print('  ' + ''.join(c for c in line if c.isprintable()), file=sys.stderr)
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
