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
    monkeypatch.setattr(wifi, 'build_helper', lambda: app)
    requests = []
    class Runner:
        def __init__(self, timeout):
            assert timeout == (125 if mode == 'authorize' else 12)
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
    assert requests and not requests[0].exists()
