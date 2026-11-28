"""Focused close actions update cached views, not session discovery."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import load_script


@pytest.fixture
def close(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "libexec"))
    return load_script(root / "libexec/t_picker_close.py", "picker_close")

A = 'local\tapi-2\tdetached\tAlpha'
B = 'local\tweb-4\tapp\tBeta'


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    for name in ('all', 'default', 'current', 'rows-0', 'rows-1'):
        (tmp_path / name).write_text(A + '\n' + B + '\n')
    (tmp_path / 'current-header').write_text('Enter open/k kill/Esc\n')
    monkeypatch.setenv('T_PICKER_HEADER_WIDTH', '80')
    monkeypatch.setenv('T_PICKER_KILL_CALLBACK', '/private/close.zsh')
    return tmp_path


def test_prune_updates_every_view_and_raw_metadata(snapshot, monkeypatch, close):
    (snapshot / 'source').write_text('local\tsid\t/path\tapi-2\tdetached\tx\tx\n'
                                   'local\t-\t/path\tweb-4\tapp\tx\tx\n')
    calls = []
    monkeypatch.setattr(close, 'render', lambda *args: calls.append(args))
    close.prune(snapshot, B)
    for name in ('all', 'default', 'current', 'rows-0', 'rows-1'):
        assert (snapshot / name).read_text() == A + '\n'
    assert 'web-4' not in (snapshot / 'source').read_text()
    assert calls[0][:4] == (snapshot, 'open', 'current', 80)


@pytest.mark.parametrize('code,output,message', [(0, '', 'web-4 closed'),
    (1, 'detail\nrefused\n', 'refused'), (1, '', 'failed; row kept'),
    (130, '', 'canceled; row kept'), (-2, '', 'canceled; row kept')])
def test_close_dispatches_only_focus_and_prunes_only_on_success(snapshot, monkeypatch, code, output, message, close):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(close.subprocess, 'run', run)
    workers = []
    monkeypatch.setattr(close.subprocess, 'Popen', lambda args, **kw:
                        workers.append((args, kw)) or SimpleNamespace(pid=42))
    monkeypatch.setenv('COV_CORE_SOURCE', 'must not inherit')
    close.close(snapshot, B)
    assert calls == [['zsh', '-f', '/private/close.zsh', B]]
    assert (B in (snapshot / 'all').read_text()) == (code != 0)
    assert message in (snapshot / 'current-header').read_text()
    assert workers[0][0][-1] == str(int(code == 0))
    assert 'COV_CORE_SOURCE' not in workers[0][1]['env']
    assert workers[0][1]['stdin'] == subprocess.DEVNULL


def test_close_invalid_identity_and_interrupt_keep_snapshot(snapshot, monkeypatch, close):
    monkeypatch.setattr(close.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(pid=42))
    def interrupt(*a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(close.subprocess, 'run', interrupt)
    close.close(snapshot, 'unknown')
    assert 'unavailable' in (snapshot / 'current-header').read_text()
    close.close(snapshot, B)
    assert 'canceled' in (snapshot / 'current-header').read_text()
    assert (snapshot / 'all').read_text() == A + '\n' + B + '\n'
    close.status(snapshot, '\x1b[31mfailed\x1b[0m\x07')
    assert (snapshot / 'current-header').read_text().startswith('Close: failed\n')


@pytest.mark.parametrize('changed', [False, True])
def test_update_restores_hidden_marks_query_and_focus(snapshot, monkeypatch, changed, close):
    requests = []
    class Picker:
        def __init__(self, count):
            assert count == 3
        def request(self, actions=None):
            requests.append(actions)
            return dict(query='Beta)+abort', selected=[dict(text=A), dict(text=B)], position=1)
        def wait(self, query, total):
            return dict(matches=[dict(text=A)], matchCount=0)
    monkeypatch.setattr(close, 'Picker', Picker)
    close.update(snapshot, changed)
    assert 'rebind(enter)' in requests[-1]
    if changed:
        assert (snapshot / 'action-query').read_text() == 'Beta)+abort'
        assert any('pos(1)+select' in (action or '') for action in requests)
        assert not any('Beta' in (action or '') for action in requests)
    else:
        assert not any('reload' in (action or '') for action in requests)


def test_update_failure_leaves_accept_disabled(snapshot, monkeypatch, close):
    class Picker:
        def __init__(self, count):
            pass
        def request(self, actions=None):
            raise OSError('gone')
    monkeypatch.setattr(close, 'Picker', Picker)
    close.update(snapshot, True)
    assert 'Esc to close' in (snapshot / 'current-header').read_text()


def test_http_client_bound_loopback_and_wait(monkeypatch, close):
    monkeypatch.setenv('FZF_PORT', '1234')
    monkeypatch.setenv('FZF_API_KEY', 'secret')
    picker = close.Picker(9)
    requests = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"query":"a", "totalCount":1, "reading":false, "progress":100}'
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()
    picker.opener = Opener()
    assert picker.wait('a', 1)['totalCount'] == 1
    picker.request('pos(1)')
    assert requests[0].full_url == 'http://127.0.0.1:1234/?limit=9'
    assert requests[1].data == b'pos(1)'
    assert requests[1].get_header('X-api-key') == 'secret'
    picker.deadline = 0
    with pytest.raises(TimeoutError):
        picker.request()


def test_refresh_replaces_metadata_and_remembers_changed_owner(snapshot, monkeypatch, close):
    import json
    old = 'local\tapi-2\tdetached\tOld title\tOld title\t17/$1'
    new = 'local\tapi-2\tdetached\tNew title\tNew title\t17/$1'
    source = 'local\tnew-id\t/path\tapi-2\tdetached\tactive\tNew title\tcodex'
    for name in ('source', 'all', 'rows-0', 'rows-1'):
        (snapshot / name).write_text(old + '\n')
    (snapshot / 'incoming.json').write_text(json.dumps(dict(
        source=[source], display=[new], heading='SESSION TITLE',
        replaced=[['local', 'api-2']], full=True)))
    monkeypatch.setattr(close, 'render', lambda *args: None)
    monkeypatch.setattr(close, 'start_update', lambda *args: None)
    close.append(snapshot, 'New')
    assert (snapshot / 'source').read_text() == source + '\n'
    assert (snapshot / 'all').read_text() == new + '\n'
    assert (snapshot / 'rows-0').read_text() == new + '\n'
    assert json.loads((snapshot / 'replaced').read_text()) == [['local', 'api-2']]
    assert (snapshot / 'action-query-locked').read_text() == 'New'


def test_completion_refresh_uses_each_source_host(close, monkeypatch):
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout='api-2\n')
    monkeypatch.setattr(close.subprocess, 'run', run)
    source = 'local\tsid\t/local\tapi-2\tdetached\tactive\tTask\tcodex\nmini\tother\t/remote\tapi-2\tdetached\tactive\tTask\tclaude\n'
    assert close.completed_rows(SimpleNamespace(hosts={'mini': 'user@mini'}), source) == [
        ['local', 'api-2'], ['mini', 'api-2']]
    assert calls[0][0][:2] == ['zsh', '-lic']
    assert calls[1][0][0] == 'ssh' and 'user@mini' in calls[1][0]
    assert calls[0][1]['input'].startswith('sid\t/local')
    assert calls[1][1]['input'].startswith('other\t/remote')


def test_metadata_probe_animates_existing_header_and_stops(snapshot, close, monkeypatch):
    import threading
    painted = threading.Event()
    commands = []
    picker = SimpleNamespace(request=lambda command: commands.append(command))
    monkeypatch.setattr(close, 'render', lambda *args: painted.set())
    with close.refreshing_header(snapshot, picker, 'open', {'hosts': []}):
        assert painted.wait(2)
    assert commands and all(command == close.HEADER for command in commands)
    count = len(commands)
    threading.Event().wait(.2)
    assert len(commands) == count
