"""Codex and Claude share the session MCP lookup and additive installation."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_script


def record(kind, payload, timestamp='2026-10-28T12:00:00Z'):
    return json.dumps(dict(type=kind, payload=payload, timestamp=timestamp)) + '\n'


def message(role, text):
    return record('response_item', dict(type='message', role=role,
                  content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)]))


def test_codex_index_search_and_detail(t_mod, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    codex = tmp_path / 'custom-codex'
    monkeypatch.setattr(t_mod, 'HOME', str(home))
    monkeypatch.setenv('CODEX_HOME', str(codex))
    monkeypatch.setattr(t_mod, '_projects_dir', lambda: str(home / '.claude/projects'))
    monkeypatch.setattr(t_mod, '_index_dir', lambda: str(tmp_path / 'index'))
    cwd = home / 'code/.worktrees/t/t-16'
    cfg = t_mod.Config()
    cfg.repos = {'t': str(home / 'code/t')}
    cfg.worktree_root = str(home / 'code/.worktrees')
    path = codex / 'sessions/2026/10/28/rollout-desktop.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(record('session_meta', dict(id='abcdef00-1111', cwd=str(cwd), git={'branch': 'dev/t-16'}))
                    + message('user', '# AGENTS.md instructions dashboard bogus')
                    + message('user', '<environment_context>dashboard bogus</environment_context>')
                    + message('user', 'Build the dashboard mobile app')
                    + record('event_msg', dict(type='user_message', message='Build the dashboard mobile app'))
                    + message('assistant', 'Preview ready https://github.com/agenthangar/t/pull/116')
                    + message('user', '# AGENTS.md instructions https://github.com/other/repo/pull/999')
                    + record('event_msg', dict(type='agent_message', message='https://github.com/other/repo/pull/998'))
                    + record('response_item', dict(type='function_call_output', output='https://github.com/other/repo/pull/997'))
                    + '{bad "response_item"\n')
    archive = codex / 'archived_sessions/rollout-local.jsonl'
    archive.parent.mkdir()
    archive.write_text(record('session_meta', dict(id='abcdef00-2222', cwd=str(cwd)))
                       + message('user', 'local inference workflow'))
    subagent = path.with_name('rollout-child.jsonl')
    subagent.write_text(record('session_meta', dict(id='child', cwd=str(cwd), source={'subagent': {}}))
                        + message('user', 'dashboard'))
    records = t_mod._mcp_records()
    assert len(records) == 2
    desktop = next(r for r in records if r['sid'] == 'abcdef00-1111')
    assert desktop['n_prompts'] == 1
    assert desktop['branch'] == 'dev/t-16'
    assert desktop['last_asst'].startswith('Preview ready')
    assert desktop['pr'].endswith('/116')
    ctx = dict(recs=records, live={}, idle=[], now=1793275200, cache_root=str(tmp_path / 'cache'), this_sid='abcdef00-1111')
    found = t_mod._tool_find(cfg, ctx, dict(query='dashboard mobile', repo='t'))
    assert found['results'][0]['agent'] == 'codex'
    assert found['results'][0]['this_session']
    monkeypatch.setattr(t_mod, '_git_brief', lambda p: None)
    detail = t_mod._tool_detail(cfg, ctx, {'session': 'abcdef00-1111'})
    assert detail['first_prompts'][0]['text'] == 'Build the dashboard mobile app'
    before = path.stat().st_size
    with path.open('a') as f:
        f.write(message('user', 'later mobile instructions').rstrip('\n'))
    partial = t_mod._index_update(str(path), str(tmp_path / 'index'))
    assert partial['off'] == before
    with path.open('a') as f:
        f.write('\n')
    assert t_mod._index_update(str(path), str(tmp_path / 'index'))['n_prompts'] == 2
    path.write_text(record('session_meta', dict(id='new-id', cwd=str(cwd))) + message('user', 'replacement'))
    assert t_mod._index_update(str(path), str(tmp_path / 'index'))['n_prompts'] == 1


def test_codex_parser_invalid_shapes(t_mod):
    st = t_mod._index_empty(1)
    for line in ['bad', '[]', '{"payload":[]}', record('response_item', dict(type='message', role='user', content=None)), record('other', {})]:
        t_mod._codex_scan_line(st, line)
    t_mod._codex_scan_line(st, message('user', 'no url /pull/'))
    assert st['n_prompts'] == 1


@pytest.fixture
def setup_module_api():
    return load_script(Path(__file__).parents[1] / 'libexec/t_mcp_setup.py', 'mcp_setup_test')


@pytest.mark.parametrize('existing', [dict(name='sessions', enabled=False), dict(name='sessions', transport={'command': '/custom/server'})])
def test_registration_preserves_existing(setup_module_api, monkeypatch, existing):
    api = setup_module_api
    monkeypatch.setattr(api.shutil, 'which', lambda _: '/fake/codex')
    calls = []
    def run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([existing]))
    monkeypatch.setattr(api.subprocess, 'run', run)
    assert api.install('/user/bin/t') == 0
    assert len(calls) == 1


@pytest.mark.parametrize('response', ['[]', '[1]', '{}', 'bad', 'list-fails', 'add-fails', 'os-error', 'timeout'])
def test_registration_results(setup_module_api, monkeypatch, response):
    api = setup_module_api
    monkeypatch.setattr(api.shutil, 'which', lambda _: '/fake/codex')
    calls = []
    def run(args, **kw):
        calls.append(args)
        assert kw['stdin'] == subprocess.DEVNULL
        if response == 'os-error':
            raise OSError('unavailable')
        if response == 'timeout':
            raise subprocess.TimeoutExpired(args, 20)
        if args[2] == 'add':
            assert args == ['/fake/codex', 'mcp', 'add', 'sessions', '--', '/user/bin/t', 'mcp']
            return SimpleNamespace(returncode=int(response == 'add-fails'))
        return SimpleNamespace(returncode=int(response == 'list-fails'), stdout='[]' if response == 'add-fails' else response)
    monkeypatch.setattr(api.subprocess, 'run', run)
    assert api.install('/user/bin/t') == (0 if response == '[]' else 1)


def test_registration_no_codex(setup_module_api, monkeypatch):
    monkeypatch.setattr(setup_module_api.shutil, 'which', lambda _: None)
    assert setup_module_api.install('/user/bin/t') == 0


def test_live_session_detail_uses_current_worktree(t_mod, tmp_path):
    cfg = t_mod.Config()
    cfg.repos = {'t': str(tmp_path / 't')}
    cfg.worktree_root = str(tmp_path / '.worktrees')
    current = str(tmp_path / '.worktrees/t/t-2')
    rec = dict(sid='a', cwd=str(tmp_path / '.worktrees/t/16'), prompts=[], n_prompts=0)
    row = t_mod._session_row(cfg, rec, {'a': dict(slot='t-2', cwd=current, state='detached')}, 1)
    assert row['cwd'] == current
    assert row['worktree'] == current
    assert row['slot'] == 't-2'


def test_codex_invalid_identity(t_mod):
    st = t_mod._index_empty(1)
    t_mod._codex_scan_line(st, record('session_meta', {'id': 1, 'cwd': []}))
    assert st['sid'] is None
    t_mod._codex_scan_line(st, record('session_meta', {'id': 'a', 'cwd': '/repo'}))
    t_mod._codex_scan_line(st, record('session_meta', {'id': 'b', 'cwd': '/repo'}))
    assert st['invalid_identity']


def test_codex_moved_session_uses_latest_turn_worktree(t_mod):
    st = t_mod._index_empty(1)
    t_mod._tx_scan_line(st, record('session_meta', {'id': 'a', 'cwd': '/repo/t-2', 'git': {'branch': 'dev/t-2'}}))
    t_mod._tx_scan_line(st, record('turn_context', {'cwd': '/repo/t-1'}))
    assert st['cwd'] == '/repo/t-1'
    assert st['branch'] is None
    for cwd in (None, [], 'relative'):
        t_mod._tx_scan_line(st, record('turn_context', {'cwd': cwd}))
    assert st['cwd'] == '/repo/t-1'
