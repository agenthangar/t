#!/usr/bin/env python3
"""Private, bounded macOS Wi-Fi observations. Never join or change radio settings."""

import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import shutil
import signal
import sys
import tempfile
import time

from nosleep_hotspot import Runner, HotspotError, Cancelled, validate_ssid

SOURCE = Path(__file__).with_suffix('.swift')
MESSAGES = {
    'setup': 'Allow Location access to find nearby Wi-Fi',
    'permission': 'Allow Location access to find nearby Wi-Fi',
    'denied': 'Allow Nosleep Wi-Fi in System Settings → Privacy & Security → Location Services',
    'disabled': 'Turn on Location Services in System Settings → Privacy & Security',
    'off': 'Wi-Fi off',
    'missing': 'No Wi-Fi interface',
    'unsupported': 'Network discovery requires macOS',
    'unknown': 'Network availability unknown; refresh to retry',
    'ok': 'Nearby Wi-Fi networks',
}
REASONS = {
    'scan_timeout': 'Wi-Fi scan timed out after 30s; retry',
    'access_timeout': 'Wi-Fi access check timed out; retry',
    'empty_scan': 'Wi-Fi scan returned no networks; retry',
    'scan_failed': 'macOS could not complete the Wi-Fi scan; retry',
    'launch_failed': 'Wi-Fi discovery helper failed; retry',
    'invalid_response': 'Wi-Fi discovery returned an invalid response; retry',
}


def message(data):
    return REASONS.get(data.get('reason'), MESSAGES[data['state']]) if data['state'] == 'unknown' else MESSAGES[data['state']]


def helper_path():
    # A linked worktree must never replace the installed release's helper.
    root = SOURCE.resolve().parent.parent
    scope = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    base = Path.home() / 'Library/Application Support/t/nosleep' / scope
    return base / 'Nosleep Wi-Fi.app'


def build_helper():
    if sys.platform != 'darwin':
        raise ValueError(MESSAGES['unsupported'])
    app = helper_path()
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    stamp = app / 'Contents/source.sha256'
    if stamp.is_file() and stamp.read_text() == digest and (app / 'Contents/MacOS/NosleepWiFi').is_file():
        return app
    if not shutil.which('swiftc') or not shutil.which('codesign'):
        raise ValueError('Install Apple command-line tools with xcode-select --install, then enable discovery again')
    app.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.wifi-build-', dir=app.parent) as directory:
        staged = Path(directory) / app.name
        contents = staged / 'Contents'
        executable = contents / 'MacOS/NosleepWiFi'
        executable.parent.mkdir(parents=True)
        info = {'CFBundleIdentifier': 'com.agenthangar.nosleep.wifi.' + app.parent.name,
                'CFBundleName': 'Nosleep Wi-Fi', 'CFBundleExecutable': 'NosleepWiFi',
                'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSUIElement': True,
                'NSLocationUsageDescription': 'Show nearby Wi-Fi networks and whether your selected hotspot is available.'}
        (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
        runner = Runner(120)
        try:
            runner.run(['/usr/bin/xcrun', 'swiftc', str(SOURCE), '-o', str(executable),
                        '-framework', 'AppKit', '-framework', 'CoreWLAN', '-framework', 'CoreLocation'])
            runner.run(['/usr/bin/codesign', '--force', '--sign', '-', str(staged)])
        except Cancelled:
            raise KeyboardInterrupt from None
        except (OSError, HotspotError) as exc:
            raise ValueError('Could not build Wi-Fi helper; install Apple command-line tools and retry') from exc
        finally:
            runner.finish()
        (contents / 'source.sha256').write_text(digest)
        # Build completely before replacing. An unchanged helper retains its identity.
        if app.exists():
            shutil.rmtree(app)
        os.rename(staged, app)
    return app


def normalize(data):
    """Validate native output; never equate hidden/redacted SSIDs with absence."""
    state = data.get('state', 'unknown')
    if state not in MESSAGES:
        state = 'unknown'
    networks = {}
    redacted = False
    for row in data.get('networks', []):
        try:
            ssid = validate_ssid(row['ssid'])
            if ssid.lower() in ('<redacted>', 'redacted'):
                raise ValueError('redacted')
            strength = int(row.get('rssi', -100))
            networks[ssid] = max(strength, networks.get(ssid, -1000))
        except (KeyError, TypeError, ValueError):
            redacted = True
    current = data.get('current', '')
    try:
        current = validate_ssid(current)
        if current.lower() in ('<redacted>', 'redacted'):
            raise ValueError('redacted')
    except ValueError:
        redacted = redacted or bool(current)
        current = ''
    if redacted and state == 'ok':
        state = 'unknown'
    observed_at = data.get('observed_at', time.time())
    if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
        observed_at = time.time()
    reason = data.get('reason')
    if not isinstance(reason, str) or reason not in REASONS:
        reason = ''
    return {'state': state, 'current': current,
            'reason': reason,
            'networks': [{'ssid': ssid, 'rssi': strength} for ssid, strength in
                         sorted(networks.items(), key=lambda row: (row[0] != current, -row[1], row[0]))],
            'observed_at': min(time.time(), observed_at)}


def observe(authorize=False, *, progress=None):
    if progress is not None:
        progress('preparing' if authorize else 'checking', 0)
    if sys.platform != 'darwin':
        return normalize({'state': 'unsupported'})
    app = build_helper() if authorize else helper_path()
    if not (app / 'Contents/MacOS/NosleepWiFi').is_file():
        return normalize({'state': 'setup'})
    stamp = app / 'Contents/source.sha256'
    if not stamp.is_file() or stamp.read_text() != hashlib.sha256(SOURCE.read_bytes()).hexdigest():
        return normalize({'state': 'setup'})
    # LaunchServices gives the app its own permission identity. The app watches
    # this private request directory and its owner's PID; removing it cancels
    # native work, including if open is interrupted before the app starts.
    with tempfile.TemporaryDirectory(prefix='nosleep-wifi-') as directory:
        request = Path(directory)
        (request / 'request.json').write_text(json.dumps({'authorize': authorize, 'owner': os.getpid()}))
        # Budget covers permission (120s) or access (10s), scan (30s), and launch.
        runner = Runner(155 if authorize else 45)
        phase, phase_started, last_tick = 'checking', time.monotonic(), None

        def report_progress():
            nonlocal phase, phase_started, last_tick
            try:
                native = json.loads((request / 'progress.json').read_text()).get('phase')
            except (OSError, ValueError, AttributeError):
                native = None
            if native in ('permission', 'scanning') and native != phase:
                phase, phase_started = native, time.monotonic()
            tick = (phase, int(time.monotonic() - phase_started))
            if tick != last_tick:
                progress(*tick)
                last_tick = tick

        try:
            runner.run(['/usr/bin/open', '-n', '-g', '-W', str(app), '--args', str(request)],
                       **({'on_wait': report_progress} if progress is not None else {}))
            try:
                return normalize(json.loads((request / 'response.json').read_text()))
            except (OSError, ValueError, TypeError, AttributeError):
                return normalize({'state': 'unknown', 'reason': 'invalid_response'})
        except Cancelled:
            raise KeyboardInterrupt from None
        except (OSError, HotspotError):
            return normalize({'state': 'unknown', 'reason': 'launch_failed'})
        finally:
            runner.finish()


def availability(data, ssid, now=None):
    if not ssid:
        return 'Not configured'
    if (time.time() if now is None else now) - data.get('observed_at', 0) > 45:
        return 'Unknown (observation expired)'
    state = data.get('state', 'unknown')
    if state in ('ok', 'unknown'):
        if data.get('current') == ssid:
            return 'Connected'
        if any(row['ssid'] == ssid for row in data.get('networks', [])):
            return 'Available · not connected'
        if state == 'ok':
            return 'Not detected'
    return {'off': 'Wi-Fi off', 'missing': 'No Wi-Fi interface',
            'permission': 'Permission needed · choose Hotspot network in config',
            'denied': 'Permission denied · choose Hotspot network in config',
            'disabled': 'Location Services off', 'setup': 'Choose Hotspot network in config'}.get(state, 'Unknown')


def main():
    # Internal runtime worker: it only emits a safe, single-line observation.
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        print(availability(observe(), sys.argv[1] if len(sys.argv) > 1 else ''))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
