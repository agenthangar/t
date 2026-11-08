#!/usr/bin/env python3
"""Opt-in real-model acceptance test. Uses installed weights; never runs in CI."""
import argparse
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']


@_spinner('Running local inference acceptance fixture')
def run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=1200, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('hybrid','local-only'), required=True)
    parser.add_argument('--model', default='mlx-community/Qwen3.8-27B-8bit')
    args = parser.parse_args()
    binary = shutil.which('codex')
    if not binary:
        parser.error('Codex CLI is required')
    local = runpy.run_path(str(ROOT / 'libexec/t_local_inference.py'))
    if not local['receipt'](args.model):
        parser.error('Install the exact artifact first with t config models install MODEL')
    directory = Path(tempfile.mkdtemp(prefix='t-local-inference-e2e-'))
    workspace = directory / 'repo'
    workspace.mkdir()
    (workspace / '.gitignore').write_text('__pycache__/\n')
    (workspace / 'ranges.py').write_text('def contains(start, stop, value):\n    """Half-open interval [start, stop)."""\n    return start <= value <= stop\n')
    (workspace / 'test_ranges.py').write_text('''import unittest
from ranges import contains
class Ranges(unittest.TestCase):
    def test_endpoints(self):
        self.assertTrue(contains(1, 4, 1))
        self.assertTrue(contains(1, 4, 3))
        self.assertFalse(contains(1, 4, 4))
        self.assertFalse(contains(2, 2, 2))
        self.assertFalse(contains(1, 4, 0))
if __name__ == '__main__': unittest.main()
''')
    env = {k:v for k,v in os.environ.items() if not k.startswith(('COV_CORE_', 'T_DEV_CONTEXT'))}
    env.update(XDG_CONFIG_HOME=str(directory/'config'), XDG_STATE_HOME=str(directory/'state'), XDG_CACHE_HOME=str(directory/'cache'))
    for command in (['git','init','-q'], ['git','add','.'], ['git','-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','core.hooksPath=/dev/null','commit','-qm','fixture']):
        run(command,cwd=workspace,env=env,capture_output=True)
    config = directory/'config/t';config.mkdir(parents=True)
    workflow = runpy.run_path(str(ROOT / 'libexec/t_workflow.py'))
    document = workflow['defaults']()
    policy = local['policy']({'mode':'automatic' if args.mode=='hybrid' else 'local-only', 'model':args.model, 'timeout_seconds':900})
    document['workflows']['standard'].update(inference=policy,stages_enabled=False,instructions='')
    (config/'workflow.json').write_text(json.dumps(document))
    (config/'local-worker.json').write_text(json.dumps({'enabled':True,'model':args.model,'provider':'lmstudio'}))
    prompt = ('Fix contains() so its documented half-open interval behavior passes the existing unittest suite. '
              'Keep the tests unchanged, make the smallest source patch, run python3 -m unittest -v and report actual evidence. '
              'Do not commit, push or install anything. The repository is '+str(workspace)+'.')
    command = [binary,'exec','--ignore-user-config','--ignore-rules','--ephemeral','--json',
               '--sandbox','workspace-write','-c','approval_policy="never"',
               '-C',str(workspace),'--output-last-message',str(directory/'answer.txt')]
    if args.mode=='hybrid':
        command += local['approval_args'](policy)
        for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
            command += ['-c', 'mcp_servers.t-local-worker.env.'+key+'='+json.dumps(env[key])]
        guidance = local['instructions'](policy) + ' Validate returned patches against this fixture before applying. Do not use subagents other than the local MCP worker.'
        command += ['--model','gpt-6.1-sol','-c','model_reasoning_effort="medium"',
                    '-c','developer_instructions='+json.dumps(guidance),
                    '-c','mcp_servers.t-local-worker.command='+json.dumps(str(ROOT/'bin/t')),
                    '-c','mcp_servers.t-local-worker.args=["mcp","--local-worker"]',
                    '-c','mcp_servers.t-local-worker.startup_timeout_sec=30']
    else:
        command = [sys.executable,str(ROOT/'libexec/t_local_inference.py'),'--lead',*command]
    command += ['-']
    print('Evidence directory: '+str(directory),flush=True)
    with (directory/'lead-events.jsonl').open('w') as output:
        run(command,cwd=workspace,env=env,input=prompt,text=True,stdout=output,stderr=subprocess.STDOUT)
    check = run([sys.executable,'-m','unittest','-v'],cwd=workspace,env=env,capture_output=True,text=True)
    (directory/'acceptance.txt').write_text(check.stdout+check.stderr)
    run(['git','diff','--exit-code','--','test_ranges.py'],cwd=workspace,capture_output=True)
    tasks = [json.loads(p.read_text()) for p in (directory/'state/t/local-worker').glob('*/result.json')]
    if args.mode=='hybrid':
        assert tasks, 'Cloud lead did not delegate; this is not an automatic-routing pass'
        assert any(t['status']=='completed' and t['model']==args.model and t.get('identity_verified') and t.get('patch') and any(e.get('exit_code')==0 for e in t.get('evidence',[])) for t in tasks), 'Missing verified worker patch and observed passing command evidence'
        results = list((directory/'state/t/local-worker').glob('*/result.json'))
        assert (workspace/'ranges.py').stat().st_mtime_ns > min(p.stat().st_mtime_ns for p in results), 'Lead duplicated the assigned source edit before receiving a worker result'
    else:
        assert 'Local only · '+args.model in (directory/'lead-events.jsonl').read_text(), 'Missing local lead runtime identity'
    result={'mode':args.mode,'model':args.model,'passed':True,'evidence_directory':str(directory),'local_tasks':len(tasks)}
    (directory/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
