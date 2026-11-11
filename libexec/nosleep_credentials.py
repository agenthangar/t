"""Bounded Wi-Fi-only Keychain metadata and explicitly saved credentials.

Listing never reads passwords. Selected system credentials are copied into the
nosleep service only on Save, keeping the runtime join path compatible.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from nosleep_hotspot import Cancelled, HotspotError, Runner, validate_ssid

SOURCE = Path(__file__).with_suffix('.swift')
ERROR = 'Could not access the Wi-Fi password. Unlock Keychain or enter a password and retry.'


def helper_path():
    scope = hashlib.sha256(str(SOURCE.resolve().parent.parent).encode()).hexdigest()[:12]
    return Path.home() / 'Library/Application Support/t/nosleep' / scope / 'NosleepCredentials'


def build_helper():
    if sys.platform != 'darwin':
        raise ValueError('Saved Wi-Fi credentials require macOS')
    target = helper_path()
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    stamp = target.with_suffix('.sha256')
    if target.is_file() and stamp.is_file() and stamp.read_text() == digest:
        return target
    if not shutil.which('swiftc'):
        raise ValueError('Install Apple command-line tools to read saved Wi-Fi networks')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.credential-build-', dir=target.parent) as directory:
        staged = Path(directory) / target.name
        runner = Runner(120)
        try:
            runner.run(['/usr/bin/xcrun', 'swiftc', str(SOURCE), '-o', str(staged),
                        '-framework', 'Security', '-framework', 'CoreWLAN'])
            staged.chmod(0o700)
            os.replace(staged, target)
            stamp.write_text(digest)
        except Cancelled:
            raise KeyboardInterrupt from None
        except (OSError, HotspotError):
            raise ValueError('Could not prepare saved Wi-Fi access; check Apple command-line tools') from None
        finally:
            runner.finish()
    return target


def _run(operation, ssid=None, password=None):
    executable = build_helper()
    runner = Runner(120 if operation != 'list' else 10)
    try:
        args = [str(executable), operation] + ([ssid] if ssid is not None else [])
        data = runner.run(args, **({'input': password} if password is not None else {}))
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError(ERROR)
        return result
    except Cancelled:
        raise KeyboardInterrupt from None
    except (OSError, HotspotError, ValueError):
        raise ValueError(ERROR) from None
    finally:
        runner.finish()


def saved_networks():
    if sys.platform != 'darwin':
        return {'state': 'unsupported', 'networks': [], 'current': ''}
    try:
        data = _run('list')
        networks = {}
        for row in data.get('networks', []):
            try:
                ssid = validate_ssid(row['ssid'])
                source = row['source']
                if source not in ('system', 'nosleep') or ssid.lower() in ('redacted', '<redacted>'):
                    continue
                if networks.get(ssid) != 'nosleep':
                    networks[ssid] = source
            except (KeyError, TypeError, ValueError):
                continue
        try:
            current = validate_ssid(data.get('current', ''))
            if current.lower() in ('redacted', '<redacted>'):
                current = ''
        except ValueError:
            current = ''
        return {'state': 'ok' if data.get('state') == 'ok' else 'unavailable',
                'networks': [{'ssid': name, 'source': networks[name]} for name in sorted(networks)],
                'current': current}
    except (OSError, TypeError, ValueError):
        return {'state': 'unavailable', 'networks': [], 'current': ''}


def save_credentials(ssid, *, password=None, source=None):
    validate_ssid(ssid)
    if password is not None:
        if source is not None:
            raise ValueError('Choose a password or a saved credential, not both')
        if isinstance(password, str):
            password = password.encode('utf-8')
        if not isinstance(password, bytes) or not 1 <= len(password) <= 1024 or any(c in password for c in (b'\0', b'\n', b'\r')):
            raise ValueError('Enter a password without line breaks or control separators')
        result = _run('store', ssid, password)
    elif source in ('system', 'nosleep'):
        result = _run('reuse' if source == 'system' else 'verify', ssid)
    else:
        raise ValueError('Choose a saved Wi-Fi password or enter one')
    if type(result.get('status')) is not int or result['status'] != 0:
        raise ValueError(ERROR)
