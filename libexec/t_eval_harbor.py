"""Harbor adapter. Only public task instructions and model leases reach the agent."""
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile

from harbor.agents.base import BaseAgent
from harbor.agents.installed.codex import Codex
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.trial.trial import Trial

ROOT = Path(__file__).resolve().parents[1]
REPO = 'cd /app 2>/dev/null || cd /testbed; '


async def checked(environment, command, **kwargs):
    result = await environment.exec(command=command, **kwargs)
    if result.return_code:
        raise RuntimeError('Benchmark environment command failed: ' + (result.stderr or '')[-1000:])
    return (result.stdout or '').strip()


def bundle(destination):
    """An allowlist, never the caller's checkout or personal agent configuration."""
    for relative in ('bin/t', 'ui/fzf.sh', 'libexec/mlx-models.json', 'libexec/local-models.json', 'libexec/t_issue_policy.txt'):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (destination / 'libexec').mkdir(exist_ok=True)
    for source in (ROOT / 'libexec').glob('*.py'):
        shutil.copy2(source, destination / 'libexec' / source.name)


async def restrict_network(environment, port):
    # Enforce the port as well as the address. The agent container has no NET_ADMIN.
    rules = ('table inet t_eval_guard { chain output { type filter hook output priority -150; '
             'policy accept; fib daddr type local accept; '
             f'ip daddr 192.168.5.2 tcp dport {int(port)} accept; reject;\n }}\n }}\n')
    result = await environment.service_exec(
        command='printf %s ' + shlex.quote(rules) + ' | nft -f -',
        service='harbor-docker-egress-control-sidecar', user='root', timeout_sec=30)
    if result.return_code:
        raise RuntimeError('Cannot enforce evaluation model-only egress: ' + (result.stderr or result.stdout or '')[-1000:])
    probe = '''import socket,sys
def connects(host,port):
 try:
  with socket.create_connection((host,port),timeout=2): return True
 except OSError: return False
assert connects('192.168.5.2',int(sys.argv[1])), 'model gateway unreachable'
assert not connects('1.1.1.1',443), 'internet egress was not blocked'
assert not connects('192.168.5.2',22), 'other host ports were not blocked'
'''
    await checked(environment, 'python3 -c ' + shlex.quote(probe) + ' ' + str(port), timeout_sec=15)


async def verify_worker_sandbox(environment, workspace, port):
    probe = '''import errno,socket,tempfile
with tempfile.TemporaryFile(dir='.') as f: f.write(b'workspace write works')
try:
 with tempfile.TemporaryFile(dir='/etc') as f: pass
except OSError as e:
 assert e.errno in (errno.EACCES,errno.EPERM,errno.EROFS), e
else: raise AssertionError('worker can write outside its workspace')
try:
 with socket.create_connection(('192.168.5.2',PORT),timeout=2): pass
except OSError: pass
else: raise AssertionError('worker sandbox permits direct network access')
'''.replace('PORT', str(int(port)))
    await checked(environment, 'codex sandbox --permission-profile :workspace -C ' + shlex.quote(workspace) +
                  ' -- python3 -c ' + shlex.quote(probe), cwd=workspace, timeout_sec=30)


class WorkflowAgent(Codex):
    def __init__(self, *args, job_file, **kwargs):
        self.job = json.loads(Path(job_file).read_text())
        super().__init__(*args, **kwargs)

    @staticmethod
    def name():
        return 't-workflow'

    async def install(self, environment):
        # Run the pinned official native Linux binary on the VM's CPU. The
        # benchmark userspace stays x86; Rosetta cannot run every Codex binary.
        await environment.upload_dir(source_dir=Path(self.job['native_codex']), target_dir='/opt/t-eval-codex')
        await checked(environment, 'ln -sf /opt/t-eval-codex/bin/codex /usr/local/bin/codex && codex --version', timeout_sec=30)
        await self.configure(environment)

    async def configure(self, environment):
        # Do not use Harbor's credential/config forwarding. The gateway owns credentials.
        await checked(environment, 'python3 -c "import sys; assert sys.version_info >= (3, 11)"', timeout_sec=30)
        with tempfile.TemporaryDirectory(prefix='t-eval-bundle-') as directory:
            bundle(Path(directory))
            await environment.upload_dir(source_dir=Path(directory), target_dir='/opt/t-eval')

    async def run(self, instruction, environment, context):
        job = self.job
        workspace = await checked(environment, REPO + 'git rev-parse --show-toplevel', timeout_sec=30)
        logs = self.environment_logs_dir.as_posix()
        await checked(environment, 'mkdir -p ' + shlex.quote(logs), timeout_sec=30)
        await restrict_network(environment, job['capability']['port'])
        if job['mode'] in ('automatic', 'required-local'):
            await verify_worker_sandbox(environment, workspace, job['capability']['port'])
        payload = {k: job[k] for k in ('config', 'mode', 'capability')}
        payload.update(workspace=workspace, logs=logs, instruction=instruction)
        with tempfile.TemporaryDirectory(prefix='t-eval-payload-') as directory:
            source = Path(directory) / 'job.json'
            source.write_text(json.dumps(payload))
            await environment.upload_file(source_path=source, target_path='/tmp/t-eval-job.json')
        try:
            execution = await environment.exec(
                command='timeout -k 15 ' + str(job['config']['task_seconds'] + 30) +
                        ' python3 /opt/t-eval/libexec/t_eval_trial.py /tmp/t-eval-job.json',
                cwd=workspace, timeout_sec=job['config']['task_seconds'] + 60)
            (Path(job['directory']) / 'entrypoint.json').write_text(json.dumps({
                'exit_code': execution.return_code, 'stdout': execution.stdout, 'stderr': execution.stderr}))
            if execution.return_code:
                raise RuntimeError('Workflow entrypoint failed: ' + (execution.stderr or execution.stdout or '')[-2000:])
        finally:
            # Fresh grading receives only the diff, never the mutated filesystem.
            await checked(environment, REPO +
                          'git -c core.hooksPath=/dev/null add -A && '
                          'git -c core.hooksPath=/dev/null diff --cached --binary HEAD > ' +
                          shlex.quote(logs + '/model.patch'), timeout_sec=120)
            await environment.exec(command='rm -f /tmp/t-eval-job.json', timeout_sec=10)


class ReplayAgent(BaseAgent):
    def __init__(self, *args, patch_file=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.patch = Path(patch_file) if patch_file else None

    @staticmethod
    def name():
        return 't-patch-replay'

    def version(self):
        return '1'

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        if self.patch and self.patch.stat().st_size:
            await environment.upload_file(source_path=self.patch, target_path='/tmp/candidate.patch')
            await checked(environment, REPO +
                          'git -c core.hooksPath=/dev/null apply --binary /tmp/candidate.patch', timeout_sec=120)


async def trial(job, name, agent, grade=True):
    # Only the benchmark service requires x86. Harbor's egress helper is built
    # for the Docker daemon's native architecture on Apple Silicon.
    platform = Path(job['directory']) / 'platform.json'
    platform.parent.mkdir(parents=True, exist_ok=True)
    service = {'platform': 'linux/amd64'}
    if name == 'candidate':
        service['cap_drop'] = ['NET_RAW']
        if job.get('worker_sandbox'):
            service['security_opt'] = job['worker_sandbox']
    platform.write_text(json.dumps({'services': {'main': service}}))
    config = TrialConfig(task=TaskConfig(path=Path(job['task'])), trial_name=job['trial_id'] + '-' + name,
                         trials_dir=Path(job['directory']) / 'harbor', agent=agent,
                         environment=EnvironmentConfig(type='docker', delete=True, extra_docker_compose=[platform]),
                         verifier=VerifierConfig(disable=not grade))
    return await (await Trial.create(config)).run()


def reward(result):
    if not result.reward_valid:
        raise RuntimeError('Official verifier did not produce a valid reward')
    values = result.verifier_result.rewards
    if values.get('reward') not in (0, 1):
        raise RuntimeError('Unexpected official verifier reward')
    return bool(values['reward'])


async def execute(job_file):
    import t_eval
    job = json.loads(Path(job_file).read_text())
    directory = Path(job['directory'])
    directory.mkdir(parents=True, exist_ok=True)
    # Validate every task/image/verifier before spending model tokens. Controls contain
    # hidden answers and remain entirely outside the candidate environment.
    key = hashlib.sha256((Path(job['task']) / 'task.toml').read_bytes()).hexdigest()
    controls = Path(job['task']).parent.parent / 'controls' / (Path(job['task']).name + '-' + key)
    with t_eval.lock(controls.with_suffix('.lock')):
        if not t_eval.read(controls / 'passed.json'):
            oracle = await trial(job, 'oracle', AgentConfig(name='oracle'))
            if not reward(oracle):
                raise RuntimeError('Benchmark control failed: reference solution did not pass; '
                                   'inspect harbor/' + job['trial_id'] + '-oracle/verifier before retrying. '
                                   'No model inference was started.')
            nop = await trial(job, 'nop', AgentConfig(import_path='t_eval_harbor:ReplayAgent'))
            if reward(nop):
                raise RuntimeError('Benchmark control failed: unchanged repository passed; '
                                   'no model inference was started.')
            t_eval.atomic(controls / 'passed.json', {'oracle': True, 'nop': False, 'task_hash': key})
    await trial(job, 'candidate', AgentConfig(
        import_path='t_eval_harbor:WorkflowAgent', model_name=job['config']['frontier_model'],
        override_timeout_sec=job['config']['task_seconds'] + 180,
        extra_allowed_hosts=['192.168.5.2'],
        kwargs={'job_file': str(job_file), 'version': job['codex_version']}), grade=False)
    agent_logs = directory / 'harbor' / (job['trial_id'] + '-candidate') / 'agent'
    patch = agent_logs / 'model.patch'
    if not patch.is_file():
        raise RuntimeError('Candidate patch was not captured; inspect candidate logs')
    workflow = t_eval.read(agent_logs / 'workflow.json', {})
    if not workflow:
        raise RuntimeError('Workflow evidence missing; inspect candidate logs and entrypoint.json')
    graded = await trial(job, 'grade', AgentConfig(import_path='t_eval_harbor:ReplayAgent',
                                                 kwargs={'patch_file': str(patch)}))
    return {'status': 'completed', 'resolved': reward(graded),
            'workflow_valid': workflow.get('workflow_valid', False),
            'workflow': workflow, 'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest()}


async def main(job_file):
    import t_eval
    job = json.loads(Path(job_file).read_text())
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        result = await execute(job_file)
    except asyncio.CancelledError:
        result = {'status': 'interrupted', 'resolved': None, 'workflow_valid': None}
    except Exception as error:
        result = {'status': 'failed', 'resolved': None, 'workflow_valid': None, 'diagnostic': str(error)}
    t_eval.atomic(Path(job['directory']) / 'outcome.json', result)


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
