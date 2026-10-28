"""Local delegation uses isolated Git/HOME state and synthetic processes, never real models."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest
from conftest import load_script


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    return load_script(Path(__file__).parents[1] / 'libexec/t_local_worker.py', 'local_worker_test')


@pytest.fixture
def repo(worker, tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    worker.git(path, 'init', '-q')
    (path / 'file.py').write_text('before\n')
    (path / '.gitignore').write_text('secret\n')
    worker.git(path, 'add', '.')
    worker.git(path, '-c', 'user.name=test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'initial')
    return path


def test_settings_environment_and_lock(worker, tmp_path, monkeypatch):
    assert not worker.settings()['enabled']
    worker.save_settings({'enabled': True, 'model': 'gpt-oss:20b'})
    assert worker.settings()['enabled']
    assert worker.config_path().stat().st_mode & 0o777 == 0o600
    worker.config_path().write_text('[]')
    with pytest.raises(ValueError): worker.settings()
    for value in ('', '--help', 'name cloud', 'a:cloud', 'http://a', '../model', None):
        with pytest.raises(ValueError): worker.model_name(value)
    monkeypatch.setenv('OPENAI_API_KEY', 'secret')
    monkeypatch.setenv('COV_CORE_SOURCE', 'secret')
    monkeypatch.setenv('GIT_DIR', 'bad')
    env = worker.environment(tmp_path / 'worker-home')
    assert not {'OPENAI_API_KEY', 'COV_CORE_SOURCE', 'GIT_DIR'} & env.keys()
    assert env['HOME'] != os.environ['HOME']
    with worker.reservation():
        with pytest.raises(ValueError, match='already running'):
            with worker.reservation(): pass
    with worker.reservation(): pass


def test_snapshot_dirty_untracked_and_no_lead_mutation(worker, repo, tmp_path):
    (repo / 'file.py').write_text('dirty\n')
    (repo / 'new.py').write_text('new\n')
    (repo / 'secret').write_text('private\n')
    before = worker.git(repo, 'status', '--porcelain')
    clone = tmp_path / 'copy'
    head = worker.snapshot(repo, clone)
    assert len(head) == 40
    assert (clone / 'file.py').read_text() == 'dirty\n'
    assert (clone / 'new.py').exists() and not (clone / 'secret').exists()
    assert not worker.git(clone, 'remote')
    assert not worker.git(clone, 'status', '--porcelain')
    assert worker.git(repo, 'status', '--porcelain') == before
    (clone / 'file.py').write_text('worker fix\n')
    assert (repo / 'file.py').read_text() == 'dirty\n'
    (repo / 'subdir').mkdir()
    with pytest.raises(ValueError, match='repository root'):
        worker.snapshot(repo / 'subdir', tmp_path / 'other')


def test_snapshot_rejects_symlinks_and_submodules(worker, repo, tmp_path):
    (repo / 'link').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='regular files'):
        worker.snapshot(repo, tmp_path / 'symlink-copy')
    (repo / 'link').unlink()
    worker.git(repo, 'update-index', '--add', '--cacheinfo', '160000',
               worker.git(repo, 'rev-parse', 'HEAD').decode().strip(), 'sub')
    with pytest.raises(ValueError, match='submodules'):
        worker.snapshot(repo, tmp_path / 'sub-copy')


def test_git_failure(worker, tmp_path):
    with pytest.raises(ValueError): worker.git(tmp_path, 'invalid-command')


def test_collect_observed_evidence_and_patch(worker, repo, tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    worker.snapshot(repo, job / 'workspace')
    (job / 'workspace/new.txt').write_text('new artifact')
    (job / 'answer.txt').write_text('reported findings')
    (job / 'events.jsonl').write_text('not json\n[]\n' + json.dumps({
        'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'pytest',
                                          'exit_code': 1, 'status': 'completed', 'aggregated_output': 'failure'}}))
    result = worker.collect(job)
    assert result['evidence'][0]['exit_code'] == 1
    assert result['findings'] == 'reported findings'
    assert 'new artifact' in result['patch']
    assert not result['validated_by_lead']
    assert not (repo / 'new.txt').exists()
    (job / 'events.jsonl').unlink()
    (job / 'answer.txt').unlink()
    assert worker.collect(job)['findings'] == ''


@pytest.mark.parametrize('args', [{}, [], {'assignment': ''}, {'assignment': 'x', 'workspace': 'relative'},
    {'assignment': 'x', 'workspace': '/repo', 'mode': 'write'},
    {'assignment': 'x', 'workspace': '/repo', 'timeout_seconds': True},
    {'assignment': 'x', 'workspace': '/repo', 'extra': 1}])
def test_start_validation(worker, args):
    with pytest.raises(ValueError): worker.Worker().start(args)


def test_task_lifecycle_and_exclusivity(worker, repo, monkeypatch):
    manager = worker.Worker()
    args = {'workspace': str(repo), 'assignment': 'inspect', 'mode': 'patch'}
    with pytest.raises(ValueError, match='disabled'): manager.start(args)
    worker.save_settings({'enabled': True, 'model': 'gpt-oss:20b'})
    started = threading.Event()
    @contextmanager
    def server(): yield 'http://127.0.0.1:1234', {}
    monkeypatch.setattr(worker, 'ollama_server', server)
    monkeypatch.setattr(worker, 'check_model', lambda *a: None)
    def run(job, base, model, timeout, cancel):
        started.set()
        assert cancel.wait(10)
        return 'cancelled'
    monkeypatch.setattr(worker, 'run_worker', run)
    task = manager.start(args)
    assert started.wait(10)
    with pytest.raises(ValueError, match='already running'): manager.start(args)
    ident = {'task_id': task['task_id']}
    assert manager.call('task_status', ident)['status'] == 'running'
    assert manager.call('cancel_task', ident)['status'] == 'cancelling'
    manager.close()
    final = manager.call('task_status', ident)
    assert final['status'] == 'cancelled' and final['elapsed_seconds'] >= 0
    assert manager.call('cancel_task', ident)['status'] == 'cancelled'
    assert json.loads((worker.state_root() / task['task_id'] / 'result.json').read_text())['status'] == 'cancelled'
    for name, params in [('bad', {}), ('task_status', {}), ('task_status', {'task_id': []})]:
        with pytest.raises(ValueError): manager.call(name, params)


def test_failure_releases_lease(worker, repo, monkeypatch):
    worker.save_settings({'enabled': True, 'model': 'gpt-oss:20b'})
    monkeypatch.setattr(worker, 'snapshot', lambda *a: (_ for _ in ()).throw(ValueError('snapshot failed')))
    manager = worker.Worker()
    task = manager.call('start_task', {'workspace': str(repo), 'assignment': 'inspect'})
    manager.close()
    assert manager.call('task_status', {'task_id': task['task_id']})['error'] == 'snapshot failed'
    with worker.reservation(): pass


def test_worker_command_and_process_timeout(worker, repo, tmp_path, monkeypatch):
    binary = tmp_path / 'fake-codex'
    binary.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n')
    binary.chmod(0o755)
    monkeypatch.setattr(worker, 'executable', lambda name: str(binary))
    job = tmp_path / 'job'
    job.mkdir()
    worker.snapshot(repo, job / 'workspace')
    (job / 'assignment.json').write_text('{"mode":"investigate","assignment":"inspect"}')
    assert worker.run_worker(job, 'http://127.0.0.1:1234', 'test', .1, threading.Event()) == 'timed_out'
    cancel = threading.Event(); cancel.set()
    assert worker.run_worker(job, 'http://127.0.0.1:1234', 'test', 10, cancel) == 'cancelled'
    binary.write_text('#!/bin/sh\nexit 1\n')
    assert worker.run_worker(job, 'http://127.0.0.1:1234', 'test', 10, threading.Event()) == 'failed'
    binary.write_text('#!/bin/sh\nexit 0\n')
    assert worker.run_worker(job, 'http://127.0.0.1:1234', 'test', 10, threading.Event()) == 'completed'
    command = worker.codex_command(repo, tmp_path / 'out', 'test')
    assert '--oss' in command and '--ignore-user-config' in command and 'ollama' in command
    assert 'approval_policy="never"' in command and '--ephemeral' in command
    assert '--dangerously-bypass-approvals-and-sandbox' not in command
    monkeypatch.setattr(worker, 'executable', lambda name: None)
    with pytest.raises(ValueError, match='missing'): worker.codex_command(repo, tmp_path / 'out', 'test')


def test_model_capabilities(worker, monkeypatch):
    for info in ({}, {'capabilities': ['tools'], 'remote_host': 'cloud'}):
        monkeypatch.setattr(worker, 'request', lambda *a: info)
        with pytest.raises(ValueError): worker.check_model('local', 'model')
    monkeypatch.setattr(worker, 'request', lambda *a: {'capabilities': ['tools']})
    assert worker.check_model('local', 'model')['capabilities'] == ['tools']


def test_ollama_server_private_environment_and_shutdown(worker, monkeypatch):
    worker.state_root().mkdir(parents=True)
    monkeypatch.setattr(worker, 'executable', lambda name: '/fake/ollama')
    stopped = []
    class Process:
        def __init__(self, argv, **kw):
            assert argv == ['/fake/ollama', 'serve']
            assert kw['env']['OLLAMA_NO_CLOUD'] == '1'
            assert kw['env']['OLLAMA_HOST'].startswith('127.0.0.1:')
            assert kw['start_new_session']
        def poll(self): return None
    monkeypatch.setattr(worker.subprocess, 'Popen', Process)
    monkeypatch.setattr(worker, 'stop', lambda proc: stopped.append(proc))
    monkeypatch.setattr(worker, 'request', lambda *a: {})
    with worker.ollama_server() as (base, env):
        assert base.startswith('http://127.0.0.1:')
    assert len(stopped) == 1
    monkeypatch.setattr(Process, 'poll', lambda self: 1)
    with pytest.raises(ValueError, match='did not start'):
        with worker.ollama_server(): pass
    monkeypatch.setattr(worker, 'executable', lambda name: None)
    with pytest.raises(ValueError, match='missing'):
        with worker.ollama_server(): pass


def test_mcp_protocol(worker, t_mod, monkeypatch, capsys):
    data = '\n'.join(['bad', json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}),
                     json.dumps({'method': 'notifications/initialized'}),
                     json.dumps({'id': 2, 'method': 'tools/list'}),
                     json.dumps({'id': 3, 'method': 'tools/call', 'params': {'name': 'task_status', 'arguments': {}}})])
    monkeypatch.setattr(t_mod.sys, 'stdin', io.StringIO(data))
    assert worker.serve(vars(t_mod)) == 0
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0]['error']['code'] == -32700
    assert replies[1]['result']['serverInfo']['name'] == 't-local-worker'
    assert len(replies[2]['result']['tools']) == 3
    assert replies[3]['result']['isError']


def test_http_request_never_uses_proxy(worker, monkeypatch):
    class Response:
        def __enter__(self): return io.StringIO('{"models": []}')
        def __exit__(self, *a): pass
    class Opener:
        def open(self, req, timeout):
            assert req.full_url == 'http://127.0.0.1:1234/api/show'
            assert json.loads(req.data) == {'model': 'test'}
            assert timeout == 3
            return Response()
    def opener(handler):
        assert handler.proxies == {}
        return Opener()
    monkeypatch.setenv('HTTPS_PROXY', 'https://must-not-use')
    monkeypatch.setattr(worker.urllib.request, 'build_opener', opener)
    assert worker.request('http://127.0.0.1:1234', '/api/show', {'model': 'test'}) == {'models': []}


def test_native_executable_and_missing(worker, monkeypatch):
    monkeypatch.setattr(worker.shutil, 'which', lambda name: None)
    assert worker.executable('codex') is None
    native = Path.home() / '.local/bin/codex'
    native.parent.mkdir(parents=True)
    native.write_text('#!/bin/sh\n')
    native.chmod(0o755)
    assert worker.executable('codex') == str(native)


def test_deleted_file_snapshot_and_patch_limit(worker, repo, tmp_path, monkeypatch):
    (repo / 'file.py').unlink()
    job = tmp_path / 'job'; job.mkdir()
    worker.snapshot(repo, job / 'workspace')
    assert not (job / 'workspace/file.py').exists()
    (job / 'workspace/new.txt').write_text('x' * 40000)
    result = worker.collect(job)
    assert result['patch_truncated']
    assert len(Path(result['patch_path']).read_bytes()) > 32000


def test_output_limit_and_setup_thread_failure(worker, repo, tmp_path, monkeypatch):
    binary = tmp_path / 'fake'
    binary.write_text('#!/bin/sh\nprintf "too much output"\nsleep 60\n'); binary.chmod(0o755)
    monkeypatch.setattr(worker, 'executable', lambda name: str(binary))
    monkeypatch.setattr(worker, 'MAX_OUTPUT', 3)
    job = tmp_path / 'job'; job.mkdir()
    worker.snapshot(repo, job / 'workspace')
    (job / 'assignment.json').write_text('{"mode":"patch","assignment":"inspect"}')
    assert worker.run_worker(job, 'http://127.0.0.1:1234', 'test', 10, threading.Event()) == 'output_limit'
    worker.save_settings({'enabled': True, 'model': 'test'})
    monkeypatch.setattr(worker.threading.Thread, 'start', lambda self: (_ for _ in ()).throw(OSError('thread failed')))
    with pytest.raises(OSError):
        worker.Worker().start({'workspace': str(repo), 'assignment': 'inspect'})
    with worker.reservation(): pass


def test_snapshot_limits(worker, repo, tmp_path, monkeypatch):
    original_git = worker.git
    def git_with_unsafe_name(path, *args, **kwargs):
        if '--cached' in args: return b'../escape\0'
        return original_git(path, *args, **kwargs)
    monkeypatch.setattr(worker, 'git', git_with_unsafe_name)
    with pytest.raises(ValueError, match='unsafe'): worker.snapshot(repo, tmp_path / 'unsafe')
    monkeypatch.setattr(worker, 'git', original_git)
    with (repo / 'large').open('wb') as stream:
        stream.truncate(257 * 1024 * 1024)
    with pytest.raises(ValueError, match='256 MiB'): worker.snapshot(repo, tmp_path / 'large-copy')
