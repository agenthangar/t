"""Fresh-launch caches retain input validation and never authorize stale policy."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
                 'XDG_DATA_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'CURSOR_CONFIG_DIR'):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    for name in ('T_LAUNCH_CONTEXT', 'T_WORKFLOW_DEFAULT_MODEL', 'T_LAUNCH_DEFAULT_MODEL',
                 'T_LAUNCH_DEFAULT_EFFORT', 'T_LAUNCH_DEFAULT_SPEED'):
        monkeypatch.delenv(name, raising=False)
    spec = importlib.util.spec_from_file_location('launch_policy_test', ROOT / 'libexec/t_launch_policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(cache, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    config = tmp_path / 'policy.json'
    payload = {'args': ['--sandbox', 'workspace-write'], 'prefix': [], 'routing': None}
    config.write_text(json.dumps(payload))
    calls = []
    def fresh(agent, cwd, common_dir=None):
        calls.append((agent, str(cwd), str(common_dir)))
        return json.loads(config.read_text())
    monkeypatch.setattr(cache, 'resolve_uncached', fresh)
    monkeypatch.setattr(cache, 'dependency_paths', lambda agent, cwd: [config])
    return repo, config, payload, calls


def stored(cache, repo):
    workspace, common = cache.repository_scope(repo)
    return cache.cache_path(cache.cache_key('codex', workspace, common))


def test_warm_hit_extends_idle_expiry_without_resolving(cache, source):
    repo, _, payload, calls = source
    assert cache.resolve('codex', repo, now=100) == {**payload, 'cached': False}
    assert cache.resolve('codex', repo, now=100 + 23 * 3600)['cached']
    assert cache.resolve('codex', repo, now=100 + 25 * 3600)['cached']
    record = json.loads(stored(cache, repo).read_text())
    assert record['expires'] == 100 + 49 * 3600
    assert len(calls) == 1
    assert stat.S_IMODE(stored(cache, repo).stat().st_mode) == 0o600


def test_idle_expiry_rebuilds_once(cache, source):
    repo, _, _, calls = source
    cache.resolve('codex', repo, now=100)
    assert not cache.resolve('codex', repo, now=100 + cache.IDLE_TTL)['cached']
    assert cache.resolve('codex', repo, now=101 + cache.IDLE_TTL)['cached']
    assert len(calls) == 2


def test_manual_permission_and_provider_change_is_immediate(cache, source):
    repo, config, payload, calls = source
    cache.resolve('codex', repo, now=100)
    routing = {'mode': 'local-only'}
    payload.update(args=['--sandbox', 'read-only'], routing=routing,
                   prefix=['python3', str(cache.LIB / 't_local_inference.py'),
                           '--lead-policy', json.dumps(routing)])
    config.write_text(json.dumps(payload))
    result = cache.resolve('codex', repo, now=101)
    assert result == {**payload, 'cached': False}
    assert len(calls) == 2


@pytest.mark.parametrize('name', ['native.md', 'worker.json', 'mcp.toml'])
def test_created_replaced_deleted_dependencies_invalidate(cache, source, tmp_path, monkeypatch, name):
    repo, config, _, calls = source
    dependency = tmp_path / name
    monkeypatch.setattr(cache, 'dependency_paths', lambda agent, cwd: [config, dependency])
    cache.resolve('codex', repo, now=100)
    dependency.write_text('new source')
    assert not cache.resolve('codex', repo, now=101)['cached']
    replacement = tmp_path / 'replacement'
    replacement.write_text('new source')
    replacement.replace(dependency)
    assert not cache.resolve('codex', repo, now=102)['cached']
    dependency.unlink()
    assert not cache.resolve('codex', repo, now=103)['cached']
    assert len(calls) == 4


def test_changed_invalid_or_removed_source_never_reuses_previous_permissions(cache, source):
    repo, config, _, calls = source
    cache.resolve('codex', repo, now=100)
    config.write_text('malformed policy')
    with pytest.raises(ValueError):
        cache.resolve('codex', repo, now=101)
    config.unlink()
    with pytest.raises(FileNotFoundError):
        cache.resolve('codex', repo, now=102)
    assert len(calls) == 3


@pytest.mark.parametrize('change', ['json', 'mode', 'symlink', 'version', 'expiry', 'prefix',
                                  'policy_json', 'size', 'clock'])
def test_invalid_cache_is_ignored(cache, source, tmp_path, change):
    repo, _, payload, calls = source
    cache.resolve('codex', repo, now=100)
    path = stored(cache, repo)
    value = json.loads(path.read_text())
    if change == 'json':
        path.write_text('{broken')
    elif change == 'mode':
        path.chmod(0o644)
    elif change == 'symlink':
        target = tmp_path / 'untrusted-cache'
        path.replace(target)
        path.symlink_to(target)
    elif change == 'size':
        path.write_text(' ' * (cache.MAX_CACHE_BYTES + 1))
    else:
        if change == 'version':
            value['version'] += 1
        elif change == 'expiry':
            value['expires'] = float('inf')
        elif change == 'prefix':
            value['bundle']['prefix'] = ['arbitrary-program']
        elif change == 'policy_json':
            value['bundle'].update(routing={'mode': 'local-only'}, prefix=[
                'python3', str(cache.LIB / 't_local_inference.py'), '--lead-policy', '{broken'])
        elif change == 'clock':
            value['last_used'] = 1000
        path.write_text(json.dumps(value))
    assert cache.resolve('codex', repo, now=101) == {**payload, 'cached': False}
    assert len(calls) == 2


def test_environment_and_tool_are_cache_inputs(cache, source, monkeypatch):
    repo, _, _, calls = source
    cache.resolve('codex', repo, now=100)
    monkeypatch.setenv('T_WORKFLOW_DEFAULT_MODEL', 'different-account-model')
    assert not cache.resolve('codex', repo, now=101)['cached']
    assert not cache.resolve('claude', repo, now=102)['cached']
    assert len(calls) == 3


def linked(main, target):
    gitdir = main / '.git/worktrees' / target.name
    gitdir.mkdir(parents=True)
    (gitdir / 'commondir').write_text('../..\n')
    target.mkdir()
    (target / '.git').write_text('gitdir: ' + str(gitdir) + '\n')
    return gitdir


@pytest.mark.parametrize('content', [None, 'same_project_policy = true'])
def test_sibling_worktrees_share_warm_policy_for_equivalent_inputs(cache, source, tmp_path, monkeypatch, content):
    repo, config, _, calls = source
    first, second = tmp_path / 'repo-1', tmp_path / 'repo-2'
    linked(repo, first)
    linked(repo, second)
    if content is not None:
        for workspace in (first, second):
            (workspace / '.codex').mkdir()
            (workspace / '.codex/config.toml').write_text(content)
    monkeypatch.setattr(cache, 'dependency_paths',
                        lambda agent, cwd: [config, Path(cwd) / '.codex/config.toml'])
    assert not cache.resolve('codex', first, now=100)['cached']
    assert cache.resolve('codex', second, now=101)['cached']
    (second / '.codex').mkdir(exist_ok=True)
    (second / '.codex/config.toml').write_text('changed = true')
    assert not cache.resolve('codex', second, now=102)['cached']
    assert len(calls) == 2


def test_prepared_context_is_accepted_only_for_current_workspace(cache, source, tmp_path, monkeypatch):
    repo, _, _, _ = source
    target = tmp_path / 'repo-1'
    linked(repo, target)
    info, marker = target.stat(), (target / '.git').stat()
    context = dict(version=1, workspace=str(target), common_dir=str(repo / '.git'),
                   workspace_identity=f'{info.st_dev}:{info.st_ino}',
                   gitfile_identity=dict(dev=marker.st_dev, ino=marker.st_ino, ctime_ns=marker.st_ctime_ns))
    monkeypatch.setenv('T_LAUNCH_CONTEXT', json.dumps(context))
    assert cache._context_scope(target) == (target, repo / '.git')
    context['gitfile_identity']['ino'] += 1
    monkeypatch.setenv('T_LAUNCH_CONTEXT', json.dumps(context))
    assert cache._context_scope(target) is None
    assert cache.repository_scope(target) == (target, repo / '.git')
    monkeypatch.setenv('T_LAUNCH_CONTEXT', '{invalid')
    assert cache._context_scope(target) is None
    monkeypatch.setenv('T_LAUNCH_CONTEXT', 'x' * 65537)
    assert cache._context_scope(target) is None


def test_prepared_context_rechecks_linked_common_pointer_without_git(cache, source, tmp_path, monkeypatch):
    repo, _, _, _ = source
    target = tmp_path / 'repo-1'
    gitdir = linked(repo, target)
    info, marker = target.stat(), (target / '.git').stat()
    context = dict(version=1, workspace=str(target), common_dir=str(repo / '.git'),
                   workspace_identity=f'{info.st_dev}:{info.st_ino}',
                   gitfile_identity=dict(dev=marker.st_dev, ino=marker.st_ino, ctime_ns=marker.st_ctime_ns))
    monkeypatch.setenv('T_LAUNCH_CONTEXT', json.dumps(context))
    monkeypatch.setattr(cache.subprocess, 'run', lambda *a, **kw: pytest.fail('Git process'))
    assert cache.repository_scope(target) == (target, repo / '.git')
    other = tmp_path / 'other.git'
    other.mkdir()
    (gitdir / 'commondir').write_text(str(other))
    assert cache._context_scope(target) is None
    assert cache.repository_scope(target) == (target, other)
    with pytest.raises(ValueError, match='does not match'):
        cache.repository_scope(target, repo / '.git')


def test_unobservable_managed_mcp_inputs_are_not_cached(cache, source):
    repo, config, payload, calls = source
    payload['cacheable'] = False
    config.write_text(json.dumps(payload))
    assert not cache.resolve('codex', repo, now=100)['cached']
    assert not cache.resolve('codex', repo, now=101)['cached']
    assert len(calls) == 2
    assert not stored(cache, repo).exists()


def test_common_directory_must_match_the_actual_workspace(cache, source, tmp_path):
    repo, _, _, _ = source
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(ValueError, match='does not match'):
        cache.repository_scope(repo, other)
    assert cache.repository_scope(other) == (other, None)
    with pytest.raises(ValueError, match='cannot be verified'):
        cache.repository_scope(other, repo / '.git')
    regular = other / 'file'
    regular.touch()
    with pytest.raises(ValueError, match='not a directory'):
        cache.repository_scope(regular)
    (other / '.git').write_text('not git')
    with pytest.raises(ValueError, match='invalid linked'):
        cache.repository_scope(other)


def test_prepared_canonical_checkout_uses_its_verified_git_directory(cache, source, monkeypatch):
    repo, _, _, _ = source
    info, marker = repo.stat(), (repo / '.git').stat()
    context = dict(version=1, workspace=str(repo), common_dir=str(repo / '.git'),
                   workspace_identity=f'{info.st_dev}:{info.st_ino}',
                   gitfile_identity=dict(dev=marker.st_dev, ino=marker.st_ino, ctime_ns=marker.st_ctime_ns))
    monkeypatch.setenv('T_LAUNCH_CONTEXT', json.dumps(context))
    assert cache.repository_scope(repo) == (repo, repo / '.git')
    assert not cache.resolve('codex', repo, now=100)['cached']
    assert cache.resolve('codex', repo, now=101)['cached']


@pytest.mark.parametrize('damage', ['gitfile', 'empty_common', 'oversized_common', 'dangling_marker'])
def test_damaged_git_identity_cannot_reuse_a_warm_prepared_policy(cache, source, tmp_path, monkeypatch, damage):
    repo, _, _, calls = source
    target = tmp_path / 'repo-1'
    gitdir = linked(repo, target)
    cache.resolve('codex', target, now=100)
    marker = target / '.git'
    if damage == 'gitfile':
        marker.write_text('invalid Git pointer')
    elif damage in ('empty_common', 'oversized_common'):
        (gitdir / 'commondir').write_text(' ' if damage == 'empty_common' else 'x' * 8193)
    else:
        marker.unlink()
        marker.symlink_to(tmp_path / 'missing-git-pointer')
    info = target.stat()
    marker_info = marker.lstat() if marker.is_symlink() else marker.stat()
    context = dict(version=1, workspace=str(target), common_dir=str(repo / '.git'),
                   workspace_identity=f'{info.st_dev}:{info.st_ino}',
                   gitfile_identity=dict(dev=marker_info.st_dev, ino=marker_info.st_ino,
                                         ctime_ns=marker_info.st_ctime_ns))
    monkeypatch.setenv('T_LAUNCH_CONTEXT', json.dumps(context))
    assert cache._context_scope(target) is None
    with pytest.raises(ValueError, match='Git|git|common'):
        cache.resolve('codex', target, now=101)
    assert len(calls) == 1  # Neither stale cache use nor fresh policy resolution is safe.


def test_project_configuration_replacement_during_read_cannot_authorize_a_launch(cache, source, monkeypatch):
    repo, config, _, calls = source
    project = repo / '.codex/config.toml'
    project.parent.mkdir()
    project.write_text('permissions = "ask"')
    monkeypatch.setattr(cache, 'dependency_paths', lambda agent, cwd: [config, project])
    cache.resolve('codex', repo, now=100)
    original_fdopen = cache.os.fdopen
    class ReplacedRead:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            value = self.stream.read(count)
            replacement = project.with_suffix('.replacement')
            replacement.write_text('permissions = "read-only"')
            replacement.replace(project)
            return value
    with monkeypatch.context() as patch:
        patch.setattr(cache.os, 'fdopen', lambda *a, **kw: ReplacedRead(original_fdopen(*a, **kw)))
        with pytest.raises(ValueError, match='configuration changed while reading'):
            cache.resolve('codex', repo, now=101)
    assert len(calls) == 1
    assert not cache.resolve('codex', repo, now=102)['cached']
    assert len(calls) == 2


def test_replaced_cache_is_repaired_from_authoritative_policy(cache, source, monkeypatch):
    repo, _, payload, calls = source
    cache.resolve('codex', repo, now=100)
    path = stored(cache, repo)
    original_load = cache.json.load
    def raced_load(stream):
        value = original_load(stream)
        replacement = path.with_suffix('.replacement')
        replaced = copy.deepcopy(value)
        replaced['bundle']['args'] = ['--sandbox', 'danger-full-access']
        replacement.write_text(json.dumps(replaced))
        replacement.replace(path)
        return value
    with monkeypatch.context() as patch:
        patch.setattr(cache.json, 'load', raced_load)
        result = cache.resolve('codex', repo, now=101)
    assert result == {**payload, 'cached': False}
    assert len(calls) == 2
    assert json.loads(path.read_text())['bundle']['args'] == payload['args']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_denied_cache_lock_uses_current_permissions_without_leaking_descriptor(cache, source, monkeypatch):
    repo, config, payload, calls = source
    cache.resolve('codex', repo, now=100)
    before = stored(cache, repo).read_bytes()
    payload['args'] = ['--sandbox', 'read-only']
    config.write_text(json.dumps(payload))
    original_open = cache.os.open
    descriptors = []
    def opened(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        descriptors.append(descriptor)
        return descriptor
    with monkeypatch.context() as patch:
        patch.setattr(cache.os, 'open', opened)
        patch.setattr(cache.fcntl, 'flock', lambda *a: (_ for _ in ()).throw(PermissionError('lock denied')))
        assert cache.resolve('codex', repo, now=101) == {**payload, 'cached': False}
    assert len(calls) == 2 and stored(cache, repo).read_bytes() == before
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_invalid_fresh_resolver_output_never_reaches_the_launch_frame(cache, source, monkeypatch):
    repo, _, _, _ = source
    monkeypatch.setattr(cache, 'resolve_uncached', lambda *a: {
        'args': '--sandbox danger-full-access', 'prefix': [], 'routing': None})
    with pytest.raises(ValueError, match='invalid resolved launch policy'):
        cache.resolve('codex', repo, now=100)
    assert not stored(cache, repo).exists()


def test_resolution_racing_a_save_retries_without_mixed_bundle(cache, source, monkeypatch):
    repo, config, payload, _ = source
    calls = []
    def fresh(*args):
        calls.append(1)
        value = json.loads(config.read_text())
        if len(calls) == 1:
            config.write_text(json.dumps({**payload, 'args': ['--sandbox', 'read-only']}))
        return value
    monkeypatch.setattr(cache, 'resolve_uncached', fresh)
    result = cache.resolve('codex', repo, now=100)
    assert result['args'] == ['--sandbox', 'read-only']
    assert len(calls) == 2


def test_continuous_edits_fail_without_publishing(cache, source, monkeypatch):
    repo, config, payload, _ = source
    def fresh(*args):
        config.write_text(config.read_text() + ' ')
        return payload
    monkeypatch.setattr(cache, 'resolve_uncached', fresh)
    with pytest.raises(ValueError, match='changed while resolving'):
        cache.resolve('codex', repo, now=100)
    assert not stored(cache, repo).exists()


def test_unavailable_cache_still_resolves_current_policy(cache, source, monkeypatch, tmp_path):
    repo, _, payload, calls = source
    invalid_root = tmp_path / 'cache-is-a-file'
    invalid_root.touch()
    monkeypatch.setenv('XDG_CACHE_HOME', str(invalid_root))
    assert cache.resolve('codex', repo, now=100) == {**payload, 'cached': False}
    assert len(calls) == 1


def test_cache_write_failure_is_nonfatal(cache, source, monkeypatch):
    repo, _, payload, _ = source
    monkeypatch.setattr(cache.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    assert cache.resolve('codex', repo, now=100) == {**payload, 'cached': False}
    assert not list(stored(cache, repo).parent.glob('.launch-policy-*'))


def test_frames_preserve_argument_boundaries(cache):
    routing = {'mode': 'local-only', 'model': 'model/name'}
    bundle = dict(args=['-c', 'mcp_servers."name with spaces".enabled=false'],
                  routing=routing, prefix=['python3', str(cache.LIB / 't_local_inference.py'),
                                           '--lead-policy', json.dumps(routing)])
    assert cache.frame(bundle).decode().split('\0') == ['4', *bundle['prefix'], *bundle['args']]
    assert cache.frame(dict(args=[], prefix=[], routing=None)) == b'0'
    with pytest.raises(ValueError, match='invalid launch frame'):
        cache.frame(dict(args=['bad\0argument'], prefix=[], routing=None))
    with pytest.raises(ValueError, match='unsupported launch tool'):
        cache.resolve('unknown')


def test_dependency_paths_cover_native_worker_mcp_and_source_inputs(cache, tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    paths = set(cache.dependency_paths('codex', repo))
    assert ROOT / 'libexec/t_launch_policy.py' in paths
    assert Path(os.environ['XDG_CONFIG_HOME']) / 't/workflow.json' in paths
    assert Path(os.environ['XDG_CONFIG_HOME']) / 't/local-worker.json' in paths
    assert Path(os.environ['CODEX_HOME']) / 'AGENTS.override.md' in paths
    assert Path(os.environ['CODEX_HOME']) / 'config.toml' in paths
    assert Path(os.environ['CLAUDE_CONFIG_DIR']) / 'CLAUDE.md' in paths
    assert repo / '.codex/config.toml' in paths


def test_fingerprint_tracks_symlink_target_and_dangling_transitions(cache, tmp_path):
    source, target = tmp_path / 'link', tmp_path / 'target'
    source.symlink_to(target)
    dangling = cache.fingerprint(source)
    assert dangling['target'] is None
    target.write_text('created')
    assert cache.fingerprint(source) != dangling
    source.unlink()
    assert cache.fingerprint(source) is None


def test_combined_resolver_pins_permissions_routing_and_common_scope(cache, tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    w = runpy.run_path(str(ROOT / 'libexec/t_workflow.py'))
    document = w['defaults']()
    policy = document['workflows']['standard']
    policy.update(stages_enabled=False,
                  session_defaults={'codex': {'model': 'cache-test-model', 'effort': 'high'}},
                  permissions={'codex': 'read_only'}, inference={'mode': 'automatic'})
    document['repositories'][str(repo)] = {'workflow': 'standard'}
    w['save'](document, None, propagate=False)
    first = cache.resolve('codex', repo, now=100)
    assert first['args'][:4] == ['--model', 'cache-test-model', '-c', 'model_reasoning_effort="high"']
    assert '--sandbox' in first['args'] and 'read-only' in first['args']
    assert first['routing']['mode'] == 'automatic'
    assert json.loads(first['prefix'][3]) == first['routing']
    assert cache.resolve('codex', repo, now=101)['cached']
    policy['permissions']['codex'] = 'full'
    policy['inference'] = {'mode': 'local-only'}
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    changed = cache.resolve('codex', repo, now=102)
    assert not changed['cached']
    assert 'danger-full-access' in changed['args']
    assert changed['routing']['mode'] == 'local-only'
    assert '--model' not in changed['args']
    w['config_path']().write_text('invalid policy')
    with pytest.raises(ValueError):
        cache.resolve('codex', repo, now=103)


def test_model_only_workflow_keeps_native_shell_wrapper_path(cache, tmp_path):
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    w = runpy.run_path(str(ROOT / 'libexec/t_workflow.py'))
    document = w['defaults']()
    document['workflows']['standard'].update(
        stages_enabled=False, session_defaults={'codex': {'model': 'native-model'}})
    w['save'](document, None, propagate=False)
    result = cache.resolve('codex', repo, now=100)
    assert result['args'] == ['--model', 'native-model']
    assert result['prefix'] == [] and result['routing'] is None


def test_prepared_prefix_cli_passes_policy_without_resolving_again(cache, monkeypatch):
    """The private CLI validates pinned routing without a second workflow lookup."""
    spec = importlib.util.spec_from_file_location('prepared_lead_test', ROOT / 'libexec/t_local_inference.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, 'effective', lambda *_: pytest.fail('second policy resolution'))
    monkeypatch.setattr(module, 'lead', lambda argv, **kwargs: calls.append((argv, kwargs)) or 7)
    assert module.prepared_lead(['codex'], json.dumps({'mode': 'local-only'})) == 7
    assert calls[0][0] == ['codex']
    assert calls[0][1]['chosen']['mode'] == 'local-only'
    for argv, payload in (([], '{}'), (['codex'], 'x' * 16385), (['codex'], 'null'),
                          (['codex'], '{broken'), (['codex'], '{"mode":"unsupported"}')):
        with pytest.raises(ValueError):
            module.prepared_lead(argv, payload)


def test_direct_provider_prefix_consumes_one_shot_trust_marker(cache, monkeypatch):
    spec = importlib.util.spec_from_file_location('direct_lead_test', ROOT / 'libexec/t_local_inference.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('_T_LAUNCH_TRUST_DONE', '/prepared/workspace')
    monkeypatch.setattr(module, 'effective', lambda *_: None)
    def native(argv):
        assert '_T_LAUNCH_TRUST_DONE' not in os.environ
        return 7
    monkeypatch.setattr(module.subprocess, 'call', native)
    assert module.lead(['codex']) == 7
