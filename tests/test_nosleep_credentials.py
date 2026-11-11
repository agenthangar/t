"""Credential access is mocked: no personal Keychain or macOS prompts in tests."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

LIB = Path(__file__).resolve().parents[1] / 'libexec'


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.syspath_prepend(str(LIB))
    spec = importlib.util.spec_from_file_location('test_credentials', LIB / 'nosleep_credentials.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    return module


def test_listing_only_requests_metadata_and_normalizes(credentials, monkeypatch):
    calls = []
    def run(*args):
        calls.append(args)
        return {'state': 'ok', 'current': 'Phone', 'networks': [
            {'ssid': 'Phone', 'source': 'system'}, {'ssid': 'Phone', 'source': 'nosleep'},
            {'ssid': 'Phone', 'source': 'system'}, {'ssid': 'Café', 'source': 'system'},
            {'ssid': '<redacted>', 'source': 'system'}, {'ssid': 'bad\n', 'source': 'system'},
            {'ssid': 'Other', 'source': 'unknown'}, {}, None]}
    monkeypatch.setattr(credentials, '_run', run)
    assert credentials.saved_networks() == {'state': 'ok', 'current': 'Phone', 'networks': [
        {'ssid': 'Café', 'source': 'system'}, {'ssid': 'Phone', 'source': 'nosleep'}]}
    assert calls == [('list',)]


@pytest.mark.parametrize('data', [None, {'networks': None}, {'state': 'denied'},
                                 {'state': 'ok', 'current': '<redacted>'}])
def test_listing_missing_metadata_is_never_reported_as_verified_password(credentials, monkeypatch, data):
    monkeypatch.setattr(credentials, '_run', lambda *_: data)
    if data is None:
        # Internal _run rejects non-dictionary native data before this boundary.
        monkeypatch.setattr(credentials, '_run', lambda *_: (_ for _ in ()).throw(ValueError('private detail')))
    result = credentials.saved_networks()
    assert result['current'] == '' and result['networks'] == []
    assert result['state'] == ('ok' if data and data.get('state') == 'ok' else 'unavailable')


def test_listing_unsupported_never_builds(credentials, monkeypatch):
    monkeypatch.setattr(credentials.sys, 'platform', 'linux')
    assert credentials.saved_networks()['state'] == 'unsupported'
    with pytest.raises(ValueError, match='macOS'):
        credentials.build_helper()


@pytest.mark.parametrize('source,operation', [('system', 'reuse'), ('nosleep', 'verify')])
def test_selected_saved_password_only_reused_on_save(credentials, monkeypatch, source, operation):
    calls = []
    monkeypatch.setattr(credentials, '_run', lambda *args: calls.append(args) or {'status': 0})
    credentials.save_credentials('Phone', source=source)
    assert calls == [(operation, 'Phone')]


def test_entered_password_goes_through_pipe_not_arguments(credentials, monkeypatch):
    calls = []
    monkeypatch.setattr(credentials, 'build_helper', lambda: Path('/private/helper'))
    class Runner:
        def __init__(self, timeout): calls.append(timeout)
        def run(self, argv, **kwargs):
            calls.append((argv, kwargs))
            return b'{"status":0}'
        def finish(self): calls.append('finished')
    monkeypatch.setattr(credentials, 'Runner', Runner)
    credentials.save_credentials('Phone', password=' fixture secret ')
    assert calls == [120, (['/private/helper', 'store', 'Phone'], {'input': b' fixture secret '}), 'finished']


@pytest.mark.parametrize('password', ['', b'', b'bad\n', b'bad\r', b'bad\0', b'x'*1025, 42])
def test_invalid_secret_is_rejected_before_access(credentials, monkeypatch, password):
    monkeypatch.setattr(credentials, '_run', lambda *_: pytest.fail('no native access'))
    with pytest.raises(ValueError):
        credentials.save_credentials('Phone', password=password)


def test_invalid_selections_and_native_status(credentials, monkeypatch):
    with pytest.raises(ValueError): credentials.save_credentials('Phone')
    with pytest.raises(ValueError): credentials.save_credentials('Phone', password='abc', source='system')
    with pytest.raises(ValueError): credentials.save_credentials('bad\n', source='system')
    for status in (None, 1, True):
        monkeypatch.setattr(credentials, '_run', lambda *_, **kw: {'status': status})
        with pytest.raises(ValueError, match='Unlock Keychain'): credentials.save_credentials('Phone', password=b'abc')


@pytest.mark.parametrize('failure', ['cancel', 'os', 'native', 'json', 'shape'])
def test_native_failures_sanitized_and_children_finished(credentials, monkeypatch, failure):
    finished = []
    monkeypatch.setattr(credentials, 'build_helper', lambda: Path('/private/helper'))
    class Runner:
        def __init__(self, timeout): assert timeout == 10
        def run(self, argv, **kwargs):
            if failure == 'cancel': raise credentials.Cancelled('private')
            if failure == 'os': raise OSError('private')
            if failure == 'native': raise credentials.HotspotError('private')
            return b'[]' if failure == 'shape' else b'private'
        def finish(self): finished.append(True)
    monkeypatch.setattr(credentials, 'Runner', Runner)
    with pytest.raises(KeyboardInterrupt if failure == 'cancel' else ValueError) as error:
        credentials._run('list')
    assert 'private' not in str(error.value)
    assert finished == [True]


def test_build_cached_private_and_bounded(credentials, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    target = credentials.helper_path()
    assert target.parent.parent == tmp_path / 'Library/Application Support/t/nosleep'
    monkeypatch.setattr(credentials.shutil, 'which', lambda _: '/tools/swiftc')
    calls = []
    class Runner:
        def __init__(self, timeout): assert timeout == 120
        def run(self, argv):
            calls.append(argv)
            Path(argv[argv.index('-o')+1]).write_bytes(b'fixture binary')
        def finish(self): calls.append('finished')
    monkeypatch.setattr(credentials, 'Runner', Runner)
    assert credentials.build_helper() == target
    assert target.stat().st_mode & 0o777 == 0o700
    assert credentials.build_helper() == target
    assert len(calls) == 2


@pytest.mark.parametrize('mode', ['missing', 'cancel', 'failure'])
def test_build_failures_never_publish_partial_binary(credentials, monkeypatch, tmp_path, mode):
    monkeypatch.setattr(credentials, 'helper_path', lambda: tmp_path / 'helper')
    monkeypatch.setattr(credentials.shutil, 'which', lambda _: None if mode == 'missing' else '/tools/swiftc')
    finished = []
    class Runner:
        def __init__(self, timeout): pass
        def run(self, argv):
            if mode == 'cancel': raise credentials.Cancelled('private')
            raise credentials.HotspotError('private')
        def finish(self): finished.append(True)
    monkeypatch.setattr(credentials, 'Runner', Runner)
    with pytest.raises(KeyboardInterrupt if mode == 'cancel' else ValueError) as error:
        credentials.build_helper()
    assert 'private' not in str(error.value)
    assert not (tmp_path / 'helper').exists()
    assert finished == ([] if mode == 'missing' else [True])


def test_native_source_scopes_metadata_and_never_outputs_secrets(credentials):
    source = credentials.SOURCE.read_text()
    listing = source.split('func listNetworks()')[1].split('let args =')[0]
    assert 'kSecReturnData' not in listing
    assert 'kSecReturnAttributes' in listing and 'AirPort network password' in listing
    assert 'SecKeychainSetUserInteractionAllowed(false)' in listing
    assert 'CWKeychainFindWiFiPassword(.user' in source
    assert 'CWKeychainFindWiFiPassword(.system' in source
    assert 'standardInput.readDataToEndOfFile' in source
    assert 'SecItemDelete' not in source
