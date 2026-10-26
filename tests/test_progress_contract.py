"""Regression enforcement and terminal behavior for quiet blocking work."""
import io
import json
from pathlib import Path
import runpy
import time

import pytest

from test_agent_seam import zsh  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
LINT = runpy.run_path(str(ROOT / 'scripts/lint_progress.py'))


def test_progress_inventory():
    assert LINT['check']() == []


def test_progress_detects_removed_spinner_and_new_wait(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'libexec').mkdir()
    (tmp_path / 'ui').mkdir()
    (tmp_path / 'ui/progress-contract.json').write_text(json.dumps({'exceptions': {}, 'shell': []}))
    path = tmp_path / 'bin/t'
    path.write_text("@_spinner('Waiting')\ndef query():\n    return subprocess.run(['gh'])\n")
    assert not LINT['check'](tmp_path)
    path.write_text(path.read_text().replace("@_spinner('Waiting')\n", ''))
    assert 'blocking work needs _spinner' in LINT['check'](tmp_path)[0]
    path.write_text("def query():\n    with _spinner('Waiting'):\n        subprocess.run(['gh'])\n    time.sleep(10)\n")
    assert len(LINT['check'](tmp_path)) == 1
    path.write_text("@_spinner('Waiting')\ndef query():\n    def later():\n        time.sleep(10)\n")
    assert len(LINT['check'](tmp_path)) == 1


def test_progress_exception_review_and_shell_seams(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'ui').mkdir()
    (tmp_path / 'bin/t').write_text('subprocess.run(["editor"])\n')
    key = 'bin/t::subprocess.run'
    path = tmp_path / 'ui/progress-contract.json'
    contract = {'exceptions': {key: {'kind': 'passthrough', 'reason': 'Editor owns terminal'}},
                'shell': [{'file': 'bin/t', 'required': ['_t_with_progress']}]}
    path.write_text(json.dumps(contract))
    assert 'missing progress seam' in LINT['check'](tmp_path)[0]
    contract['shell'] = []
    path.write_text(json.dumps(contract))
    assert not LINT['check'](tmp_path)
    (tmp_path / 'bin/t').write_text('')
    assert 'stale progress exception' in LINT['check'](tmp_path)[0]


@pytest.mark.parametrize('mode', ['normal', 'fast', 'nested', 'dumb', 'json', 'redirected'])
def test_shared_spinner_gates_and_cleanup(monkeypatch, mode):
    class Stream(io.StringIO):
        def isatty(self): return mode != 'redirected'
    out, err = Stream(), Stream()
    monkeypatch.setattr('sys.stdout', out)
    monkeypatch.setattr('sys.stderr', err)
    monkeypatch.setattr('sys.argv', ['t', '--json'] if mode == 'json' else ['t'])
    monkeypatch.setenv('TERM', 'dumb' if mode == 'dumb' else 'xterm')
    first = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']
    second = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']
    with first('Outer'):
        if mode == 'nested':
            with second('Inner'):
                time.sleep(.4)
        elif mode != 'fast':
            time.sleep(.4)
    assert out.getvalue() == ''
    if mode in ('normal', 'nested'):
        assert 'Outer' in err.getvalue() and 'Inner' not in err.getvalue()
        assert err.getvalue().endswith('\r\x1b[2K')
    else:
        assert err.getvalue() == ''
    assert not runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['_state'].lock.locked()


def test_shell_readiness_wait_uses_spinner_without_changing_completion(zsh):
    script = '''
TERM=xterm-256color
checks=0
kill() { if [[ $1 == -0 ]]; then (( checks++ < 8 )); else builtin kill "$@"; fi; }
_t_wait_for_exit 123
print -r -- "CHECKS=$checks"
'''
    result = zsh(script, _tty=True)
    assert 'Waiting for the previous agent to exit' in result.stdout
    assert 'CHECKS=9' in result.stdout
    assert '\x1b[2K' in result.stdout
    result = zsh(script)
    assert result.stdout == 'CHECKS=9\n' and not result.stderr
