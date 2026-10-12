"""Exercise the desktop-test gate without opening a real desktop window."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("args,summary,ran_ui", [
    ([], "1 passed, 1 skipped", False),
    (["-m", "macos_ui"], "1 skipped, 1 deselected", False),
    (["tests/test_desktop.py::test_native_window"], "1 skipped", False),
    (["--run-macos-ui"], "2 passed", True),
])
def test_desktop_tests_need_explicit_opt_in_before_fixture_setup(tmp_path, args, summary, ran_ui):
    root = Path(__file__).resolve().parents[1]
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copyfile(root / "tests" / "conftest.py", tests / "conftest.py")
    shutil.copyfile(root / "pyproject.toml", tmp_path / "pyproject.toml")
    (tests / "test_desktop.py").write_text('''
from pathlib import Path
import pytest

@pytest.fixture(scope="session")
def native_window():
    # Stand in for the first native side effect, including session fixture setup.
    Path("window-opened").touch()

@pytest.mark.macos_ui
def test_native_window(native_window):
    pass

def test_headless_logic():
    pass
''')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COV_CORE_") and key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", *args],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary in result.stdout
    assert (tmp_path / "window-opened").exists() is ran_ui
    if not ran_ui:
        assert "opens desktop windows; opt in with --run-macos-ui" in result.stdout
