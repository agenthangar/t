import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace

import pytest
from conftest import load_script
from test_eval import evaluation, ROOT


def test_container_configuration_uses_production_workflow(evaluation, tmp_path, monkeypatch):
    m, _ = evaluation
    t = load_script(ROOT / 'libexec/t_eval_trial.py', 'eval_trial_test')
    monkeypatch.chdir(tmp_path)
    # configure mutates these environment names; monkeypatch owns their restoration.
    monkeypatch.setenv('TERM', 'dumb')
    monkeypatch.setenv('T_LOCAL_RUNTIME_RECEIPT', '')
    logs = tmp_path / 'logs'
    capability = {'token': 'fixture-lease', 'frontier_base': 'http://127.0.0.1:1234/fixture-lease/frontier/v1',
                  'local': {'model': m.defaults()['local_model'], 'provider': 'lmstudio', 'revision': 'revision',
                            'alias': 'alias', 'base': 'http://127.0.0.1:1234/fixture-lease/local', 'context_tokens': 32768}}
    for mode in m.MODES:
        job = {'config': m.defaults(), 'mode': mode, 'capability': capability}
        local, chosen = t.configure(job, tmp_path, logs)
        assert chosen['mode'] == ('local-only' if mode == 'local-only' else 'automatic')
        workflow = t.helper('t_workflow')
        document, _ = workflow['load']()
        assert document['workflows']['standard']['inference']['model'] == m.defaults()['local_model']
        command = t.native_command(job, tmp_path, logs, chosen)
        assert ('mcp_servers.t-local-worker.args=["mcp","--local-worker"]' in command) == (mode in ('automatic', 'required-local'))
        assert 'OPENAI_API_KEY' not in (logs / 'runtime/.codex/config.toml').read_text()
    job['config']['reasoning_effort'] = 'high'
    t.configure(job, tmp_path, logs)
    assert 'model_reasoning_effort = "high"' in (logs / 'runtime/.codex/config.toml').read_text()
    (logs / 'events.jsonl').write_text('not-json\n{"type":"valid"}\n')
    assert t.collect_events(logs) == [{'type': 'valid'}]


def test_native_execution_checkpoints_without_model(evaluation, tmp_path, monkeypatch):
    t = load_script(ROOT / 'libexec/t_eval_trial.py', 'eval_trial_exec_test')
    checkpoints = []
    monkeypatch.setattr(t, 'checkpoint', lambda job, kind: checkpoints.append(kind))
    job = {'config': {'task_seconds': 5}}
    program = 'import json,sys;sys.stdin.read();print(json.dumps({"type":"item.completed","item":{"type":"command_execution","exit_code":0}}))'
    with (tmp_path / 'events').open('w') as stream:
        code, timed_out = t.native_exec([sys.executable, '-c', program], 'fixture', stream, job)
    assert code == 0 and not timed_out and checkpoints == ['tool_completed']
    assert json.loads((tmp_path / 'events').read_text())['observed_at'] > 0


@pytest.fixture
def harbor_adapter(monkeypatch):
    # Optional Harbor dependency is installed only for explicit evaluation runs.
    # Test its boundary with typed-shaped values, never a Docker daemon.
    class Base:
        def __init__(self, *args, **kwargs): self.kwargs = kwargs
    class Config:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    modules = {
        'harbor.agents.base': {'BaseAgent': Base}, 'harbor.agents.installed.codex': {'Codex': Base},
        'harbor.models.trial.config': {key: Config for key in ('AgentConfig', 'EnvironmentConfig', 'TaskConfig', 'TrialConfig', 'VerifierConfig')},
        'harbor.trial.trial': {'Trial': Base}}
    for name, members in modules.items():
        module = ModuleType(name); module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
    return load_script(ROOT / 'libexec/t_eval_harbor.py', 'eval_harbor_test')


def test_harbor_patch_boundary_and_network(harbor_adapter, tmp_path):
    h = harbor_adapter
    calls = []
    class Environment:
        async def exec(self, **kwargs):
            calls.append(kwargs); return SimpleNamespace(return_code=0, stdout='/app', stderr='')
        async def upload_file(self, **kwargs): calls.append(kwargs)
        async def service_exec(self, **kwargs):
            calls.append(kwargs); return SimpleNamespace(return_code=0)
    env = Environment()
    h.bundle(tmp_path / 'bundle')
    assert (tmp_path / 'bundle/bin/t').is_file()
    assert not (tmp_path / 'bundle/.git').exists()
    asyncio.run(h.restrict_network(env, 1234))
    assert 'tcp dport 1234' in calls[0]['command'] and 'reject' in calls[0]['command']
    asyncio.run(h.verify_worker_sandbox(env, '/app', 1234))
    assert '--permission-profile :workspace' in calls[-1]['command']
    assert 'worker can write outside its workspace' in calls[-1]['command']
    assert 'socket.create_connection' in calls[-1]['command']
    patch = tmp_path / 'candidate.patch'; patch.write_text('fixture diff')
    agent = h.ReplayAgent(patch_file=str(patch))
    assert agent.name() == 't-patch-replay' and agent.version() == '1'
    asyncio.run(agent.setup(env))
    asyncio.run(agent.run('', env, None))
    assert any('apply --binary' in c.get('command', '') for c in calls)
    asyncio.run(h.ReplayAgent().run('', env, None))
    for valid, rewards in ((False, {}), (True, {'reward': .5})):
        with pytest.raises(RuntimeError): h.reward(SimpleNamespace(reward_valid=valid, verifier_result=SimpleNamespace(rewards=rewards)))
    assert h.reward(SimpleNamespace(reward_valid=True, verifier_result=SimpleNamespace(rewards={'reward': 1})))
    class Broken(Environment):
        async def exec(self, **kwargs): return SimpleNamespace(return_code=1, stderr='failed')
        async def service_exec(self, **kwargs): return SimpleNamespace(return_code=1, stderr='blocked', stdout='')
    with pytest.raises(RuntimeError): asyncio.run(h.checked(Broken(), 'fixture'))
    with pytest.raises(RuntimeError): asyncio.run(h.restrict_network(Broken(), 1))
    with pytest.raises(RuntimeError): asyncio.run(h.verify_worker_sandbox(Broken(), '/app', 1))


def test_harbor_controls_and_fresh_grading(harbor_adapter, evaluation, tmp_path, monkeypatch):
    h = harbor_adapter; m, _ = evaluation
    monkeypatch.setitem(sys.modules, 't_eval', m)
    task = tmp_path / 'tasks/fixture'; task.mkdir(parents=True)
    (task / 'task.toml').write_text('[environment]\ndocker_image="pinned"\n')
    job = {'task': str(task), 'directory': str(tmp_path / 'attempt'), 'trial_id': 'b'*32,
           'config': m.defaults(), 'codex_version': '0.155.1'}
    job_file = tmp_path / 'job.json'; job_file.write_text(json.dumps(job))
    phases = []
    async def trial(value, name, agent, grade=True):
        phases.append((name, grade, agent))
        if name == 'candidate':
            logs = Path(value['directory']) / 'harbor' / (value['trial_id'] + '-candidate') / 'agent'
            logs.mkdir(parents=True)
            (logs / 'model.patch').write_text('patch')
            (logs / 'workflow.json').write_text('{"workflow_valid":true}')
        return SimpleNamespace(reward_valid=True, verifier_result=SimpleNamespace(rewards={'reward': 0 if name == 'nop' else 1}))
    monkeypatch.setattr(h, 'trial', trial)
    result = asyncio.run(h.execute(job_file))
    assert result['resolved'] and result['workflow_valid']
    assert [p[0] for p in phases] == ['oracle', 'nop', 'candidate', 'grade']
    assert phases[2][1] is False and phases[3][1] is True
    assert phases[2][2].extra_allowed_hosts == ['192.168.5.2']
    assert phases[3][2].kwargs['patch_file'].endswith('/model.patch')
    phases.clear()
    # Controls are cached, but candidate and fresh verifier still run.
    async def repeat(value, name, agent, grade=True):
        phases.append(name)
        return SimpleNamespace(reward_valid=True, verifier_result=SimpleNamespace(rewards={'reward': 0}))
    monkeypatch.setattr(h, 'trial', repeat)
    assert asyncio.run(h.execute(job_file))['resolved'] is False
    assert phases == ['candidate', 'grade']
    (task / 'task.toml').write_text('changed digest')
    phases.clear()
    with pytest.raises(RuntimeError, match='reference solution did not pass'): asyncio.run(h.execute(job_file))
    assert phases == ['oracle']
    async def invalid_control(value, name, agent, grade=True):
        phases.append(name)
        return SimpleNamespace(reward_valid=True, verifier_result=SimpleNamespace(rewards={'reward': 1}))
    monkeypatch.setattr(h, 'trial', invalid_control)
    phases.clear()
    with pytest.raises(RuntimeError, match='unchanged repository passed'): asyncio.run(h.execute(job_file))
    assert phases == ['oracle', 'nop']


def test_harbor_trial_config_and_candidate_bundle(harbor_adapter, evaluation, tmp_path, monkeypatch):
    h = harbor_adapter; m, _ = evaluation
    job = {'task': str(tmp_path), 'directory': str(tmp_path / 'attempt'), 'trial_id': 'c'*32,
           'config': m.defaults(), 'native_codex':str(tmp_path/'native'), 'capability': {'port': 1234}, 'mode': 'automatic',
           'worker_sandbox': ['seccomp=/fixture/policy.json', 'apparmor=t-eval-userns']}
    configs = []
    class Trial:
        @staticmethod
        async def create(config):
            configs.append(config); return Trial()
        async def run(self): return 'result'
    monkeypatch.setattr(h, 'Trial', Trial)
    assert asyncio.run(h.trial(job, 'candidate', h.AgentConfig(), False)) == 'result'
    assert configs[0].trial_name == 'c'*32 + '-candidate' and configs[0].verifier.disable
    assert json.loads(configs[0].environment.extra_docker_compose[0].read_text()) == {'services': {'main': {
        'platform': 'linux/amd64', 'cap_drop':['NET_RAW'], 'security_opt': job['worker_sandbox']}}}
    asyncio.run(h.trial(job, 'oracle', h.AgentConfig()))
    assert json.loads(configs[-1].environment.extra_docker_compose[0].read_text()) == {'services': {'main': {'platform': 'linux/amd64'}}}
    payloads = []
    class Env:
        async def exec(self, **kwargs):
            return SimpleNamespace(return_code=0, stdout='/app', stderr='')
        async def upload_dir(self, **kw):
            assert kw['target_dir']=='/opt/t-eval-codex' or (kw['source_dir'] / 'libexec/mlx-models.json').is_file()
        async def upload_file(self, **kw): payloads.append(json.loads(kw['source_path'].read_text()))
    async def restrict(*a): pass
    monkeypatch.setattr(h, 'restrict_network', restrict)
    job_file = tmp_path / 'job.json'; job_file.write_text(json.dumps(job))
    agent = h.WorkflowAgent(job_file=str(job_file))
    agent.environment_logs_dir = Path('/logs/agent')
    assert agent.name() == 't-workflow'
    asyncio.run(agent.install(Env()))
    asyncio.run(agent.run('public instruction', Env(), None))
    assert payloads[0]['instruction'] == 'public instruction'
    assert 'task' not in payloads[0] and 'benchmark_root' not in payloads[0]


@pytest.mark.parametrize('mode', ['frontier-only', 'automatic', 'required-local', 'local-only', 'outage'])
def test_native_modes_and_evidence(evaluation, tmp_path, monkeypatch, mode):
    m, _ = evaluation
    t = load_script(ROOT / 'libexec/t_eval_trial.py', 'eval_evidence_test')
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / 'logs'; logs.mkdir()
    job = {'config': m.defaults(), 'mode': mode, 'workspace': str(tmp_path), 'logs': str(logs),
           'capability': {'token': 'sensitive', 'frontier_base': 'http://fixture/sensitive/frontier/v1'}, 'instruction': 'task'}
    job_file = tmp_path / 'job.json'; job_file.write_text(json.dumps(job))
    monkeypatch.setattr(t, 'configure', lambda *a: ({}, {}))
    monkeypatch.setattr(t, 'native_command', lambda *a: ['codex', 'exec'])
    def execute(command, instruction, stream, payload):
        if mode == 'local-only': assert '--lead' in command
        worker = logs / 'runtime/state/t/local-worker/task/result.json'
        worker.parent.mkdir(parents=True)
        worker.write_text(json.dumps({'status': 'completed', 'identity_verified': True, 'patch': 'patch'}))
        for command in ('git diff', 'pytest'):
            stream.write(json.dumps({'type': 'item.completed', 'observed_at': worker.stat().st_mtime + 1,
                                     'item': {'type': 'command_execution', 'command': command, 'exit_code': 0}}) + '\n')
        (logs / 'answer.txt').write_text('sensitive')
        return 0, False
    monkeypatch.setattr(t, 'native_exec', execute)
    def outage(*args):
        (logs / 'outage.json').write_text('{}'); return 0, True
    monkeypatch.setattr(t, 'outage', outage)
    assert t.execute(job_file) == 0
    result = json.loads((logs / 'workflow.json').read_text())
    assert result['workflow_valid'] is True and result['effective_effort'] is None
    if mode != 'outage': assert (logs / 'answer.txt').read_text() == '[redacted-capability]'


def test_outage_observer_checkpoints_and_injects_once(evaluation, tmp_path, monkeypatch):
    m, _ = evaluation
    t = load_script(ROOT / 'libexec/t_eval_trial.py', 'eval_outage_observer')
    requests = []
    def urlopen(request, **kwargs):
        requests.append(request.full_url); return io.BytesIO(b'{}')
    monkeypatch.setattr(t.urllib.request, 'urlopen', urlopen)
    def supervise(command, chosen, workspace, observer):
        event = {'method': 'item/completed', 'params': {'item': {'type': 'commandExecution', 'exitCode': 0, 'id': 'first'}}}
        observer(event); observer(event)
        return 0
    monkeypatch.setattr(t, 'helper', lambda name: {'supervise': supervise})
    job = {'instruction': 'fixture', 'capability': {'frontier_base': 'http://fixture/token/frontier/v1'}}
    assert t.outage_child(job, tmp_path, tmp_path, {}) == 0
    assert sum(url.endswith('/outage') for url in requests) == 1
    assert sum(url.endswith('/checkpoint') for url in requests) == 2
    assert json.loads((tmp_path / 'outage.json').read_text())['after_command'] == 'first'


@pytest.mark.parametrize('complete', [True, False])
def test_outage_pty_completion_and_timeout(evaluation, tmp_path, monkeypatch, complete):
    t = load_script(ROOT / 'libexec/t_eval_trial.py', 'eval_outage_pty')
    job_file = tmp_path / 'job.json'; job_file.write_text('{}')
    logs = tmp_path / 'logs'; logs.mkdir()
    session = logs / 'runtime/state/t/local-worker/lead-fixture/.codex/sessions/1.jsonl'
    program = ('import time,pathlib;'
               f'p=pathlib.Path({str(session)!r});p.parent.mkdir(parents=True);'
               + ('p.write_text(\'{"task_complete":true}\');print("\\x1b[6n",flush=True);' if complete else '')
               + 'time.sleep(10)')
    real_popen = t.subprocess.Popen
    def popen(command, **kwargs):
        kwargs['env'] = {k: v for k, v in t.os.environ.items() if not k.startswith('COV_CORE_')}
        return real_popen([sys.executable, '-c', program], **kwargs)
    monkeypatch.setattr(t.subprocess, 'Popen', popen)
    code, completed = t.outage(job_file, {'config': {'task_seconds': 3 if complete else .05}}, tmp_path, logs)
    assert completed is complete and code is not None


def test_native_timeout_reaps_process(evaluation, tmp_path, monkeypatch):
    t = load_script(ROOT / 'libexec/t_eval_trial.py', 'eval_native_timeout')
    monkeypatch.setattr(t, 'checkpoint', lambda *a: None)
    with (tmp_path / 'log').open('w') as stream:
        code, timed_out = t.native_exec([sys.executable, '-c', 'import time;print("not-json",flush=True);time.sleep(10)'],
                                      '', stream, {'config': {'task_seconds': .05}})
    assert code == 124 and timed_out
