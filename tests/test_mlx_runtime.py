"""Private MLX runtime ownership and admission; no real weights or processes."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest
from conftest import load_script


@pytest.fixture
def mlx(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    m = load_script(Path(__file__).parents[1] / 'libexec/t_mlx_runtime.py', 'mlx_runtime_test')
    monkeypatch.setattr(m.sys, 'platform', 'darwin')
    monkeypatch.setattr(m.platform, 'machine', lambda: 'arm64')
    return m


def test_install_is_explicit_and_versioned(mlx, monkeypatch):
    m = mlx
    with pytest.raises(ValueError, match='missing'):
        m.available()
    binary = m.python()
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=m.VERSION))
    assert m.available() == binary
    assert m.install() == binary
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='wrong'))
    with pytest.raises(ValueError, match='unverified'):
        m.available()
    monkeypatch.setattr(m.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='Apple Silicon'):
        m.install()


def test_stop_requires_exact_owned_process(mlx, monkeypatch):
    m = mlx
    calls = []
    monkeypatch.setattr(m, 'helper', lambda name: {'process_identity': lambda pid: 'new-process'})
    monkeypatch.setattr(m.os, 'killpg', lambda *a: calls.append(a))
    m.stop({'owned': True, 'pid': 123, 'process_identity': 'old-process'})
    assert calls == []
    with pytest.raises(ValueError, match='unowned'):
        m.stop({'owned': False})
    alive = [True, True, False, False]
    monkeypatch.setattr(m, 'alive', lambda instance: alive.pop(0))
    m.stop({'owned': True, 'pid': 123})
    assert calls == [(123, m.signal.SIGTERM)]


def test_ready_checks_authenticated_identity_and_context(mlx, monkeypatch):
    m = mlx
    instance = dict(base='http://127.0.0.1:9', api_key='secret', alias='/models/exact', context=32768)
    monkeypatch.setattr(m, 'alive', lambda x: True)
    view = dict(loaded_model=instance['alias'], configured_context_limit=32768, preload_failures={})
    calls = []
    monkeypatch.setattr(m, 'helper', lambda name: {'request': lambda *a, **k: calls.append((a, k)) or view})
    assert m.ready(instance)
    assert calls[0][1]['headers'] == {'Authorization': 'Bearer secret'}
    view['configured_context_limit'] = 8192
    assert not m.ready(instance)
    view['configured_context_limit'] = 32768
    view['loaded_model'] = '/models/other'
    assert not m.ready(instance)


def test_start_journals_before_readiness_and_uses_private_endpoint(mlx, monkeypatch):
    m = mlx
    calls = []
    monkeypatch.setattr(m, 'available', lambda: Path('/runtime/python'))
    proc = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(m.subprocess, 'Popen', lambda args, **kw: calls.append((args, kw)) or proc)
    monkeypatch.setattr(m, 'helper', lambda name: {'process_identity': lambda pid: 'identity'})
    instance = dict(alias='/models/exact', context=32768)
    journal = []
    monkeypatch.setattr(m, 'ready', lambda value: bool(journal))
    m.start(instance, lambda: journal.append(dict(instance)))
    args, kw = calls[0]
    assert args[args.index('--host') + 1] == '127.0.0.1'
    assert '--trust-remote-code' not in args
    assert args[args.index('--max-num-seqs') + 1] == '1'
    assert kw['env']['HF_HUB_OFFLINE'] == '1'
    assert kw['env']['MLX_VLM_SERVER_API_KEY'] == instance['api_key']
    assert journal[0]['process_identity'] == 'identity'
    assert kw['start_new_session']


def test_failed_start_stops_owned_server(mlx, monkeypatch):
    m = mlx
    monkeypatch.setattr(m, 'available', lambda: Path('/runtime/python'))
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(pid=123, poll=lambda: 1))
    monkeypatch.setattr(m, 'helper', lambda name: {'process_identity': lambda pid: 'identity'})
    stopped = []
    monkeypatch.setattr(m, 'stop', lambda value: stopped.append(value['pid']))
    with pytest.raises(ValueError, match='did not confirm'):
        m.start(dict(alias='/models/exact', context=32768), lambda: None)
    assert stopped == [123]
