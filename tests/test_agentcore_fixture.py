"""Local-only tests of the disposable runtime and deployment/cleanup receipts."""
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'scripts/agentcore-test-runtime'
ACCOUNT = '111122223333'
REGION = 'us-east-1'
NAME = 'trial123'


def module(name):
    spec = importlib.util.spec_from_file_location('fixture_' + name, FIXTURE / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / 'bundle'
    module('prepare').bundle(ACCOUNT, REGION, NAME, root)
    return root


def test_reproducible_zip_and_bounded_permissions(prepared, tmp_path):
    second = tmp_path / 'second'
    module('prepare').bundle(ACCOUNT, REGION, NAME, second)
    assert (prepared / 'agent.zip').read_bytes() == (second / 'agent.zip').read_bytes()
    with zipfile.ZipFile(prepared / 'agent.zip') as archive:
        assert archive.namelist() == ['agent.py']
        assert archive.getinfo('agent.py').external_attr >> 16 == 0o100644
        assert archive.read('agent.py') == (FIXTURE / 'agent.py').read_bytes()
    template = json.loads((prepared / 'bootstrap.json').read_text())
    assert set(template['Resources']) == {'CodeBucket', 'ExecutionRole'}
    policy = template['Resources']['ExecutionRole']['Properties']['Policies'][0]['PolicyDocument']
    assert not any(s['Action'] == '*' for s in policy['Statement'])
    assert not any('bedrock:InvokeModel' in s['Action'] for s in policy['Statement'])
    request = json.loads((prepared / 'runtime-template.json').read_text())
    assert request['lifecycleConfiguration'] == {'idleRuntimeSessionTimeout':60, 'maxLifetime':600}
    assert request['platformVersion'] == 'V1'
    assert request['agentRuntimeArtifact']['codeConfiguration']['runtime'] == 'PYTHON_3_13'
    with pytest.raises(FileExistsError): module('prepare').bundle(ACCOUNT, REGION, NAME, prepared)


@pytest.mark.parametrize('account,region,name', [('bad',REGION,NAME),(ACCOUNT,'--endpoint-url',NAME),(ACCOUNT,REGION,'x;exit')])
def test_invalid_target_is_rejected_before_files(tmp_path, account, region, name):
    with pytest.raises(ValueError): module('prepare').bundle(account, region, name, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


def test_http_health_continuity_errors_and_new_process_state():
    agent = module('agent')
    boots = []
    for _ in range(2):
        server = agent.server(('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(method, path, body=None, headers=None):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                connection.request(method, path, body, headers or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read()), dict(response.getheaders())
            finally: connection.close()
        try:
            assert request('GET','/ping')[:2] == (200, {'status':'Healthy'})
            first = request('POST','/invocations', b'{"prompt":"hello"}')[1]
            second = request('POST','/invocations', b'{"prompt":"again"}')[1]
            assert first['reply'] == 'ack:hello' and first['count'] == 1
            assert second['count'] == 2 and second['boot_id'] == first['boot_id']
            boots.append(first['boot_id'])
            for body in (b'{}', b'[]', b'no-json', b'{"prompt":1}', b'{"prompt":"fixture-error"}'):
                assert request('POST','/invocations', body)[0] == 400
            assert request('POST','/invocations', b'x'*4097)[0] == 413
            assert request('POST','/wrong', b'{}')[0] == 404
            assert request('GET','/wrong')[0] == 404
            third = request('POST','/invocations', b'{"prompt":"safe"}', {'X-Injected':'yes'})
            assert third[1]['count'] == 3 and 'X-Injected' not in third[2]
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert boots[0] != boots[1]


@pytest.fixture
def fake_aws(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'; bindir.mkdir()
    log = tmp_path / 'aws-calls.jsonl'
    executable = bindir / 'aws'
    executable.write_text('#!' + sys.executable + '''
import json,os,sys
from pathlib import Path
a=sys.argv[1:]
service=next(x for x in a if x in ('sts','cloudformation','s3api','bedrock-agentcore-control','logs'))
op=a[a.index(service)+1]
with open(os.environ['FIXTURE_AWS_LOG'],'a') as f: f.write(json.dumps([service,op])+"\\n")
account='111122223333'; region='us-east-1'; stack='t-agentcore-test-trial123'; rid='t_accept_trial123-ABCDEFGHIJ'
if op=='get-caller-identity':
 print(os.environ.get('FIXTURE_ACCOUNT',account)); sys.exit()
if op=='describe-stacks':
 result={'Stacks':[{'StackId':f'arn:aws:cloudformation:{region}:{account}:stack/{stack}/test', 'StackStatus':'CREATE_COMPLETE', 'Outputs':[
 {'OutputKey':'Bucket','OutputValue':stack+'-codebucket-abc'}, {'OutputKey':'RoleArn','OutputValue':f'arn:aws:iam::{account}:role/{stack}-ExecutionRole-abc'}]}]}
elif op=='create-agent-runtime':
 result={'agentRuntimeId':rid,'agentRuntimeArn':f'arn:aws:bedrock-agentcore:{region}:{account}:runtime/{rid}',
 'workloadIdentityDetails':{'workloadIdentityArn':f'arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/{rid}'}}
elif op=='get-agent-runtime':
 if Path(os.environ['FIXTURE_AWS_LOG']+'.deleted').exists():
  print('ResourceNotFoundException',file=sys.stderr);sys.exit(254)
 result={'status':'READY'}
elif op=='delete-agent-runtime':
 Path(os.environ['FIXTURE_AWS_LOG']+'.deleted').touch();result={}
elif op=='describe-log-groups':
 result={'logGroups':[{'logGroupName':'/aws/bedrock-agentcore/runtimes/'+rid+'-DEFAULT'}]}
else: result={}
print(json.dumps(result))
''')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('FIXTURE_AWS_LOG', str(log))
    for key in tuple(os.environ):
        if key.startswith('T_AGENTCORE_APPROVE_'): monkeypatch.delenv(key)
    return log


def operate(action, root):
    env = {k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_')}
    return subprocess.run(['bash', str(FIXTURE / (action + '.sh')), str(root), 'test-profile'],
                          capture_output=True, text=True, env=env, timeout=20)


def test_deploy_cleanup_guards_and_complete_lifecycle(prepared, fake_aws, monkeypatch):
    assert operate('deploy', prepared).returncode == 2
    assert operate('cleanup', prepared).returncode == 2
    assert not fake_aws.exists()  # No STS or other AWS call before approval gate.
    monkeypatch.setenv('T_AGENTCORE_APPROVE_DEPLOY', NAME)
    assert (result := operate('deploy', prepared)).returncode == 0, result.stderr
    calls = [json.loads(x) for x in fake_aws.read_text().splitlines()]
    assert ['bedrock-agentcore-control','create-agent-runtime'] in calls
    assert not any(x[1]=='invoke-agent-runtime' for x in calls)
    assert operate('deploy', prepared).returncode != 0  # Refuses accidental duplicate deployment.
    (prepared / 'agent.zip').unlink()  # Cleanup does not depend on keeping the code archive.
    monkeypatch.setenv('T_AGENTCORE_APPROVE_CLEANUP', NAME)
    assert (result := operate('cleanup', prepared)).returncode == 0, result.stderr
    calls = [json.loads(x) for x in fake_aws.read_text().splitlines()]
    assert ['bedrock-agentcore-control','delete-workload-identity'] in calls
    assert ['logs','delete-log-group'] in calls
    assert ['cloudformation','delete-stack'] in calls
    assert (prepared / 'cleanup.complete').exists()


def test_wrong_account_and_corrupt_bundle_never_mutate(prepared, fake_aws, monkeypatch):
    monkeypatch.setenv('T_AGENTCORE_APPROVE_DEPLOY', NAME)
    monkeypatch.setenv('FIXTURE_ACCOUNT','999900001111')
    assert operate('deploy', prepared).returncode == 2
    assert json.loads(fake_aws.read_text().strip()) == ['sts','get-caller-identity']
    fake_aws.unlink()
    (prepared / 'agent.zip').write_bytes(b'corrupt')
    assert operate('deploy', prepared).returncode != 0
    assert not fake_aws.exists()


def test_ambiguous_runtime_creation_blocks_cleanup(prepared, fake_aws, monkeypatch):
    monkeypatch.setenv('T_AGENTCORE_APPROVE_CLEANUP', NAME)
    (prepared / 'runtime-create.started').touch()
    assert operate('cleanup', prepared).returncode != 0
    assert json.loads(fake_aws.read_text().strip()) == ['sts','get-caller-identity']


def test_runtime_receipts_cannot_target_other_resources(prepared):
    rid='other-ABCDEFGHIJ'
    (prepared / 'runtime-create.json').write_text(json.dumps({'agentRuntimeId':rid,'agentRuntimeArn':'wrong'}))
    with pytest.raises(ValueError): module('render').render('runtime-id',prepared)
