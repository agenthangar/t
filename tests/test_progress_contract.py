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


@pytest.mark.parametrize('mode', ['normal', 'fast', 'nested', 'dumb', 'json', 'quiet', 'redirected'])
def test_shared_spinner_gates_and_cleanup(monkeypatch, mode):
    class Stream(io.StringIO):
        def isatty(self): return mode != 'redirected'
    out, err = Stream(), Stream()
    monkeypatch.setattr('sys.stdout', out)
    monkeypatch.setattr('sys.stderr', err)
    monkeypatch.setattr('sys.argv', ['t', '--' + mode] if mode in ('json', 'quiet') else ['t'])
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


@pytest.mark.parametrize('delay, result_code', [(0.03, 0), (0.3, 7), (0.03, 130)])
def test_spinner_never_runs_callers_session_save_hooks(zsh, tmp_path, delay, result_code):
    temporary = tmp_path / 'spinner'
    temporary.mkdir()
    result = zsh(f'''
      autoload -Uz add-zsh-hook
      terminal_save() {{ print -u2 -- "SESSION_SAVE subshell=$ZSH_SUBSHELL"; }}
      add-zsh-hook zshexit terminal_save
      zshexit() {{ print -u2 -- "DIRECT_EXIT subshell=$ZSH_SUBSHELL"; }}
      TRAPEXIT() {{ print -u2 -- "EXIT_TRAP subshell=$ZSH_SUBSHELL"; }}
      TERM=xterm-256color
      _T_PROGRESS=1
      work() {{ sleep {delay}; print payload; print -u2 diagnostic; return {result_code}; }}
      value=$(_t_with_progress 'Testing progress' work)
      result_code=$?
      print -r -- "RESULT=$value RC=$result_code"
    ''', _tty=True, TMPDIR=str(temporary))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f'RESULT=payload RC={result_code}' in output
    assert 'diagnostic' in output
    assert output.count('SESSION_SAVE') == 1 and 'SESSION_SAVE subshell=0' in output
    # The caller's command substitution has its ordinary EXIT trap too; only
    # the nested animation child must omit it.
    assert output.count('EXIT_TRAP') == 2
    assert 'EXIT_TRAP subshell=0' in output and 'EXIT_TRAP subshell=1' in output
    assert 'EXIT_TRAP subshell=2' not in output
    assert output.count('DIRECT_EXIT') == 1 and 'DIRECT_EXIT subshell=0' in output
    assert not list(temporary.iterdir())
