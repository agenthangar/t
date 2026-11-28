"""Regression enforcement and terminal behavior for quiet blocking work."""
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from pty_helpers import _run_under_pty
from test_agent_seam import zsh  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
LINT = runpy.run_path(str(ROOT / 'scripts/lint_progress.py'))


def test_progress_inventory():
    assert LINT['check']() == []


def test_quiet_feedback_is_nested_thread_local_and_shared_across_loads(monkeypatch):
    import threading
    first=runpy.run_path(str(ROOT/'libexec/t_progress.py'))
    second=runpy.run_path(str(ROOT/'libexec/t_progress.py'))
    with first['quiet']():
        with second['quiet']():
            assert first['_state'].local.quiet
            with second['spinner']('must stay quiet'):
                assert not first['_state'].lock.locked()
        assert first['_state'].local.quiet
        other=[]
        thread=threading.Thread(target=lambda:other.append(getattr(first['_state'].local,'quiet',False)))
        thread.start();thread.join();assert other==[False]
    assert not first['_state'].local.quiet


def test_progress_detects_removed_spinner_and_new_wait(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'libexec').mkdir()
    (tmp_path / 'ui').mkdir()
    (tmp_path / 'ui/progress-contract.json').write_text(json.dumps({
        'exceptions': {}, 'shell': [], 'phases': [['bin/t', 'query', 'records.glob']],
    }))
    path = tmp_path / 'bin/t'
    path.write_text("@_spinner('Waiting')\ndef query():\n    records.glob('*')\n    return subprocess.run(['gh'])\n")
    assert not LINT['check'](tmp_path)
    path.write_text(path.read_text().replace("@_spinner('Waiting')\n", ''))
    errors = LINT['check'](tmp_path)
    assert any('blocking work needs _spinner' in error for error in errors)
    assert any('collection phase needs _spinner' in error for error in errors)
    path.write_text("def query():\n    with _spinner('Waiting'):\n        subprocess.run(['gh'])\n    time.sleep(10)\n")
    errors = LINT['check'](tmp_path)
    assert len(errors) == 2
    assert any('stale collection phase' in error for error in errors)
    path.write_text("@_spinner('Waiting')\ndef query():\n    def later():\n        time.sleep(10)\n")
    assert any('later:time.sleep' in error for error in LINT['check'](tmp_path))


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


@pytest.mark.parametrize('mode', ['normal', 'fast', 'nested', 'dumb', 'json', 'raw', 'quiet', 'redirected'])
def test_shared_spinner_gates_and_cleanup(monkeypatch, mode):
    class Stream(io.StringIO):
        def isatty(self): return mode != 'redirected'
    out, err = Stream(), Stream()
    monkeypatch.setattr('sys.stdout', out)
    monkeypatch.setattr('sys.stderr', err)
    monkeypatch.setattr('sys.argv', ['t', '--' + mode] if mode in ('json', 'raw', 'quiet') else ['t'])
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


@pytest.mark.parametrize('mode', ['terminal', 'redirected', 'json'])
def test_mcp_list_progress_clears_before_output(tmp_path, monkeypatch, mode):
    tools = tmp_path / 'bin'
    tools.mkdir()
    codex = tools / 'codex'
    codex.write_text('#!/bin/sh\nsleep 0.45\nprintf "[]\\n"\n')
    codex.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.pop('T_UPDATE_QUIET', None)
    env.update(HOME=str(tmp_path), CODEX_HOME=str(tmp_path / 'codex'),
               CLAUDE_CONFIG_DIR=str(tmp_path / 'claude'), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_CACHE_HOME=str(tmp_path / 'cache'), XDG_STATE_HOME=str(tmp_path / 'state'),
               PATH=f'{tools}{os.pathsep}{env["PATH"]}', TERM='xterm-256color', T_NO_UPDATE_CHECK='1')
    command = [sys.executable, str(ROOT / 'bin/t'), 'mcp', 'list']
    if mode == 'json':
        command.append('--json')
    if mode == 'redirected':
        result = subprocess.run(command, env=env, capture_output=True, text=True, cwd=tmp_path, timeout=20)
    else:
        result = _run_under_pty(command, env, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    if mode == 'terminal':
        assert 'Reading MCP server configuration' in result.stdout
        assert '\x1b[2KNo MCP servers configured' in result.stdout
    else:
        assert '\x1b' not in result.stdout and not result.stderr
        if mode == 'json':
            assert json.loads(result.stdout) == {'scope': 'this machine', 'servers': []}
        else:
            assert result.stdout == 'No MCP servers configured on this machine.\n'


def test_issue_list_shows_progress_for_cumulative_short_queries(monkeypatch):
    issue = runpy.run_path(str(ROOT / 'libexec/t_issue.py'))
    command = issue['IssueCommand'](None, {'_t_source_root': lambda: ROOT})
    monkeypatch.setattr(command, 'list_repositories', lambda *a: (['a', 'b', 'c', 'd'], None, []))

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    terminal = Terminal()
    monkeypatch.setattr(sys, 'stdout', terminal)
    monkeypatch.setattr(sys, 'stderr', terminal)
    monkeypatch.setattr(sys, 'argv', ['t', 'issue', 'list', '--all'])
    monkeypatch.setenv('TERM', 'xterm')

    @issue['_spinner']('Individual query')
    def query(repo, state):
        time.sleep(.12)
        return [repo]

    monkeypatch.setattr(command, 'issues', query)
    monkeypatch.setattr(command, 'render_issues', lambda rows, *_: print(', '.join(rows)))
    assert command.list_issues(SimpleNamespace(repo=None, all=True, state='open', json=False)) == 0
    output = terminal.getvalue()
    assert 'Checking issues' in output and 'Individual query' not in output
    assert output.endswith('\r\x1b[2Ka, b, c, d\n')


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
    if delay < .25:
        assert '\x1b[2K' not in output
    else:
        assert 'Testing progress' in output
        assert '\x1b[2Kdiagnostic' in output
    assert output.count('SESSION_SAVE') == 1 and 'SESSION_SAVE subshell=0' in output
    # The caller's command substitution has its ordinary EXIT trap too; only
    # the nested animation child must omit it.
    assert output.count('EXIT_TRAP') == 2
    assert 'EXIT_TRAP subshell=0' in output and 'EXIT_TRAP subshell=1' in output
    assert 'EXIT_TRAP subshell=2' not in output
    assert output.count('DIRECT_EXIT') == 1 and 'DIRECT_EXIT subshell=0' in output
    assert not list(temporary.iterdir())
