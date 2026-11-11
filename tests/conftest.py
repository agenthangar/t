"""Pytest fixtures that import the extensionless bin script as a module.

`bin/t` is an executable symlinked onto PATH by install.sh and referenced by the
`t()` zsh shim — renaming it to `.py` would break those references. So we load it
in place via importlib instead.

It guards its entrypoint with `if __name__ == "__main__"`, so importing runs only
its (side-effect-free) module-level constant setup, not the CLI.
"""

import importlib.util
import pathlib
import sys
from importlib.machinery import SourceFileLoader

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"


def pytest_addoption(parser):
    parser.addoption(
        "--run-macos-ui", action="store_true", default=False,
        help="run native macOS tests that open windows and take desktop focus",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-macos-ui"):
        return
    skip = pytest.mark.skip(reason="opens desktop windows; opt in with --run-macos-ui")
    for item in items:
        if item.get_closest_marker("macos_ui"):
            item.add_marker(skip)


def load_script(path, mod_name):
    """Import an extensionless script at `path` as a module named `mod_name`.

    spec_from_file_location returns None for a path with no recognised source
    suffix, so we hand it an explicit SourceFileLoader.
    """
    loader = SourceFileLoader(mod_name, str(path))
    spec = importlib.util.spec_from_file_location(mod_name, str(path), loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def t_mod():
    return load_script(BIN / "t", "t_bin")


@pytest.fixture(autouse=True)
def disable_recovery_watchers(monkeypatch):
    """Session tests must never start a watcher against the developer's tmux."""
    monkeypatch.setenv("T_RECOVERY_DISABLE", "1")


@pytest.fixture(autouse=True)
def isolate_automatic_trust(monkeypatch):
    """Auto-trust tests opt in explicitly; inherited personal settings must not write
    developer agent stores during unrelated desktop handoff tests.
    """
    for name in ('T_AUTO_TRUST', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope='session')
def runtime_namespaces():
    """Clean after per-test monkeypatches of socket/filesystem functions are undone."""
    import json
    import shutil
    import socket
    import time
    directories = []
    yield directories
    for directory in directories:
        root = pathlib.Path(directory) / 'state'
        path = root / 'control.sock'
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(3)
                    conn.connect(str(path))
                    conn.sendall(json.dumps({'action': 'shutdown'}).encode() + b'\n')
                    conn.recv(4096)
                deadline = time.monotonic() + 5
                while path.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
            except OSError:
                pass
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_local_runtime_manager(monkeypatch, runtime_namespaces):
    """Short, private Unix-socket namespace; never contact the user's coordinator."""
    import tempfile
    directory = tempfile.mkdtemp(prefix='t-test-runtime-', dir='/tmp')
    runtime_namespaces.append(directory)
    monkeypatch.setenv('T_LOCAL_RUNTIME_ROOT', str(pathlib.Path(directory) / 'state'))


class ManagedClientFixture:
    """Synthetic manager boundary for worker/launch unit tests; HTTP is tested separately."""
    def __init__(self, chosen, *args, **kwargs):
        import threading
        self.info = {'id': 'fixture'}
        self.closed = threading.Event()
        self.chosen = chosen
        self.base = 'http://127.0.0.1:1234/client-token'
        self.error = None
        self.last = dict(state='running', identity_verified=True, revision='a'*40,
                         runtime_model=chosen['model'], owner_verified=True)
    def __enter__(self): return self
    def __exit__(self, *args): self.closed.set()
    def waited(self): return 0


@pytest.fixture(autouse=True)
def isolate_discovery_collectors(monkeypatch):
    """Legacy scanner tests exercise uncached collectors, never the user's daemon.

    Cache integration tests explicitly clear this and provide temporary HOME/XDG.
    """
    monkeypatch.setenv('T_DISCOVERY_COLLECTOR', '1')


@pytest.fixture(autouse=True)
def isolate_discovery_artifacts(monkeypatch):
    import copy
    import runpy
    artifacts = {}
    original = runpy.run_path
    def run(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if str(path).endswith('/t_session_cache.py'):
            actual = result['request']
            def request(operation, payload=None):
                if operation != 'artifact':
                    return actual(operation, payload)
                key = str(payload['key'])
                if 'value' in payload:
                    artifacts[key] = copy.deepcopy(payload['value'])
                return {'value': copy.deepcopy(artifacts.get(key))}
            result['request'] = request
        return result
    monkeypatch.setattr(runpy, 'run_path', run)
    return artifacts
