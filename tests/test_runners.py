"""Runner receipts and fake private/SSH transports never touch real accounts."""
import base64
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runners(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('runners_test', ROOT / 'libexec/t_runners.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    return module


def config(provider='ssh'):
    target = {'provider': provider, 'os': 'linux', 'arch': 'x64', 'capabilities': ['gpu']}
    target.update({'host': 'user@worker'} if provider == 'ssh' else {'repository': 'owner/private', 'label': 'ubuntu-latest'})
    return {'runners': {'targets': {'worker': target}, 'default_targets': ['worker'],
                        'timeout_seconds': 5, 'queue_timeout_seconds': 3, 'artifacts': ['base.txt']}}


def receipt(r, tmp_path, provider='ssh', status='unknown'):
    job_id = str(uuid.uuid4())
    directory = r._job_dir(job_id)
    (directory / 'worker').mkdir(parents=True)
    target = {**config(provider)['runners']['targets']['worker'], 'type': provider,
              'status': status, 'token': str(uuid.uuid4()), 'remote_path': '/tmp/t-runner-'+job_id+'-worker',
              'repository': 'owner/private', 'repository_id': 12, 'workflow_id': 7, 'release_tag': 't-runner-'+job_id,
              'release_id':21,
              'resource_created': True, 'dispatched': provider == 'github'}
    target['asset_ids'] = {'snapshot.tar':1,'manifest.json':2,'t_runner_job.py':3,'worker.json':4}
    target['snapshot_id'] = str(uuid.uuid4())
    value = {'id': job_id, 'created_at': 1, 'status': status, 'targets': {'worker': target}, 'resources': [],
             'snapshot':{'archive_sha256':'a'*64,'manifest_sha256':'b'*64}}
    r._save(value, directory)
    return value, directory, target


def test_target_selection_and_receipt_paths(runners, tmp_path, monkeypatch):
    r = runners
    assert r.state_path(tmp_path) == tmp_path
    assert r.select_targets(config())['worker']['type'] == 'ssh'
    assert r._normalized_arch('x64') == 'x86_64'
    assert r._normalized_os('macos') == 'darwin'
    with pytest.raises(ValueError, match='no runner targets'):
        r.select_targets(config(), os_filter='macos')
    with pytest.raises(ValueError, match='no runner targets'):
        r.select_targets(config(), capabilities=['metal'])
    assert r.select_targets(config(), targets='worker', capabilities=['gpu'])
    for bad in ['bad', '../bad', str(uuid.uuid4()).upper()]:
        with pytest.raises(ValueError, match='UUID'):
            r.status(bad)
    with pytest.raises(ValueError, match='unknown runner job'):
        r.status(str(uuid.uuid4()))
    assert r.list_jobs() == []
    assert not r.state_path().exists()
    for mutate, match in [
        (lambda c: c['runners'].update(targets=[]), 'mapping'),
        (lambda c: c['runners'].update(default_targets=['missing']), 'unknown'),
        (lambda c: c['runners']['targets']['worker'].update(provider='other'), 'transport'),
        (lambda c: c['runners']['targets']['worker'].update(os='bad'), 'OS'),
        (lambda c: c['runners']['targets']['worker'].update(arch='bad'), 'architecture'),
        (lambda c: c['runners']['targets']['worker'].update(host='-oProxyCommand=oops'), 'host'),
    ]:
        data = config()
        mutate(data)
        with pytest.raises(ValueError, match=match):
            r.select_targets(data)
    data = config('github')
    data['runners']['targets']['worker']['label'] = None
    with pytest.raises(ValueError, match='GitHub'):
        r.select_targets(data)


def test_local_inspection_and_safe_artifacts(runners, tmp_path, monkeypatch):
    r = runners
    value, directory, target = receipt(r, tmp_path, status='succeeded')
    (directory/'worker/output.log').write_text('command output')
    bundle = directory/'worker/artifacts.tar'
    with tarfile.open(bundle, 'w') as archive:
        item = tarfile.TarInfo('report/output.txt')
        item.size = 3
        archive.addfile(item, io.BytesIO(b'yes'))
    target['result'] = {'artifacts':[{'path':'report/output.txt','mode':0o644,'size':3,
                                     'sha256':hashlib.sha256(b'yes').hexdigest()}]}
    r._save(value,directory)
    monkeypatch.setattr(r, '_api', lambda *a, **kw: pytest.fail('read-only inspection used network'))
    before = (directory/'receipt.json').stat().st_mtime_ns
    assert r.list_jobs()[0]['id'] == value['id']
    assert r.status(value['id'])['id'] == value['id']
    assert r.logs(value['id']) == {'worker': 'command output'}
    assert r.artifacts(value['id']) == {'worker': str(bundle)}
    extracted = r.artifacts(value['id'], destination=tmp_path/'out')
    assert (Path(extracted['worker'])/'report/output.txt').read_text() == 'yes'
    assert (directory/'receipt.json').stat().st_mtime_ns == before
    for function in (r.logs, r.artifacts):
        with pytest.raises(ValueError, match='unknown target'):
            function(value['id'], target='missing')
    bundle.unlink()
    assert r.artifacts(value['id']) == {}
    (directory/'worker/output.log').unlink()
    assert r.logs(value['id']) == {'worker': ''}
    for name, kind in [('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
        with tarfile.open(bundle, 'w') as archive:
            item = tarfile.TarInfo(name)
            item.type = kind
            archive.addfile(item)
        with pytest.raises(ValueError):
            r.artifacts(value['id'], destination=tmp_path/'unsafe')


def test_run_wrapper_and_api(runners, monkeypatch):
    r = runners
    calls = []
    monkeypatch.setattr(r.subprocess, 'run', lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0,stdout='{"id":1}',stderr=''))
    assert r._api('test', 'POST', {'x': '$(not shell)'}) == {'id': 1}
    assert json.loads(calls[0][1]['input']) == {'x': '$(not shell)'}
    r._ssh('host', 'printf test')
    assert calls[-1][0][-1] == "zsh -lic 'printf test'"
    assert 'ForwardAgent=no' in calls[-1][0]
    monkeypatch.setattr(r.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout='',stderr='broken'))
    with pytest.raises(ValueError, match='broken'):
        r._run(['bad'])
    def fail(*a, **kw):
        raise subprocess.TimeoutExpired('bad', 1)
    monkeypatch.setattr(r.subprocess, 'run', fail)
    with pytest.raises(ValueError, match='transport failed'):
        r._run(['bad'])


def test_setup_private_identity_and_atomic_preflight(runners, monkeypatch):
    r = runners
    templates = r._templates()
    writes = []
    state = {'installed': False, 'private': True, 'id': 12, 'bad': False, 'unmanaged': False}
    def api(path, method='GET', data=None):
        if path == 'repos/owner/private':
            return {'id': state['id'], 'private': state['private'], 'default_branch': 'main'}
        if path.endswith('/contents'):
            return [{'name': 'existing'}] if state['unmanaged'] else []
        if '/contents/' in path:
            relative = path.split('/contents/')[1]
            if method == 'PUT':
                writes.append(relative)
                return {}
            if not state['installed']:
                raise r.RunnerError('HTTP 404')
            content = templates[relative]
            if state['bad'] and relative == r.SWEEPER:
                content = 'modified'
            return {'content': base64.b64encode(content.encode()).decode()}
        return {'path': r.WORKFLOW, 'state': 'active', 'id': 7}
    monkeypatch.setattr(r, '_api', api)
    monkeypatch.setattr(r, '_run', lambda args, **kw: writes.append(args))
    with pytest.raises(ValueError, match='OWNER/REPO'):
        r.setup_github('bad')
    with pytest.raises(ValueError, match='OWNER/REPO'):
        r._private('-bad')
    with pytest.raises(ValueError, match='setup'):
        r._broker('owner/private', None)
    binding = r.setup_github('owner/private', create=True)
    assert binding['repository_id'] == 12
    assert len(writes) == 4
    state['installed'] = True
    assert r._broker('owner/private', None)['workflow_id'] == 7
    writes.clear()
    r.setup_github('owner/private')
    assert writes == []
    state['bad'] = True
    with pytest.raises(ValueError, match='incompatible'):
        r.setup_github('owner/private')
    assert writes == []
    with pytest.raises(ValueError, match='protocol changed'):
        r._broker('owner/private', None)
    state.update(bad=False, installed=False, unmanaged=True)
    with pytest.raises(ValueError, match='not empty'):
        r.setup_github('owner/private')
    assert writes == []
    state['private'] = False
    with pytest.raises(ValueError, match='private'):
        r._private('owner/private')
    state.update(private=True, id=13)
    with pytest.raises(ValueError, match='identity'):
        r._private('owner/private', 12)
    state['id'] = None
    with pytest.raises(ValueError, match='immutable'):
        r._private('owner/private')


def fake_snapshot(root, archive, manifest_path, include=()):
    archive.write_bytes(b'snapshot')
    manifest_path.write_text('{}')
    return {'id': str(uuid.uuid4()), 'files': [], 'source_head': 'abc'}


def command_result(target, status='failed', code=17):
    return {'status':status,'exit_code':code,'id':target['token'],'snapshot_id':target['snapshot_id']}


def test_start_concurrent_ssh_and_failure_cleanup(runners, monkeypatch, tmp_path):
    r = runners
    monkeypatch.setattr(r, '_helper', lambda: SimpleNamespace(create_snapshot=fake_snapshot))
    calls = []
    def ssh(host, command, timeout=60):
        calls.append(command)
        if command.startswith('uname'):
            return 'Linux\nx86_64\n'
        return ''
    monkeypatch.setattr(r, '_ssh', ssh)
    def run(args, **kw):
        calls.append(args)
        if args[0] == 'scp' and ':/' in args[-2]:
            path = Path(args[-1])
            if path.name == 'result.json':
                current=r.status(path.parents[1].name)['targets'][path.parent.name]
                path.write_text(json.dumps(command_result(current)))
            elif path.name == 'output.log':
                path.write_text('test failed')
            else:
                raise r.RunnerError('no artifacts')
        return ''
    monkeypatch.setattr(r, '_run', run)
    data = config()
    data['runners']['targets']['other'] = dict(data['runners']['targets']['worker'])
    data['runners']['default_targets'].append('other')
    value = r.start(tmp_path, data, ['printf', 'space;$(literal)'], artifacts=['extra.txt'])
    assert value['exit_code'] == 17 and value['status'] == 'failed'
    assert len(value['targets']) == 2
    directory = r._job_dir(value['id'])
    assert not (directory/'snapshot.tar').exists()
    assert all(t['cleaned'] for t in value['targets'].values())
    assert json.loads((directory/'worker.json').read_text())['artifacts'] == ['base.txt','extra.txt']
    assert any('space;$(literal)' in c for c in calls if isinstance(c,str))
    for bad in [[], 'echo', ['bad\0arg']]:
        with pytest.raises(ValueError, match='argv'):
            r.start(tmp_path, config(), bad)
    for bad in [0, True, 21601]:
        with pytest.raises(ValueError, match='deadlines'):
            r.start(tmp_path, config(), ['true'], timeout=bad)
    monkeypatch.setattr(r, '_ssh', lambda *a, **kw: 'Darwin\narm64\n')
    value = r.start(tmp_path, config(), ['true'])
    assert value['targets']['worker']['status'] == 'error'
    assert 'OS/architecture' in value['targets']['worker']['error']


def test_start_snapshot_error_and_interrupt(runners, monkeypatch, tmp_path):
    r = runners
    def fail(*a, **kw):
        raise ValueError('snapshot failed')
    monkeypatch.setattr(r, '_helper', lambda: SimpleNamespace(create_snapshot=fail))
    value = r.start(tmp_path, config(), ['true'])
    assert value['status'] == 'error' and value['exit_code'] == 1
    assert r.status(value['id'])['error'] == 'snapshot failed'


def test_github_exact_run_and_provider_result(runners, monkeypatch, tmp_path):
    r = runners
    value, directory, target = receipt(r, tmp_path, 'github')
    target.update(branch='main', release_id=21, label='ubuntu-latest')
    calls = []
    run = {'id': 100, 'workflow_id': 7, 'display_title': 't-runner-'+value['id']+'-worker',
           'status': 'completed', 'conclusion': 'success'}
    def api(path, method='GET', data=None):
        calls.append((path,method,data))
        return {'workflow_runs': [{'id': 50, 'workflow_id': 7, 'display_title': 'other'}, run]}
    monkeypatch.setattr(r, '_api', api)
    def download(args, **kw):
        out = Path(kw['output_path'])
        out.write_text(json.dumps(command_result(target,code=23)) if out.name == 'result.json' else 'output')
        return ''
    monkeypatch.setattr(r, '_run', download)
    monkeypatch.setattr(r, '_assets',lambda *a:[{'name':'worker-'+name,'id':i,'size':len(json.dumps(command_result(target,code=23))) if name=='result.json' else 6}
        for i,name in enumerate(['result.json','output.log','artifacts.tar'])])
    r._github_target(value, 'worker', target, directory, 5, 3)
    assert target['status'] == 'failed' and target['exit_code'] == 23
    assert target['provider_conclusion'] == 'success'
    assert calls[0][2]['inputs']['job_id'] == value['id']
    assert r._find_run(target, value['id'], 'none') is None
    monkeypatch.setattr(r, '_api', lambda *a, **kw: {'workflow_runs':[run,run]})
    with pytest.raises(ValueError, match='multiple'):
        r._find_run(target, value['id'], 'worker')
    for invalid in [{}, {'status':'succeeded','exit_code':True}, {'status':'succeeded','exit_code':None}]:
        r._write(directory/'worker/result.json', invalid)
        with pytest.raises(ValueError, match='valid command'):
            r._result(target, directory/'worker/result.json')
    r._write(directory/'worker/result.json', command_result(target,'error',None))
    r._result(target, directory/'worker/result.json')
    assert target['status'] == 'error' and target['exit_code'] == 1


def test_github_start_resources_and_terminal_cleanup(runners, monkeypatch, tmp_path):
    r = runners
    calls = []
    monkeypatch.setattr(r, '_helper', lambda: SimpleNamespace(create_snapshot=fake_snapshot))
    monkeypatch.setattr(r, '_broker', lambda *a: {'repository':'owner/private','repository_id':12,'branch':'main','workflow_id':7})
    monkeypatch.setattr(r, '_private', lambda *a: {'id':12})
    def api(path, method='GET', data=None):
        # A receipt exists before the first remote write.
        assert r.list_jobs()
        calls.append((path,method,data))
        if '/assets?' in path:
            return [{'name':name,'id':index} for index,name in enumerate(['snapshot.tar','manifest.json','t_runner_job.py','worker.json'])]
        return {'id':21}
    monkeypatch.setattr(r, '_api', api)
    monkeypatch.setattr(r, '_run', lambda args, **kw: calls.append(args) or '')
    def target(receipt, name, target, directory, timeout, queue_timeout):
        assert (directory/'snapshot.tar').is_file()
        target.update(status='succeeded', exit_code=0)
    monkeypatch.setattr(r, '_github_target', target)
    value = r.start(tmp_path, config('github'), ['true'])
    assert value['status'] == 'succeeded' and value['exit_code'] == 0
    assert value['resources'][0]['status'] == 'deleted'
    assert any(isinstance(row,list) and row[:3] == ['gh','release','upload'] for row in calls)


def test_cancel_bounded_and_cleanup_reconciliation(runners, monkeypatch, tmp_path):
    r = runners
    value, directory, target = receipt(r,tmp_path,'github')
    monkeypatch.setattr(r, '_private', lambda *a: {})
    monkeypatch.setattr(r, '_find_run', lambda *a: {'id':100,'status':'queued'})
    monkeypatch.setattr(r, '_cancel_github', lambda *a, **kw: False)
    cancelled = r.cancel(value['id'])
    assert cancelled['targets']['worker']['status'] == 'unknown'
    assert (directory/'cancel.requested').exists()
    monkeypatch.setattr(r, '_find_run', lambda *a: {'id':100,'status':'completed','conclusion':'cancelled'})
    def unavailable(*a, **kw):
        raise r.RunnerError('no result')
    monkeypatch.setattr(r, '_download_result', unavailable)
    result = r.cleanup(value['id'])
    assert result['targets']['worker']['status'] == 'cancelled'
    assert result['exit_code'] == 130
    value, directory, target = receipt(r,tmp_path,'ssh')
    commands = []
    monkeypatch.setattr(r, '_ssh', lambda host, command, **kw: commands.append(command) or '')
    def download(target,name,directory):
        r._write(directory/name/'result.json', command_result(target,code=9))
        (directory/name/'output.log').write_text('retained after cancel')
        r._result(target,directory/name/'result.json')
    monkeypatch.setattr(r, '_download_ssh_result', download)
    cancelled = r.cancel(value['id'])
    assert cancelled['targets']['worker']['status'] == 'unknown'
    result = r.cleanup(value['id'])
    assert result['targets']['worker']['cleaned'] is True
    assert result['exit_code'] == 9
    assert any(command.startswith('rm -rf -- /tmp/t-runner-') for command in commands)
    value, directory, target = receipt(r,tmp_path,'ssh',status='queued')
    target['resource_created'] = False
    r._save(value,directory)
    assert r.cancel(value['id'])['targets']['worker']['status'] == 'cancelled'


@pytest.mark.parametrize('provider', ['github', 'ssh'])
@pytest.mark.parametrize('saved_status', ['preparing', 'queued', 'running'])
@pytest.mark.parametrize('completed', [False, True])
def test_cleanup_recovers_receipt_after_controller_disappears(runners, monkeypatch, tmp_path,
                                                             provider, saved_status, completed):
    r = runners
    value, directory, target = receipt(r, tmp_path, provider, status=saved_status)
    if provider == 'github':
        value['resources'] = [{'type': 'github', 'repository': 'owner/private',
                               'repository_id': 12, 'tag': target['release_tag'], 'status': 'created'}]
    r._save(value, directory)
    mutations = []
    monkeypatch.setattr(r, '_private', lambda *a: {})
    monkeypatch.setattr(r, '_find_run', lambda *a: {
        'id': 100, 'status': 'completed' if completed else 'queued', 'conclusion': 'failure'})
    monkeypatch.setattr(r, '_run', lambda *a, **kw: mutations.append(a) or '')
    monkeypatch.setattr(r, '_ssh', lambda *a, **kw: mutations.append(a) or '')
    def download(target, name, directory):
        if not completed:
            raise r.RunnerError('result not yet available')
        r._write(directory / name / 'result.json', command_result(target, code=17))
        (directory / name / 'output.log').write_text('retained after controller exit')
        r._result(target, directory / name / 'result.json')
    monkeypatch.setattr(r, '_download_result', download)
    monkeypatch.setattr(r, '_download_ssh_result', download)
    result = r.cleanup(value['id'])
    recovered = result['targets']['worker']
    if completed:
        assert result['status'] == recovered['status'] == 'failed'
        assert result['exit_code'] == recovered['exit_code'] == 17
        assert r.logs(value['id']) == {'worker': 'retained after controller exit'}
        assert mutations
        if provider == 'ssh':
            assert recovered['cleaned'] is True
        else:
            assert result['resources'][0]['status'] == 'deleted'
    else:
        assert recovered['status'] == saved_status
        assert not recovered.get('cleaned')
        assert not mutations
        if provider == 'github':
            assert result['resources'][0]['status'] == 'created'


@pytest.mark.parametrize('provider', ['github', 'ssh'])
def test_cleanup_before_remote_launch_does_not_probe_missing_resources(runners, monkeypatch, tmp_path, provider):
    r = runners
    value, directory, target = receipt(r, tmp_path, provider, status='queued')
    target.pop('repository_id')
    target.pop('workflow_id')
    target.pop('release_id')
    target['dispatched'] = target['resource_created'] = False
    if provider == 'github':
        value['resources'] = [{'type': 'github', 'repository': 'owner/private',
                               'repository_id': 12, 'tag': target['release_tag'], 'status': 'created'}]
    r._save(value, directory)
    def forbidden(*a, **kw):
        pytest.fail('cleanup probed an unlaunched resource')
    for method in ('_private', '_find_run', '_download_result', '_download_ssh_result', '_ssh', '_run'):
        monkeypatch.setattr(r, method, forbidden)
    result = r.cleanup(value['id'])
    assert result['targets']['worker']['status'] == 'queued'
    assert not result['targets']['worker'].get('cleaned')
    if provider == 'github':
        target['dispatched'] = True
        r._save(value, directory)
        result = r.cleanup(value['id'])
        assert 'metadata is incomplete' in result['targets']['worker']['cleanup_error']
        target['dispatched'] = False
        r._save(value, directory)
        mutations = []
        monkeypatch.setattr(r, '_private', lambda *a: {})
        monkeypatch.setattr(r, '_run', lambda *a, **kw: mutations.append(a) or '')
        assert r.cancel(value['id'])['targets']['worker']['status'] == 'cancelled'
        assert r.cleanup(value['id'])['resources'][0]['status'] == 'deleted'
        assert mutations


def test_cancel_github_wait_and_queue_deadline(runners, monkeypatch, tmp_path):
    r = runners
    value, directory, target = receipt(r,tmp_path,'github')
    target.update(branch='main',release_id=21,label=['self-hosted','linux'])
    calls = []
    monkeypatch.setattr(r, '_api', lambda *a, **kw: calls.append((a,kw)) or {'status':'completed','conclusion':'cancelled'})
    monkeypatch.setattr(r.time, 'sleep', lambda *a: None)
    assert not r._cancel_github(target, None)
    assert r._cancel_github(target, {'id':1,'status':'queued'})
    ticks = iter([0,2,3])
    monkeypatch.setattr(r.time, 'monotonic', lambda: next(ticks))
    assert not r._cancel_github(target, {'id':1,'status':'queued'}, wait_seconds=1)
    monkeypatch.setattr(r, '_find_run', lambda *a: None)
    monkeypatch.setattr(r, '_cancel_github', lambda *a: False)
    ticks = iter([0,10])
    monkeypatch.setattr(r.time, 'monotonic', lambda: next(ticks))
    r._github_target(value,'worker',target,directory,5,1)
    assert target['status'] == 'unknown'


def test_setup_http_failures_and_disabled_workflow(runners, monkeypatch):
    r = runners
    templates = r._templates()
    def api(path, method='GET', data=None):
        if '/contents/' in path:
            raise r.RunnerError('403 denied')
        return {'id':12,'private':True}
    monkeypatch.setattr(r, '_api', api)
    with pytest.raises(ValueError, match='403'):
        r.setup_github('owner/private')
    def api(path, method='GET', data=None):
        if method == 'PUT':
            return {}
        if '/contents/' in path:
            raise r.RunnerError('404 missing')
        if path.endswith('/contents'):
            raise r.RunnerError('404 empty')
        return {'id':12,'private':True}
    monkeypatch.setattr(r, '_api', api)
    r.setup_github('owner/private')
    def api(path, method='GET', data=None):
        if '/contents/' in path:
            return {'content':base64.b64encode(templates[path.split('/contents/')[1]].encode()).decode()}
        if path.endswith('/contents'):
            raise r.RunnerError('403 root denied')
        if '/actions/' in path:
            return {'path':r.WORKFLOW,'state':'disabled_manually'}
        return {'id':12,'private':True}
    monkeypatch.setattr(r, '_api', api)
    with pytest.raises(ValueError, match='403 root'):
        r.setup_github('owner/private')
    with pytest.raises(ValueError, match='not active'):
        r._broker('owner/private', None)


def test_artifact_manifest_required_and_duplicates(runners, tmp_path, monkeypatch):
    r = runners
    archive = tmp_path/'bundle.tar'
    with tarfile.open(archive,'w') as bundle:
        item = tarfile.TarInfo('file')
        bundle.addfile(item)
        bundle.addfile(item)
    with pytest.raises(ValueError,match='unavailable'):
        r._extract_artifacts(archive,tmp_path/'out',None)
    with pytest.raises(ValueError,match='invalid artifact'):
        r._extract_artifacts(archive,tmp_path/'out',[{}])
    records=[{'path':'file','size':0,'mode':0o644,'sha256':hashlib.sha256(b'').hexdigest()}]
    with pytest.raises(ValueError,match='unexpected'):
        r._extract_artifacts(archive,tmp_path/'out',records)
    with tarfile.open(archive,'w') as bundle:
        bundle.addfile(item)
    r._extract_artifacts(archive,tmp_path/'out',records)
    with pytest.raises(ValueError,match='must be new'):
        r._extract_artifacts(archive,tmp_path/'out',records)


def test_ssh_transport_failure_and_terminal_cleanup_errors(runners, tmp_path, monkeypatch):
    r = runners
    value,directory,target = receipt(r,tmp_path)
    r._write(directory/'worker.json',{'command':['true'],'artifacts':[]})
    def ssh(host,command,**kwargs):
        if command.startswith('uname'):
            return 'Linux\nx86_64\n'
        if command.startswith('nohup'):
            raise r.RunnerError('connection lost')
        return ''
    monkeypatch.setattr(r,'_ssh',ssh)
    def run(args,**kw):
        if ':/' in args[-2]:
            raise r.RunnerError('download failed')
        return ''
    monkeypatch.setattr(r,'_run',run)
    with pytest.raises(ValueError,match='connection lost'):
        r._ssh_target(value,'worker',target,directory,1)
    target['status']='unknown'
    r._cleanup_receipt(value,directory)
    assert not target.get('cleaned')
    target['status']='failed'
    def fail(*a,**kw):
        raise r.RunnerError('cleanup failed')
    monkeypatch.setattr(r,'_ssh',fail)
    value['resources']=[{'type':'github','repository':'owner/private','repository_id':12,'tag':'tag','status':'created'}]
    monkeypatch.setattr(r,'_private',fail)
    r._cleanup_receipt(value,directory)
    assert target['cleanup_error']=='cleanup failed'
    assert value['resources'][0]['cleanup_error']=='cleanup failed'
    target['status']='unknown'
    r._save(value,directory)
    assert r.cleanup(value['id'])['targets']['worker']['status']=='unknown'
    assert r.cancel(value['id'])['targets']['worker']['error']=='cleanup failed'


def test_github_download_failure_and_running_cancellation(runners, tmp_path, monkeypatch):
    r = runners
    value,directory,target = receipt(r,tmp_path,'github')
    target.update(branch='main',release_id=21,label='ubuntu-latest')
    def fail(*a,**kw):
        raise r.RunnerError('download unavailable')
    monkeypatch.setattr(r,'_run',fail)
    monkeypatch.setattr(r,'_assets',lambda *a:[{'name':'worker-result.json','id':1,'size':10}])
    with pytest.raises(ValueError,match='without a command result'):
        r._download_result(target,'worker',directory)
    monkeypatch.setattr(r,'_api',lambda *a,**kw:{})
    monkeypatch.setattr(r,'_find_run',lambda *a:{'id':1,'status':'in_progress'})
    monkeypatch.setattr(r,'_cancel_github',lambda *a:True)
    target['provider_conclusion']='cancelled'
    monkeypatch.setattr(r.time,'sleep',lambda *a:None)
    (directory/'cancel.requested').write_text('{}')
    r._github_target(value,'worker',target,directory,1,1)
    assert target['status']=='cancelled' and target['exit_code']==130
    (directory/'cancel.requested').unlink()
    target['status']='queued'
    monkeypatch.setattr(r,'_find_run',lambda *a:{'id':1,'status':'queued'})
    ticks=iter([0,10])
    monkeypatch.setattr(r.time,'monotonic',lambda:next(ticks))
    r._github_target(value,'worker',target,directory,1,1)
    assert target['status']=='timed_out'
    monkeypatch.setattr(r,'_private',lambda *a:{})
    target['status']='unknown'
    r._save(value,directory)
    assert r.cancel(value['id'])['targets']['worker']['status']=='cancelled'
    # Terminal targets are ignored by cancellation; cleanup accepts all jobs.
    r.cancel(value['id'])
    assert r.cleanup()


def test_provider_failure_cleanup_and_start_cancellation(runners,tmp_path,monkeypatch):
    r=runners
    monkeypatch.setattr(r,'_helper',lambda:SimpleNamespace(create_snapshot=fake_snapshot))
    def broker(*a):
        raise r.RunnerError('private preflight failed')
    monkeypatch.setattr(r,'_broker',broker)
    result=r.start(tmp_path,config('github'),['true'])
    assert result['status']=='error'
    class CancelledPool:
        def __init__(self,**kw):
            pass
        def map(self,function,items):
            current=r.list_jobs()[0]
            (r._job_dir(current['id'])/'cancel.requested').write_text('{}')
            return [function(item) for item in items]
        def shutdown(self,**kw):
            pass
    monkeypatch.setattr(r,'ThreadPoolExecutor',CancelledPool)
    result=r.start(tmp_path,config(),['true'])
    assert result['targets']['worker']['status']=='cancelled'
    class InterruptPool(CancelledPool):
        def map(self,*a):
            raise KeyboardInterrupt
    monkeypatch.setattr(r,'ThreadPoolExecutor',InterruptPool)
    result=r.start(tmp_path,config(),['true'])
    assert result['interrupted']


def test_real_helper_through_fake_ssh_preserves_argv(runners,tmp_path,monkeypatch):
    r=runners
    repo=tmp_path/'repo'
    repo.mkdir()
    home=tmp_path/'home'
    home.mkdir()
    monkeypatch.setenv('HOME',str(home))
    for key in list(os.environ):
        if key.startswith('COV_CORE_'):
            monkeypatch.delenv(key)
    subprocess.run(['git','init','-q',str(repo)],check=True)
    (repo/'tracked.txt').write_text('dirty worktree bytes')
    subprocess.run(['git','-C',str(repo),'add','tracked.txt'],check=True)
    # An initial commit is unnecessary; source_head truthfully remains null.
    def ssh(host,command,timeout=60):
        result=subprocess.run(['zsh','-lic',command],capture_output=True,text=True,timeout=timeout)
        if result.returncode:
            raise r.RunnerError(result.stderr)
        return result.stdout
    monkeypatch.setattr(r,'_ssh',ssh)
    def copy(args,**kw):
        assert args[0]=='scp'
        if ':/' in args[-1]:
            destination=Path(args[-1].split(':',1)[1])
            for item in args[args.index('--')+1:-1]:
                shutil.copyfile(item,destination/Path(item).name)
        else:
            shutil.copyfile(args[-2].split(':',1)[1],args[-1])
        return ''
    monkeypatch.setattr(r,'_run',copy)
    data=config()
    target=data['runners']['targets']['worker']
    target.update(os=platform.system().lower(),arch=platform.machine().lower())
    data['runners']['artifacts']=['report.txt']
    literal="space ' quotes; $(never-execute) `literal`"
    code="import pathlib,sys;pathlib.Path('report.txt').write_text(sys.argv[1]);print(pathlib.Path('tracked.txt').read_text());raise SystemExit(7)"
    value=r.start(repo,data,['python3','-c',code,literal])
    assert value['exit_code']==7
    assert 'dirty worktree bytes' in r.logs(value['id'])['worker']
    output=r.artifacts(value['id'],destination=tmp_path/'out')
    assert (Path(output['worker'])/'report.txt').read_text()==literal
    assert value['targets']['worker']['cleaned']


def test_partial_setup_retry_and_permission_diagnostic(runners,monkeypatch):
    r=runners
    installed={}
    fail=[True]
    def api(path,method='GET',data=None):
        if '/contents/' in path:
            name=path.split('/contents/')[1]
            if method=='PUT':
                if fail[0] and name==r.WORKFLOW:
                    raise r.RunnerError('HTTP404')
                installed[name]=data['content']
                return {}
            if name not in installed:
                raise r.RunnerError('HTTP404')
            return {'content':installed[name]}
        if path.endswith('/contents'):
            return list(installed)
        return {'id':12,'private':True}
    monkeypatch.setattr(r,'_api',api)
    with pytest.raises(ValueError,match='workflow write permission'):
        r.setup_github('owner/private')
    assert list(installed)==['.t-runner-protocol.json']
    fail[0]=False
    assert r.setup_github('owner/private')['repository_id']==12
    assert len(installed)==3


def test_results_and_receipt_paths_bound_execution_identity(runners,tmp_path,monkeypatch):
    r=runners
    value,directory,target=receipt(r,tmp_path)
    path=directory/'worker/result.json'
    for result in [command_result(target,'succeeded',9),
                   {**command_result(target),'id':str(uuid.uuid4())},
                   {**command_result(target),'snapshot_id':str(uuid.uuid4())}]:
        r._write(path,result)
        with pytest.raises(ValueError,match='inconsistent|does not match'):
            r._result(target,path)
    with path.open('wb') as stream:
        stream.truncate(8*1024*1024+1)
    with pytest.raises(ValueError,match='metadata exceeds'):
        r._result(target,path)
    with pytest.raises(ValueError,match='invalid SSH'):
        r._ssh('-oProxyCommand=bad','true')
    target['remote_path']='/tmp/other'
    target['status']='failed'
    calls=[]
    monkeypatch.setattr(r,'_ssh',lambda *a,**kw:calls.append(a))
    r._cleanup_receipt(value,directory)
    assert calls==[] and 'exact job identity' in target['cleanup_error']


def test_ssh_cancellation_before_launch_and_bounded_probe(runners,tmp_path,monkeypatch):
    r=runners
    value,directory,target=receipt(r,tmp_path)
    r._write(directory/'worker.json',{'command':['true'],'artifacts':[]})
    calls=[]
    def ssh(host,command,**kw):
        calls.append(command)
        if command.startswith('uname'):
            return 'Linux\nx86_64\n'
        return ''
    monkeypatch.setattr(r,'_ssh',ssh)
    monkeypatch.setattr(r,'_run',lambda *a,**kw:'')
    (directory/'cancel.requested').write_text('{}')
    r._ssh_target(value,'worker',target,directory,1)
    assert target['status']=='cancelled' and not any(c.startswith('nohup') for c in calls)
    (directory/'cancel.requested').unlink()
    def ssh(host,command,**kw):
        calls.append(command)
        if command.startswith('uname'):
            return 'Linux\nx86_64\n'
        if command.startswith('nohup'):
            (directory/'cancel.requested').write_text('{}')
        if command.startswith('test -f'):
            raise r.RunnerError('still running')
        return ''
    monkeypatch.setattr(r,'_ssh',ssh)
    monkeypatch.setattr(r.time,'sleep',lambda *a:None)
    ticks=iter([0,0,1,100])
    monkeypatch.setattr(r.time,'monotonic',lambda:next(ticks))
    with pytest.raises(ValueError,match='terminal state is unverified'):
        r._ssh_target(value,'worker',target,directory,1)
    assert any(' cancel ' in command for command in calls)


def test_asset_inventory_and_download_size_guards(runners,tmp_path,monkeypatch):
    r=runners
    value,directory,target=receipt(r,tmp_path,'github')
    monkeypatch.setattr(r,'_api',lambda *a,**kw:{})
    with pytest.raises(ValueError,match='invalid release asset'):
        r._assets('owner/private',1)
    monkeypatch.setattr(r,'_api',lambda *a,**kw:[{}]*100)
    with pytest.raises(ValueError,match='inventory exceeds'):
        r._assets('owner/private',1)
    monkeypatch.setattr(r,'_assets',lambda *a:[{'name':'worker-result.json','id':1,'size':8*1024*1024+1}])
    with pytest.raises(ValueError,match='exceeds limit'):
        r._download_result(target,'worker',directory)
    monkeypatch.setattr(r,'_assets',lambda *a:[{'name':'worker-result.json','id':1,'size':1}])
    def download(args,output_path=None,**kw):
        Path(output_path).write_text('{}')
    monkeypatch.setattr(r,'_run',download)
    with pytest.raises(ValueError,match='without a command result'):
        r._download_result(target,'worker',directory)
    result=command_result(target)
    encoded=json.dumps(result)
    monkeypatch.setattr(r,'_assets',lambda *a:[{'name':'worker-result.json','id':1,'size':len(encoded)}])
    monkeypatch.setattr(r,'_run',lambda *a,output_path=None,**kw:Path(output_path).write_text(encoded))
    r._download_result(target,'worker',directory)
    assert target['exit_code']==17


def test_early_progress_interrupt_and_partial_source_upload(runners,tmp_path,monkeypatch):
    r=runners
    seen=[]
    def snapshot(*a,**kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(r,'_helper',lambda:SimpleNamespace(create_snapshot=snapshot))
    def progress(receipt):
        assert r.status(receipt['id'])['status']=='preparing'
        seen.append(receipt['id'])
    result=r.start(tmp_path,config(),['true'],progress=progress)
    assert result['interrupted'] and result['exit_code']==130 and seen==[result['id']]
    monkeypatch.setattr(r,'_helper',lambda:SimpleNamespace(create_snapshot=fake_snapshot))
    monkeypatch.setattr(r,'_broker',lambda *a:{'repository':'owner/private','repository_id':12,'workflow_id':7,'branch':'main'})
    monkeypatch.setattr(r,'_api',lambda *a,**kw:{'id':21})
    monkeypatch.setattr(r,'_assets',lambda *a:[])
    monkeypatch.setattr(r,'_private',lambda *a:{})
    monkeypatch.setattr(r,'_run',lambda *a,**kw:'')
    result=r.start(tmp_path,config('github'),['true'])
    assert result['status']=='error' and 'incomplete' in result['error']
    assert result['resources'][0]['status']=='deleted'
    value,directory,target=receipt(r,tmp_path,'github')
    target['dispatched']=False
    r._save(value,directory)
    assert r.cancel(value['id'])['targets']['worker']['status']=='cancelled'
