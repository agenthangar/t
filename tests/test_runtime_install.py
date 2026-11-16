"""Runtime installers use only synthetic binaries and sandbox homes."""
from pathlib import Path
import subprocess
import pytest
from conftest import load_script


@pytest.fixture
def installer(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('T_LOCAL_RUNTIME_ROOT', str(tmp_path / 'state'))
    return load_script(Path(__file__).parents[1] / 'libexec/t_runtime_install.py', 'runtime_install_test')


def test_inventory_does_not_launch_or_install(installer, monkeypatch):
    m = installer
    monkeypatch.setattr(m, 'installed', lambda p: p == 'mlx-vlm')
    rows = m.inventory()
    assert [r['runtime'] for r in rows] == list(m.RUNTIMES)
    assert rows[0] == {'runtime': 'mlx-vlm', 'enabled': True, 'installed': True}
    assert not (Path.home() / 'state').exists()


def test_install_already_present_and_invalid(installer, monkeypatch):
    m = installer
    monkeypatch.setattr(m, 'installed', lambda p: True)
    assert not m.install('ollama')['changed']
    assert not (Path.home() / '.local').exists()
    with pytest.raises(ValueError, match='Choose'):
        m.plan('bad')


@pytest.mark.parametrize('provider', ['mlx-vlm', 'lmstudio', 'ollama'])
def test_install_preserves_switches_and_verifies_runtime(installer, monkeypatch, provider):
    m = installer; calls = []; ready = [False]
    manager = m.helper('t_local_manager')
    manager['set_inference_enabled'](False)
    manager['set_runtime_enabled'](provider, False)
    before = {p.name: p.read_bytes() for p in (Path.home() / 'state').glob('*.json')}
    monkeypatch.setattr(m.sys, 'platform', 'darwin')
    monkeypatch.setattr(m.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(m.shutil, 'which', lambda name: '/fake/' + name)
    monkeypatch.setattr(m, 'installed', lambda p: ready[0])
    def run(command, timeout=900):
        calls.append(command); ready[0] = True
    monkeypatch.setattr(m, 'run', run)
    original = m.helper
    monkeypatch.setattr(m, 'helper', lambda name: {'VERSION': 'test', 'install': lambda runner: runner(['mlx-install'])} if name == 't_mlx_runtime' else original(name))
    result = m.install(provider)
    assert result == {'runtime': provider, 'installed': True, 'changed': True}
    assert calls and all('model' not in x for cmd in calls for x in cmd)
    assert before == {p.name: p.read_bytes() for p in (Path.home() / 'state').glob('*.json')}


@pytest.mark.parametrize('provider', ['mlx-vlm', 'lmstudio', 'ollama'])
def test_prerequisites_and_unsupported_platform(installer, monkeypatch, provider):
    m = installer
    monkeypatch.setattr(m, 'installed', lambda p: False)
    monkeypatch.setattr(m.sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='macOS and Linux'):
        m.plan(provider)
    monkeypatch.setattr(m.sys, 'platform', 'darwin')
    monkeypatch.setattr(m.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(m.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='Install'):
        m.plan(provider)


def test_failed_install_does_not_claim_success(installer, monkeypatch):
    m = installer
    monkeypatch.setattr(m, 'installed', lambda p: False)
    monkeypatch.setattr(m, 'plan', lambda p: {'installed': False})
    monkeypatch.setattr(m.sys, 'platform', 'linux')
    monkeypatch.setattr(m, 'run', lambda *a, **kw: None)
    with pytest.raises(ValueError, match='could not be verified'):
        m.install('ollama')


@pytest.mark.parametrize('failure', [KeyboardInterrupt(), subprocess.TimeoutExpired('install', 1), ValueError('failed')])
def test_cancel_and_failure_stop_installer_group(installer, monkeypatch, failure):
    m = installer; killed = []; waits = []
    class Child:
        pid = 123
        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise failure
            return 0
    monkeypatch.setattr(m.subprocess, 'Popen', lambda *a, **kw: Child())
    monkeypatch.setattr(m.os, 'killpg', lambda *a: killed.append(a))
    with pytest.raises(type(failure)):
        m.run(['installer'])
    assert killed == [(123, m.signal.SIGTERM)] and len(waits) == 2
