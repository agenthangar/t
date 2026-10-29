"""Private parser support is available without installing packages on the host."""
import hashlib
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / 'libexec/t_toml.py'


def test_stdlib_and_bundled_parser_agree(monkeypatch):
    standard = runpy.run_path(str(ADAPTER))
    sample = 'model="test"\n[agents]\nvalues=[1,2]\nactive=true\n'
    expected = standard['loads'](sample)
    monkeypatch.setitem(sys.modules, 'tomllib', None)
    bundled = runpy.run_path(str(ADAPTER))
    assert bundled['loads'](sample) == expected
    with pytest.raises(bundled['TOMLDecodeError']): bundled['loads']('bad=[')
    assert 'tomli' not in sys.modules  # no global-package shadowing


def test_vendor_hashes_and_license_are_shipped():
    vendor = ROOT / 'libexec/vendor'
    manifest = (vendor / 'SHA256SUMS').read_text()
    assert '__pycache__' not in manifest and '.pyc' not in manifest
    for line in manifest.splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest
    assert 'MIT License' in (vendor / 'tomli-LICENSE').read_text()
    readme = (vendor / 'README.md').read_text()
    assert 'Mistune 3.3.4' in readme and 'Two local compatibility changes' in readme and 'Tomli 2.2.1' in readme


def test_system_python_can_parse_without_pip(tmp_path):
    executable = Path('/usr/bin/python3')
    if not executable.exists(): pytest.skip('system Python unavailable')
    code = 'import runpy,sys; p=runpy.run_path(sys.argv[1]); assert p["loads"]("model=\\\"test\\\"")=={"model":"test"}'
    result = subprocess.run([str(executable), '-I', '-c', code, str(ADAPTER)],
                            env={'HOME':str(tmp_path),'PATH':'/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
