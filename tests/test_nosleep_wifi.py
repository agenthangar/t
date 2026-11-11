"""Discovery uses fake native responses; no test requests desktop permissions."""
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest

LIB = Path(__file__).resolve().parents[1] / 'libexec'


@pytest.fixture
def wifi(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(LIB))
    spec = importlib.util.spec_from_file_location('tested_nosleep_wifi', LIB / 'nosleep_wifi.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('HOME', str(tmp_path))
    return module


def test_normalization_deduplicates_sorts_and_preserves_exact_ssids(wifi):
    data = wifi.normalize({'state': 'ok', 'current': 'Weak', 'networks': [
        {'ssid': 'Strong', 'rssi': -30}, {'ssid': 'Strong', 'rssi': -70},
        {'ssid': 'Weak', 'rssi': -90}, {'ssid': "📱 'phone';$()", 'rssi': -50}]})
    assert [row['ssid'] for row in data['networks']] == ['Weak', 'Strong', "📱 'phone';$()"]
    assert data['networks'][1]['rssi'] == -30
    assert wifi.availability(data, 'Weak') == 'Connected'
    assert wifi.availability(data, 'Strong') == 'Available · not connected'
    assert wifi.availability(data, 'Absent') == 'Not detected'
    assert wifi.availability(data, '') == 'Not configured'
    assert 'expired' in wifi.availability(data, 'Weak', now=time.time() + 60)


@pytest.mark.parametrize('row', [{}, {'ssid': ''}, {'ssid': '<redacted>'}, {'ssid': 'bad\nname'},
                               {'ssid': 'Valid', 'rssi': 'bad'}, {'ssid': 8}])
def test_redacted_or_malformed_network_never_means_absent(wifi, row):
    data = wifi.normalize({'state': 'ok', 'networks': [row, {'ssid': 'Visible', 'rssi': -30}]})
    assert data['state'] == 'unknown'
    assert wifi.availability(data, 'Absent') == 'Unknown'
    assert wifi.availability(data, 'Visible') == 'Available · not connected'


@pytest.mark.parametrize('state', ['permission', 'denied', 'disabled', 'off', 'missing', 'setup',
                                 'unsupported', 'unknown', 'unexpected'])
def test_permission_and_radio_states_never_claim_absence(wifi, state):
    data = wifi.normalize({'state': state})
    assert data['state'] in wifi.MESSAGES
    assert wifi.availability(data, 'Phone') != 'Not detected'


def test_redacted_current_association_is_unknown(wifi):
    assert wifi.normalize({'state': 'ok', 'current': '<redacted>'})['state'] == 'unknown'


def test_platform_and_missing_helper_never_build_or_prompt(wifi, monkeypatch):
    monkeypatch.setattr(wifi.sys, 'platform', 'linux')
    assert wifi.observe()['state'] == 'unsupported'
    with pytest.raises(ValueError, match='macOS'):
        wifi.build_helper()
    monkeypatch.setattr(wifi.sys, 'platform', 'darwin')
    assert wifi.observe()['state'] == 'setup'
    monkeypatch.setattr(wifi.shutil, 'which', lambda _: None)
    with pytest.raises(ValueError, match='command-line tools'):
        wifi.build_helper()


def test_helper_is_stable_and_worktrees_are_isolated(wifi, monkeypatch, tmp_path):
    first = wifi.helper_path()
    assert first == wifi.helper_path()
    monkeypatch.setattr(wifi, 'SOURCE', tmp_path / 'other/libexec/nosleep_wifi.swift')
    assert wifi.helper_path() != first


@pytest.mark.parametrize('failure', [False, True])
def test_build_is_lazy_and_rebuilds_only_when_source_changes(wifi, monkeypatch, tmp_path, failure):
    monkeypatch.setattr(wifi.sys, 'platform', 'darwin')
    monkeypatch.setattr(wifi.shutil, 'which', lambda _: '/usr/bin/tool')
    source = tmp_path / 'source.swift'
    source.write_text('// first')
    monkeypatch.setattr(wifi, 'SOURCE', source)
    calls = []
    class Runner:
        def __init__(self, timeout): pass
        def run(self, argv):
            calls.append(argv)
            if failure:
                raise wifi.HotspotError('private diagnostic')
            if 'swiftc' in argv:
                Path(argv[argv.index('-o') + 1]).write_text('binary')
        def finish(self): calls.append('finished')
    monkeypatch.setattr(wifi, 'Runner', Runner)
    if failure:
        with pytest.raises(ValueError, match='Could not build'):
            wifi.build_helper()
        assert not wifi.helper_path().exists()
        return
    app = wifi.build_helper()
    assert calls[-1] == 'finished'
    count = len(calls)
    assert wifi.build_helper() == app and len(calls) == count
    source.write_text('// second')
    assert wifi.build_helper() == app and len(calls) > count
    assert not list(app.parent.glob('.wifi-build-*'))


@pytest.mark.parametrize('mode', ['scan', 'authorize', 'error', 'malformed', 'interrupted', 'cancelled'])
def test_native_requests_are_private_bounded_and_cleaned(wifi, monkeypatch, mode):
    monkeypatch.setattr(wifi.sys, 'platform', 'darwin')
    app = wifi.helper_path()
    binary = app / 'Contents/MacOS/NosleepWiFi'
    binary.parent.mkdir(parents=True)
    binary.touch()
    (app / 'Contents/source.sha256').write_text(wifi.hashlib.sha256(wifi.SOURCE.read_bytes()).hexdigest())
    monkeypatch.setattr(wifi, 'build_helper', lambda: app)
    requests = []
    class Runner:
        def __init__(self, timeout):
            assert timeout == (155 if mode == 'authorize' else 45)
        def run(self, argv):
            assert argv[:4] == ['/usr/bin/open', '-n', '-g', '-W']
            request = Path(argv[-1])
            requests.append(request)
            assert request.stat().st_mode & 0o777 == 0o700
            body = json.loads((request / 'request.json').read_text())
            assert body['authorize'] == (mode == 'authorize')
            if mode == 'error': raise OSError('private error')
            if mode == 'interrupted': raise KeyboardInterrupt
            if mode == 'cancelled': raise wifi.Cancelled('cancelled')
            (request / 'response.json').write_text('bad' if mode == 'malformed' else '{"state":"ok"}')
        def finish(self): pass
    monkeypatch.setattr(wifi, 'Runner', Runner)
    if mode in ('interrupted', 'cancelled'):
        with pytest.raises(KeyboardInterrupt): wifi.observe()
    else:
        result = wifi.observe(authorize=mode == 'authorize')
        assert result['state'] == ('unknown' if mode in ('error', 'malformed') else 'ok')
        assert result['reason'] == {'error': 'launch_failed', 'malformed': 'invalid_response'}.get(mode, '')
    assert requests and not requests[0].exists()


def test_connected_network_survives_empty_or_failed_scan(wifi):
    data = wifi.normalize({'state': 'unknown', 'current': 'Connected Wi-Fi', 'networks': []})
    assert data['networks'] == []
    assert data['current'] == 'Connected Wi-Fi'
    assert wifi.availability(data, 'Connected Wi-Fi') == 'Connected'
    assert wifi.availability(data, 'Phone hotspot') == 'Unknown'


@pytest.mark.parametrize('stamp', [None, 'old-source'])
def test_old_helper_requires_explicit_setup_instead_of_running_stale_code(wifi, monkeypatch, stamp):
    monkeypatch.setattr(wifi.sys, 'platform', 'darwin')
    app = wifi.helper_path()
    binary = app / 'Contents/MacOS/NosleepWiFi'
    binary.parent.mkdir(parents=True)
    binary.touch()
    if stamp:
        (app / 'Contents/source.sha256').write_text(stamp)
    monkeypatch.setattr(wifi, 'build_helper', lambda: pytest.fail('read-only observation must not build'))
    assert wifi.observe()['state'] == 'setup'


def test_scan_timeout_keeps_association_and_its_original_age(wifi, monkeypatch):
    monkeypatch.setattr(wifi.time, 'time', lambda: 1030)
    data = wifi.normalize({'state': 'unknown', 'reason': 'scan_timeout',
                          'current': 'Connected Wi-Fi', 'observed_at': 1000})
    assert data['networks'] == []
    assert wifi.message(data) == 'Wi-Fi scan timed out after 30s; retry'
    assert wifi.availability(data, 'Connected Wi-Fi') == 'Connected'
    assert wifi.availability(data, 'Different Wi-Fi') == 'Unknown'
    assert wifi.availability(data, 'Connected Wi-Fi', now=1046) == 'Unknown (observation expired)'


@pytest.mark.parametrize('reason', ['scan_timeout', 'access_timeout', 'empty_scan',
                                  'scan_failed', 'launch_failed', 'invalid_response'])
def test_discovery_failures_explain_the_cause_without_claiming_absence(wifi, reason):
    data = wifi.normalize({'state': 'unknown', 'reason': reason})
    assert wifi.message(data) == wifi.REASONS[reason]
    assert wifi.availability(data, 'Phone') == 'Unknown'
    assert wifi.message(wifi.normalize({'state': 'permission', 'reason': reason})) == wifi.MESSAGES['permission']


@pytest.mark.parametrize('value', [None, [], 'private native details', 42])
def test_unrecognized_diagnostics_are_not_displayed(wifi, value):
    data = wifi.normalize({'state': 'unknown', 'reason': value})
    assert data['reason'] == ''
    assert wifi.message(data) == wifi.MESSAGES['unknown']


@pytest.mark.parametrize('value', [None, 'private', float('nan'), float('inf'), True, 2000])
def test_native_observation_timestamp_cannot_extend_freshness(wifi, monkeypatch, value):
    monkeypatch.setattr(wifi.time, 'time', lambda: 1000)
    data = wifi.normalize({'state': 'unknown', 'observed_at': value})
    assert data['observed_at'] == 1000


def test_progress_tracks_permission_then_scan_and_deduplicates_ticks(wifi, monkeypatch):
    monkeypatch.setattr(wifi.sys, 'platform', 'darwin')
    app = wifi.helper_path()
    binary = app / 'Contents/MacOS/NosleepWiFi'
    binary.parent.mkdir(parents=True)
    binary.touch()
    (app / 'Contents/source.sha256').write_text(wifi.hashlib.sha256(wifi.SOURCE.read_bytes()).hexdigest())
    monkeypatch.setattr(wifi, 'build_helper', lambda: app)
    now = [0]
    monkeypatch.setattr(wifi.time, 'monotonic', lambda: now[0])
    events, requests, finished = [], [], []
    class Runner:
        def __init__(self, timeout): pass
        def run(self, argv, *, on_wait):
            request = Path(argv[-1])
            requests.append(request)
            on_wait()  # Native app has not started yet.
            for body in ('invalid', '[]', '{"phase":"private untrusted text"}',
                         '{"phase":"permission"}', '{"phase":"scanning"}'):
                (request / 'progress.json').write_text(body)
                on_wait()
                on_wait()
            now[0] = 12.3
            on_wait()
            (request / 'response.json').write_text('{"state":"ok", "networks":[{"ssid":"Phone"}]}')
        def finish(self): finished.append(True)
    monkeypatch.setattr(wifi, 'Runner', Runner)
    result = wifi.observe(authorize=True, progress=lambda *args: events.append(args))
    assert events == [('preparing', 0), ('checking', 0), ('permission', 0), ('scanning', 0), ('scanning', 12)]
    assert result['state'] == 'ok' and result['networks'][0]['ssid'] == 'Phone'
    assert finished == [True] and not requests[0].exists()
