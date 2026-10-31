"""Snapshots and real bounded runner processes never touch the source checkout."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid

import pytest

from conftest import REPO_ROOT, load_script

job = load_script(REPO_ROOT / 'libexec/t_runner_job.py', 't_runner_job_tests')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'Test')
    (root / 'tracked').write_text('original')
    (root / 'deleted').write_text('delete me')
    (root / '.gitignore').write_text('ignored\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'initial')
    return root


def snapshot(repo, tmp_path, **kwargs):
    directory = tmp_path / str(uuid.uuid4())
    directory.mkdir()
    archive, manifest = directory / 'snapshot.tar', directory / 'manifest.json'
    data = job.create_snapshot(repo, archive, manifest, **kwargs)
    return directory, archive, manifest, data


def run(repo, tmp_path, code, **kwargs):
    directory, archive, manifest, data = snapshot(repo, tmp_path)
    result = job.run_job(directory, archive, manifest, [sys.executable, '-c', code],
                         kwargs.pop('timeout', 3), str(uuid.uuid4()), **kwargs)
    return directory, result


def test_snapshot_current_tree_and_explicit_untracked(repo, tmp_path):
    (repo / 'tracked').write_text('staged')
    git(repo, 'add', 'tracked')
    (repo / 'tracked').write_text('working tree')
    (repo / 'deleted').unlink()
    (repo / 'extra').mkdir()
    (repo / 'extra/file').write_text('included')
    (repo / 'ignored').write_text('ignored')
    (repo / 'unrelated').write_text('not included')
    for secret in ('.env', '.env.local', 'credentials.json', '.npmrc', 'private.pem'):
        (repo / secret).write_text('secret')
        git(repo, 'add', '-f', secret)
    head, index = git(repo, 'rev-parse', 'HEAD'), (repo / '.git/index').read_bytes()
    _, archive, _, manifest = snapshot(repo, tmp_path, include=['extra', 'ignored'])
    payloads = dict((record['path'], payload) for record, payload in job.validate_archive(archive, manifest))
    assert payloads == {'.gitignore': b'ignored\n', 'extra/file': b'included', 'tracked': b'working tree'}
    assert manifest['source_head'] == head.decode().strip()
    assert git(repo, 'rev-parse', 'HEAD') == head
    assert (repo / '.git/index').read_bytes() == index
    assert str(repo) not in json.dumps(manifest)
    _, _, _, default = snapshot(repo, tmp_path)
    assert 'extra/file' not in [entry['path'] for entry in default['files']]


def test_snapshot_exec_and_unborn(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'exec').write_text('#!/bin/sh\n')
    (repo / 'exec').chmod(0o755)
    git(repo, 'add', 'exec')
    _, archive, _, manifest = snapshot(repo, tmp_path)
    assert manifest['source_head'] is None
    destination = tmp_path / 'extracted'
    job.extract_snapshot(archive, manifest, destination)
    assert (destination / 'exec').stat().st_mode & 0o777 == 0o755
    with pytest.raises(ValueError, match='must be new'):
        job.extract_snapshot(archive, manifest, destination)


@pytest.mark.parametrize('path', ['', '.', '..', '../x', '/x', 'a/../x', 'a//b', 'a\\b', 'a\x00b', 'a:b'])
def test_unsafe_paths(path):
    with pytest.raises(ValueError):
        job.safe_path(path)


def test_snapshot_rejects_links_submodules_and_bounds(repo, tmp_path):
    with pytest.raises(ValueError, match='count'):
        snapshot(repo, tmp_path, max_files=1)
    with pytest.raises(ValueError, match='limit'):
        snapshot(repo, tmp_path, max_bytes=2)
    (repo / 'tracked').unlink()
    (repo / 'tracked').symlink_to('/etc/passwd')
    with pytest.raises(OSError):
        snapshot(repo, tmp_path)
    git(repo, 'add', 'tracked')
    with pytest.raises(ValueError, match='unsupported'):
        snapshot(repo, tmp_path)
    git(repo, 'update-index', '--add', '--cacheinfo', '160000,' + git(repo, 'rev-parse', 'HEAD').decode().strip() + ',submodule')
    with pytest.raises(ValueError, match='unsupported'):
        snapshot(repo, tmp_path)


def test_descriptor_safety_and_races(repo, tmp_path, monkeypatch):
    (repo / 'dir').mkdir()
    (repo / 'dir/file').write_text('file')
    assert job.read_regular(repo, 'dir/file')[0] == b'file'
    (repo / 'link').symlink_to('dir', target_is_directory=True)
    with pytest.raises(OSError):
        job.read_regular(repo, 'link/file')
    assert job.read_regular(repo / 'link', 'file')[0] == b'file'
    os.mkfifo(repo / 'pipe')
    with pytest.raises(ValueError, match='regular'):
        job.read_regular(repo, 'pipe')
    original = os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            (repo / 'tracked').write_text('modified while reading')
        return data

    monkeypatch.setattr(job.os, 'read', race)
    with pytest.raises(ValueError, match='changed'):
        job.read_regular(repo, 'tracked')


def test_ancestor_swap_race(repo, monkeypatch):
    (repo / 'dir').mkdir()
    (repo / 'dir/file').write_text('file')
    original = os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            (repo / 'dir').rename(repo / 'old')
            (repo / 'dir').mkdir()
        return data

    monkeypatch.setattr(job.os, 'read', race)
    with pytest.raises(ValueError, match='directory changed'):
        job.read_regular(repo, 'dir/file')


def make_archive(tmp_path, names):
    archive = tmp_path / 'hostile.tar'
    files = []
    with tarfile.open(archive, 'w') as tar:
        for name, content, kind in names:
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.type = len(content), 0o644, kind
            if kind == tarfile.SYMTYPE:
                member.linkname = '/etc/passwd'
            tar.addfile(member, io.BytesIO(content))
            files.append({'path': name, 'size': len(content), 'mode': 0o644,
                          'sha256': hashlib.sha256(content).hexdigest()})
    return archive, {'version': 1, 'id': str(uuid.uuid4()), 'files': files,
                     'total_bytes': sum(f['size'] for f in files)}


@pytest.mark.parametrize('names', [[('../escape', b'x', tarfile.REGTYPE)],
                                   [('a', b'x', tarfile.SYMTYPE)],
                                   [('a', b'x', tarfile.REGTYPE), ('a', b'x', tarfile.REGTYPE)],
                                   [('.env', b'secret', tarfile.REGTYPE)]])
def test_extract_rejects_all_before_writing(tmp_path, names):
    archive, manifest = make_archive(tmp_path, names)
    destination = tmp_path / 'output'
    with pytest.raises(ValueError):
        job.extract_snapshot(archive, manifest, destination)
    assert not destination.exists()
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('change,match', [
    (lambda m: m.update(version=2), 'version'),
    (lambda m: m.update(total_bytes=2), 'size mismatch'),
    (lambda m: m['files'][0].update(sha256='0' * 64), 'digest'),
    (lambda m: m['files'][0].update(sha256='0'), 'digest'),
    (lambda m: m['files'][0].update(mode=0o777), 'member'),
    (lambda m: m['files'][0].update(size=2), 'size mismatch'),
    (lambda m: m.update(files=[] ,total_bytes=0), 'unexpected'),
])
def test_archive_integrity(tmp_path, change, match):
    archive, manifest = make_archive(tmp_path, [('a', b'x', tarfile.REGTYPE)])
    change(manifest)
    with pytest.raises(ValueError, match=match):
        job.validate_archive(archive, manifest)


def test_archive_limits_and_missing_member(tmp_path):
    archive, manifest = make_archive(tmp_path, [('a', b'x', tarfile.REGTYPE)])
    with pytest.raises(ValueError, match='count'):
        job.validate_archive(archive, manifest, max_files=0)
    with pytest.raises(ValueError, match='size limit'):
        job.validate_archive(archive, manifest, max_bytes=0)
    with tarfile.open(archive, 'w'):
        pass
    with pytest.raises(ValueError, match='missing'):
        job.validate_archive(archive, manifest)


def test_real_success_clean_environment_artifacts_cleanup(repo, tmp_path, monkeypatch):
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'hidden')
    monkeypatch.setenv('GITHUB_TOKEN', 'hidden')
    monkeypatch.setenv('COV_CORE_SOURCE', 'hidden')
    code = """import os,pathlib,json
assert 'AWS_SECRET_ACCESS_KEY' not in os.environ
assert 'GITHUB_TOKEN' not in os.environ
assert not any(k.startswith('COV_CORE_') for k in os.environ)
assert pathlib.Path.home().is_dir()
assert pathlib.Path.home() != pathlib.Path.cwd()
assert not pathlib.Path('.git').exists()
pathlib.Path('tracked').write_text('isolated')
pathlib.Path('out').mkdir()
pathlib.Path('out/report').write_text('evidence')
print('runner success')
"""
    directory, result = run(repo, tmp_path, code, artifacts=['out'], expected_os=job.probe()['os'], expected_arch=job.probe()['arch'])
    assert result['status'] == 'succeeded' and result['exit_code'] == 0
    assert (directory / 'output.log').read_text() == 'runner success\n'
    assert result['artifacts'][0]['path'] == 'out/report'
    assert (repo / 'tracked').read_text() == 'original'
    assert not list(directory.glob('runtime-*'))
    assert not (directory / 'snapshot.tar').exists()
    assert not (directory / 'manifest.json').exists()
    assert json.loads((directory / 'result.json').read_text()) == result


def test_real_failure_timeout_and_cancel(repo, tmp_path):
    _, result = run(repo, tmp_path, 'raise SystemExit(7)')
    assert result['status'] == 'failed' and result['exit_code'] == 7
    directory, result = run(repo, tmp_path, 'import time;time.sleep(30)', timeout=.1)
    assert result['status'] == 'timed_out' and result['exit_code'] == -signal.SIGKILL
    directory, archive, manifest, _ = snapshot(repo, tmp_path)
    token = str(uuid.uuid4())
    (directory / 'token').write_text(token)
    with pytest.raises(ValueError, match='token'):
        job.cancel_job(directory, str(uuid.uuid4()))
    job.cancel_job(directory, token)
    result = job.run_job(directory, archive, manifest, [sys.executable, '-c', 'import time;time.sleep(30)'], 3, token)
    assert result['status'] == 'cancelled' and result['exit_code'] == -signal.SIGKILL


def test_cancel_running_job_and_kill_descendants(repo, tmp_path):
    directory, archive, manifest, _ = snapshot(repo, tmp_path)
    token = str(uuid.uuid4())
    outcome = []
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print(p.pid,flush=True);time.sleep(30)"
    thread = threading.Thread(target=lambda: outcome.append(job.run_job(directory, archive, manifest, [sys.executable, '-c', code], 5, token)))
    thread.start()
    deadline = time.monotonic() + 3
    while not (directory / 'output.log').exists() or not (directory / 'output.log').stat().st_size:
        assert time.monotonic() < deadline
        time.sleep(.02)
    child = int((directory / 'output.log').read_text())
    job.cancel_job(directory, token)
    thread.join(3)
    assert not thread.is_alive()
    assert outcome[0]['status'] == 'cancelled'
    # A terminated child may remain a zombie briefly until adopted/reaped.
    status = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
    assert not status or status.startswith('Z')


def test_stale_cancel_marker_and_log_bound(repo, tmp_path):
    directory, archive, manifest, _ = snapshot(repo, tmp_path)
    token = str(uuid.uuid4())
    (directory / 'cancel').write_text(str(uuid.uuid4()))
    workspace = directory / 'workspace'
    job.extract_snapshot(archive, json.loads(manifest.read_text()), workspace)
    result = job.execute([sys.executable, '-c', "print('x'*10000)"], workspace, directory / 'home', directory, token, 3, max_log=100)
    assert result['status'] == 'succeeded'
    assert result['log_truncated'] and (directory / 'output.log').stat().st_size == 100
    with pytest.raises(ValueError, match='command and timeout'):
        job.execute([], workspace, directory / 'home', directory, token, 0)


@pytest.mark.parametrize('kwargs,match', [({'expected_os': 'wrong'}, 'OS'), ({'expected_arch': 'wrong'}, 'architecture')])
def test_profile_probe_mismatch(repo, tmp_path, kwargs, match):
    directory, result = run(repo, tmp_path, 'raise Exception()', **kwargs)
    assert result['status'] == 'error' and match in result['error']
    assert not list(directory.glob('runtime-*'))


def test_job_error_and_artifact_safety(repo, tmp_path):
    directory, result = run(repo, tmp_path, "import pathlib;pathlib.Path('leak').symlink_to('/etc/passwd')", artifacts=['leak'])
    assert result['status'] == 'succeeded' and 'artifact_error' in result
    assert result['exit_code'] == 0
    assert not (directory / 'artifacts.tar').exists()
    _, result = run(repo, tmp_path, '', artifacts=['.env'])
    assert result['status'] == 'succeeded' and 'excluded' in result['artifact_error']
    _, result = run(repo, tmp_path, '', artifacts=['missing'])
    assert result['status'] == 'succeeded' and 'artifact_error' in result
    _, result = run(repo, tmp_path, '', artifacts=['../escape'])
    assert result['status'] == 'succeeded' and 'artifact_error' in result
    directory, archive, manifest, _ = snapshot(repo, tmp_path)
    token = str(uuid.uuid4())
    (directory / 'token').write_text(str(uuid.uuid4()))
    with pytest.raises(ValueError, match='token'):
        job.run_job(directory, archive, manifest, ['true'], 1, token)


def test_collect_artifact_bounds_and_nested_safety(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'out').mkdir()
    (workspace / 'out/file').write_text('evidence')
    archive = tmp_path / 'artifact.tar'
    with pytest.raises(ValueError, match='count'):
        job.collect_artifacts(workspace, ['out'], archive, max_files=0)
    with pytest.raises(ValueError, match='limit'):
        job.collect_artifacts(workspace, ['out'], archive, max_bytes=1)
    (workspace / 'out/link').symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='symlink'):
        job.collect_artifacts(workspace, ['out'], archive)
    (workspace / 'out/link').unlink()
    (workspace / 'out/.env').write_text('secret')
    with pytest.raises(ValueError, match='excluded'):
        job.collect_artifacts(workspace, ['out'], archive)


def test_cli_probe_and_run(repo, tmp_path):
    helper = REPO_ROOT / 'libexec/t_runner_job.py'
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    assert json.loads(subprocess.check_output([sys.executable, str(helper), 'probe'], env=env)) == job.probe()
    directory, archive, manifest, _ = snapshot(repo, tmp_path)
    command = [sys.executable, str(helper), 'run', '--job-dir', str(directory), '--archive', str(archive),
               '--manifest', str(manifest), '--timeout', '3', '--token', str(uuid.uuid4()), '--', sys.executable, '-c', 'print("cli")']
    assert subprocess.run(command, env=env).returncode == 0
    assert (directory / 'output.log').read_text() == 'cli\n'
    assert job._normal_os('macos') == 'darwin'
    assert job._normal_arch('aarch64') == 'arm64'


def test_root_descriptor_failure_closes_open_ancestors(repo, monkeypatch):
    original = os.open
    descriptors = []

    def fail_second(path, flags, **kwargs):
        if descriptors:
            raise OSError('ancestor vanished')
        descriptor = original(path, flags, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(job.os, 'open', fail_second)
    with pytest.raises(OSError, match='ancestor vanished'):
        job.read_regular(repo, 'tracked')
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_growing_file_and_oversized_transfer(repo, tmp_path, monkeypatch):
    (repo / 'one').write_text('a')
    monkeypatch.setattr(job.os, 'read', lambda descriptor, count: b'ab')
    with pytest.raises(ValueError, match='limit'):
        job.read_regular(repo, 'one', 1)
    archive, manifest = make_archive(tmp_path, [('a', b'x', tarfile.REGTYPE)])
    with archive.open('ab') as stream:
        stream.write(b'\0' * 20000)
    with pytest.raises(ValueError, match='transfer size'):
        job.validate_archive(archive, manifest, max_bytes=1, max_files=1)


def test_artifact_file_selection_count(tmp_path):
    (tmp_path / 'a').write_text('a')
    (tmp_path / 'b').write_text('b')
    with pytest.raises(ValueError, match='count'):
        job.collect_artifacts(tmp_path, ['a', 'b'], tmp_path / 'out.tar', max_files=1)


def test_escaped_descendant_cannot_hold_result_open(repo, tmp_path):
    # This is deliberately outside the job's process group. The helper must
    # finish draining its inherited pipe within a bound; the test owns cleanup.
    child_pid = None
    try:
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(15)'],start_new_session=True);print(p.pid,flush=True)"
        before = time.monotonic()
        directory, result = run(repo, tmp_path, code)
        child_pid = int((directory / 'output.log').read_text())
        assert result['status'] == 'succeeded'
        assert time.monotonic() - before < 4
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_artifact_nested_directory_ancestor_link_and_special(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'out/sub').mkdir(parents=True)
    (workspace / 'out/sub/report').write_text('nested')
    records = job.collect_artifacts(workspace, ['out'], tmp_path / 'nested.tar')
    assert records[0]['path'] == 'out/sub/report'
    (workspace / 'link').symlink_to('out', target_is_directory=True)
    with pytest.raises(OSError):
        job.collect_artifacts(workspace, ['link/sub'], tmp_path / 'link.tar')
    os.mkfifo(workspace / 'pipe')
    with pytest.raises(ValueError, match='special'):
        job.collect_artifacts(workspace, ['pipe'], tmp_path / 'pipe.tar')


@pytest.mark.parametrize('timeout', [float('nan'), float('inf'), -1, 86401])
def test_timeout_must_be_finite_bounded(tmp_path, timeout):
    with pytest.raises(ValueError, match='timeout'):
        job.execute(['true'], tmp_path, tmp_path, tmp_path, str(uuid.uuid4()), timeout)


def test_artifact_discovery_detects_directory_replacement(tmp_path, monkeypatch):
    (tmp_path / 'out').mkdir()
    (tmp_path / 'out/report').write_text('evidence')
    original = os.fwalk

    def race(*args, **kwargs):
        for row in original(*args, **kwargs):
            yield row
        (tmp_path / 'out').rename(tmp_path / 'old')
        (tmp_path / 'out').mkdir()

    monkeypatch.setattr(job.os, 'fwalk', race)
    with pytest.raises(ValueError, match='changed'):
        job.collect_artifacts(tmp_path, ['out'], tmp_path / 'out.tar')


def test_snapshot_git_ignores_inherited_repository_and_requires_root(repo, tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_DIR', '/does/not/exist')
    monkeypatch.setenv('GIT_INDEX_FILE', '/does/not/exist')
    _, archive, _, manifest = snapshot(repo, tmp_path)
    assert manifest['source_head']
    assert job.validate_archive(archive, manifest)
    (repo / 'sub').mkdir()
    with pytest.raises(ValueError, match='repository root'):
        snapshot(repo / 'sub', tmp_path)


def test_snapshot_preserves_long_regular_paths(repo, tmp_path):
    relative = '/'.join(['x' * 80] * 4) + '/file'
    path = repo / relative
    path.parent.mkdir(parents=True)
    path.write_text('long path')
    git(repo, 'add', relative)
    _, archive, _, manifest = snapshot(repo, tmp_path)
    payloads = dict((record['path'], data) for record, data in job.validate_archive(archive, manifest))
    assert payloads[relative] == b'long path'
    with pytest.raises(ValueError, match='portable limit'):
        job.safe_path('x' * 4097)
