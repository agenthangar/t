"""Fresh allocation uses only local evidence; maintenance never changes checkouts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'libexec/t_fresh.py'
    spec = importlib.util.spec_from_file_location('fresh_workspace', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repository = tmp_path / 'repo'
    repository.mkdir()
    for args in (['init', '-q', '-b', 'main'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                              'commit', '-q', '--allow-empty', '-m', 'base']):
        subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True)
    original = subprocess.run
    def isolated(argv, **kwargs):
        if argv[0] == 'tmux':
            return subprocess.CompletedProcess(argv, 1, '', '')
        assert argv[0] != 'gh' and 'fetch' not in argv, 'foreground must never contact a host'
        return original(argv, **kwargs)
    monkeypatch.setattr(module.subprocess, 'run', isolated)
    return module, repository, tmp_path / 'worktrees'


def test_counter_is_shared_by_alias_and_worktree_and_is_monotonic(fresh):
    module, repo, root = fresh
    alias = repo.parent / 'alias'
    alias.symlink_to(repo, target_is_directory=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda i: int(module.reserve(repo if i % 2 else alias, root)), range(8)))
    assert sorted(values) == list(range(1, 9))
    prepared = module.prepare(repo, root, module.reserve(repo, root))
    assert module.reserve(prepared['workspace'], root) == '10'
    counter = module.folder(repo) / 'counter.json'
    counter.write_text('corrupt')
    assert module.reserve(repo, root) == '10'  # Rebuild from actual local branches/directories.


def test_cold_counter_skips_all_refs_and_both_directory_layouts(fresh):
    module, repo, root = fresh
    module.git(repo, 'branch', 'dev/repo-4')
    module.git(repo, 'update-ref', 'refs/remotes/origin/dev/repo-8', 'HEAD')
    (root / 'repo/11').mkdir(parents=True)
    assert module.reserve(repo, root) == '12'
    assert module.reserve(repo, root) == '13'  # No rollback after a failed/crashed launch.


def test_prepare_returns_frozen_history_evidence_and_local_default(fresh):
    module, repo, root = fresh
    value = module.prepare(repo, root, '1')
    assert value['commit'] == module.git(repo, 'rev-parse', 'main')
    assert value['branch'] == 'dev/repo-1' and value['slot'] == '1'
    assert value['repo'] == 'local:' + str(repo)
    path = Path(value['workspace'])
    info = (path / '.git').stat()
    expected = hashlib.sha256(json.dumps([value['gitdir'], info.st_dev, info.st_ino, info.st_ctime_ns]).encode()).hexdigest()
    assert value['generation'] == expected
    assert value['workspace_identity'] == f'{path.stat().st_dev}:{path.stat().st_ino}'
    assert value['observed'] > 0
    assert module.git(repo, 'branch', '--show-current') == 'main'


@pytest.mark.parametrize('collision', ['local', 'remote', 'named', 'legacy', 'symlink', 'history'])
def test_prepare_never_reuses_or_reclaims_any_claim(fresh, collision):
    module, repo, root = fresh
    parent = root / 'repo'
    parent.mkdir(parents=True)
    if collision == 'local':
        module.git(repo, 'branch', 'dev/repo-1')
    elif collision == 'remote':
        module.git(repo, 'update-ref', 'refs/remotes/origin/dev/repo-1', 'HEAD')
    elif collision == 'symlink':
        (parent / 'repo-1').symlink_to(parent / 'missing')
    else:
        path = parent / {'named': 'repo-1', 'legacy': '1', 'history': '.repo-1.history-reservation'}[collision]
        path.write_text('preserve')
    with pytest.raises(module.Collision):
        module.prepare(repo, root, '1')
    assert not (parent / 'repo-1/.git').exists()
    assert module.git(repo, 'branch', '--show-current') == 'main'


def test_cached_origin_default_is_pinned_before_creation(fresh):
    module, repo, root = fresh
    old = module.git(repo, 'rev-parse', 'HEAD')
    module.git(repo, 'update-ref', 'refs/remotes/origin/custom', old)
    module.git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/custom')
    module.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
               'commit', '-q', '--allow-empty', '-m', 'local ahead')
    assert module.prepare(repo, root, '1')['commit'] == old


def test_missing_remote_default_uses_its_local_counterpart(fresh):
    module, repo, root = fresh
    original = module.git(repo, 'rev-parse', 'HEAD')
    module.git(repo, 'branch', 'trunk')
    module.git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
    module.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
               'commit', '-q', '--allow-empty', '-m', 'main ahead')
    assert module.default_revision(repo) == original


def test_missing_default_and_invalid_slot_fail_without_creation(fresh):
    module, repo, root = fresh
    module.git(repo, 'branch', '-m', 'feature')
    with pytest.raises(ValueError, match='default branch'):
        module.prepare(repo, root, '1')
    with pytest.raises(ValueError, match='invalid fresh slot'):
        module.prepare(repo, root, 'bad')
    assert not root.exists()


def test_unsafe_private_state_is_rejected(fresh):
    module, repo, root = fresh
    (repo / '.git/t-fresh').symlink_to(root)
    root.mkdir()
    with pytest.raises(ValueError, match='unsafe'):
        module.reserve(repo, root)


def test_schedule_is_detached_and_event_coalesces_in_worker(fresh, monkeypatch):
    module, repo, root = fresh
    calls = []
    original = module.subprocess.Popen
    def popen(argv, **kwargs):
        if argv[0] == 'git':
            return original(argv, **kwargs)
        calls.append(((argv,), kwargs))
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    module.schedule(repo)
    assert not calls  # Test isolation is inherited by every session fixture.
    monkeypatch.delenv('T_FRESH_REFRESH_DISABLE')
    monkeypatch.setenv('COV_CORE_SOURCE', 'measured')
    module.schedule(repo, force=True)
    (argv,), options = calls.pop()
    assert argv[-2:] == ['watch', str(repo)]
    assert options['start_new_session'] and options['close_fds']
    assert options['cwd'] == '/' and not any(k.startswith('COV_CORE_') for k in options['env'])
    assert (module.folder(repo) / 'refresh-request').exists()


@pytest.mark.parametrize('success,interval', [(True, 300), (False, 60)])
def test_worker_repeats_on_interval_and_releases_lock(fresh, monkeypatch, success, interval):
    module, repo, root = fresh
    clock, calls = [0], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'refresh', lambda directory: calls.append(str(directory)) or success)
    def sleep(seconds):
        assert seconds == 1
        if len(calls) == 2:
            raise StopIteration
        clock[0] += interval
    monkeypatch.setattr(module.time, 'sleep', sleep)
    with pytest.raises(StopIteration):
        module.watch(repo)
    assert calls == [str(repo), str(repo)]
    assert module.read(module.folder(repo) / 'refresh.json')['success'] is success
    with module.locked(module.folder(repo) / 'refresh.lock', blocking=False) as acquired:
        assert acquired


def test_worker_does_not_overlap_and_force_wakes_before_interval(fresh, monkeypatch):
    module, repo, root = fresh
    state = module.folder(repo)
    calls = []
    monkeypatch.setattr(module, 'refresh', lambda directory: calls.append(directory) or True)
    with module.locked(state / 'refresh.lock'):
        module.watch(repo)
    assert not calls
    monkeypatch.setattr(module.time, 'monotonic', lambda: 0)
    def sleep(_):
        if len(calls) == 2:
            raise StopIteration
        (state / 'refresh-request').touch()
    monkeypatch.setattr(module.time, 'sleep', sleep)
    with pytest.raises(StopIteration):
        module.watch(repo)
    assert len(calls) == 2


@pytest.mark.parametrize('outcome', ['ok', 'failed', 'timeout'])
def test_background_fetch_is_bounded_noninteractive_and_only_updates_refs(fresh, monkeypatch, outcome):
    module, repo, root = fresh
    def run(argv, **kwargs):
        assert argv == ['git', '-C', str(repo), 'fetch', '--quiet', '--prune', '--no-tags', 'origin']
        assert kwargs['timeout'] == 30 and kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 0 if outcome == 'ok' else 1, '', '')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.refresh(repo) is (outcome == 'ok')


def test_two_creators_cannot_adopt_one_anothers_worktree(fresh):
    module, repo, root = fresh
    def create(_):
        try:
            return module.prepare(repo, root, '1')
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(create, range(2)))
    assert sum(value is not None for value in values) == 1
    assert module.git(root / 'repo/repo-1', 'branch', '--show-current') == 'dev/repo-1'


def test_failed_post_checkout_hook_stops_after_one_attempt_and_retains_evidence(fresh, monkeypatch):
    module, repo, root = fresh
    hook = repo / '.git/hooks/post-checkout'
    hook.write_text('#!/bin/sh\nprintf "checkout hook failed\\n" >&2\nexit 1\n')
    hook.chmod(0o755)
    module.git(repo, 'config', 'core.hooksPath', str(hook.parent))
    original, attempts = module.git, []
    def counted(repository, *args, **kwargs):
        if args[:2] == ('worktree', 'add'):
            attempts.append(args)
        return original(repository, *args, **kwargs)
    monkeypatch.setattr(module, 'git', counted)
    slot = module.reserve(repo, root)
    with pytest.raises(ValueError, match='checkout hook failed') as failure:
        module.prepare(repo, root, slot)
    assert not isinstance(failure.value, module.Collision)
    assert len(attempts) == 1
    workspace = root / 'repo/repo-1'
    assert workspace.is_dir() and (workspace / '.git').is_file()
    assert module.git(workspace, 'branch', '--show-current') == 'dev/repo-1'
    assert module.git(repo, 'branch', '--show-current') == 'main'
    assert module.read(module.folder(repo) / 'counter.json')['next'] == 2
    assert not (root / 'repo/repo-2').exists()


# Real isolated tmux exercises the complete launch claim, not just Git's branch
# race. No native desktop UI or user's tmux server participates.
from test_session_lifecycle import lifecycle, _until  # noqa: E402,F401


def test_simultaneous_fresh_alias_opens_allocate_distinct_owners(lifecycle):
    base, env = lifecycle
    with (base / 'config/t/local.zsh').open('a') as config:
        config.write('DEV_REPOS[alias]=$DEV_REPOS[repo]\n')
    commands = ['t open repo --new --codex --cli', 't open alias --new --codex --cli']
    processes = [subprocess.Popen(['zsh', '-lic', command], env=env, cwd=base,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for command in commands]
    try:
        results = [process.communicate(timeout=25) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], results
        lines = _until(lambda: (base / 'agents.log').read_text().splitlines() if (base / 'agents.log').exists() else [])
        assert len(lines) == 2 and len({line.split('|')[1] for line in lines}) == 2
        paths = {line.split('|')[1] for line in lines}
        assert paths == {str(base / 'worktrees/repo/repo-1'), str(base / 'worktrees/repo/repo-2')}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
