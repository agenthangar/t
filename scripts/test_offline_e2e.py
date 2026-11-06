#!/usr/bin/env python3
"""Opt-in macOS native CLI outage test with client/tool internet access denied.

Uses a headless PTY, installed weights and an existing loopback LM Studio runtime.
Does not alter system networking or sandbox the preexisting model daemon.
"""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import runpy
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'mlx-community/Qwen3.8-27B-8bit'
PROMPT = ('Fix contains() to implement its documented half-open interval. Keep tests unchanged, '
          'make the smallest source patch and run python3 -m unittest -v. Do not commit or install anything.')


def child(directory, model):
    # An IP avoids mistaking a DNS outage for enforced outbound denial.
    try:
        connection = socket.create_connection(('1.1.1.1',443),timeout=3)
    except PermissionError:
        pass
    else:
        connection.close()
        raise RuntimeError('External networking was not denied by the test sandbox')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen(1)
        with socket.create_connection(listener.getsockname(),timeout=3):
            connection,_=listener.accept();connection.close()
    (directory/'network.json').write_text(json.dumps({'external':'denied by OS','loopback':'reachable',
        'scope':'launcher, native cloud/local clients and their descendant tools; preexisting model daemon excluded'}))
    local=runpy.run_path(str(ROOT/'libexec/t_local_inference.py'))
    selected=local['policy']({'mode':'automatic','model':model,'on_cloud_failure':'local'})
    # macOS cannot nest sandbox-exec; the outer test profile supplies write/network bounds.
    return local['lead']([shutil.which('codex'),'--no-daemon','--sandbox','danger-full-access',
                          '--ask-for-approval','never','--',PROMPT],cwd=str(directory/'repo'),chosen=selected)


def completed(directory):
    for path in directory.glob('state/t/local-worker/lead-*/.codex/sessions/**/*.jsonl'):
        try:
            rows=[json.loads(line) for line in path.read_text().splitlines()]
        except (OSError,ValueError):
            continue
        if any(r.get('type')=='event_msg' and r.get('payload',{}).get('type')=='task_complete' for r in rows):
            return True
    return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',default=MODEL)
    parser.add_argument('--child',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.child:
        return child(args.child,args.model)
    if sys.platform!='darwin' or not shutil.which('sandbox-exec') or not shutil.which('codex'):
        parser.error('Requires macOS sandbox-exec and installed Codex CLI')
    local=runpy.run_path(str(ROOT/'libexec/t_local_inference.py'))
    if not local['receipt'](args.model):parser.error('Install and verify the selected MLX model first')
    directory=Path(tempfile.mkdtemp(prefix='t-offline-e2e-')).resolve()
    workspace=directory/'repo';workspace.mkdir()
    (workspace/'ranges.py').write_text('def contains(start, stop, value):\n    """Half-open interval [start, stop)."""\n    return start <= value <= stop\n')
    tests='''import unittest
from ranges import contains
class Ranges(unittest.TestCase):
    def test_bounds(self):
        self.assertTrue(contains(1,4,1))
        self.assertFalse(contains(1,4,4))
        self.assertFalse(contains(2,2,2))
        self.assertFalse(contains(1,4,0))
if __name__ == '__main__': unittest.main()
'''
    (workspace/'test_ranges.py').write_text(tests)
    (workspace/'.gitignore').write_text('__pycache__/\n')
    for command in (['git','init','-q'],['git','add','.'],['git','-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','core.hooksPath=/dev/null','commit','-qm','fixture']):
        subprocess.run(command,cwd=workspace,check=True,capture_output=True)
    home=directory/'codex';home.mkdir()
    (home/'config.toml').write_text('''model_provider = "offline_fixture"
model = "fixture"
[model_providers.offline_fixture]
name = "Offline fixture"
base_url = "https://1.1.1.1/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
[features]
multi_agent = false
[projects.'''+json.dumps(str(workspace))+''']
trust_level = "trusted"
''')
    env={k:v for k,v in os.environ.items() if not k.startswith(('COV_CORE_','T_DEV_CONTEXT'))}
    env.update(CODEX_HOME=str(home),XDG_CONFIG_HOME=str(directory/'config'),
               XDG_STATE_HOME=str(directory/'state'),XDG_CACHE_HOME=str(directory/'cache'),TERM='xterm-256color')
    config=directory/'config/t';config.mkdir(parents=True)
    workflow=runpy.run_path(str(ROOT/'libexec/t_workflow.py'));doc=workflow['defaults']()
    doc['workflows']['standard'].update(stages_enabled=False,instructions='')
    (config/'workflow.json').write_text(json.dumps(doc))
    profile=directory/'network.sb'
    profile.write_text('(version 1)\n(allow default)\n(deny network-outbound)\n'
                       '(allow network-outbound (remote ip "localhost:*"))\n'
                       '(allow network-outbound (remote unix-socket))\n'
                       '(deny file-write*)\n'
                       '(allow file-write* (subpath '+json.dumps(str(directory))+') '
                       '(subpath "/private/tmp") (subpath '+json.dumps(tempfile.gettempdir())+') '
                       '(literal "/dev/null") (regex #"^/dev/ttys"))\n')
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,140,0,0))
    command=['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,str(Path(__file__).resolve()),'--child',str(directory),'--model',args.model]
    print('Evidence directory: '+str(directory),flush=True)
    def terminal_session():
        os.setsid()
        fcntl.ioctl(0,termios.TIOCSCTTY,0)
    process=subprocess.Popen(command,stdin=slave,stdout=slave,stderr=slave,env=env,preexec_fn=terminal_session)
    os.close(slave);deadline=time.monotonic()+900;finished=False;exit_sent=0
    try:
        with (directory/'terminal.log').open('wb') as log:
            while process.poll() is None:
                if time.monotonic()>deadline:raise TimeoutError('Offline acceptance exceeded 15 minutes')
                if select.select([master],[],[],.2)[0]:
                    try:data=os.read(master,65536)
                    except OSError as exc:
                        if exc.errno==errno.EIO:break
                        raise
                    if not data:break
                    log.write(data);log.flush()
                    # Native terminal queries must be answered by the headless terminal.
                    if b'\x1b[6n' in data:os.write(master,b'\x1b[1;1R')
                if not exit_sent and completed(directory):
                    finished=True;exit_sent=time.monotonic()
                if exit_sent and time.monotonic()-exit_sent>1:
                    os.write(master,b'\x03');exit_sent=time.monotonic()
            process.wait(timeout=20)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        os.close(master)
    assert finished,'Local task did not finish; inspect terminal.log'
    assert process.returncode==0, f'Launcher exited {process.returncode}'
    assert (workspace/'test_ranges.py').read_text()==tests,'Model changed acceptance tests'
    check=subprocess.run([sys.executable,'-m','unittest','-v'],cwd=workspace,capture_output=True,text=True)
    (directory/'acceptance.txt').write_text(check.stdout+check.stderr)
    assert check.returncode==0,'Acceptance failed'
    checkpoints=list(directory.glob('state/t/local-worker/outage-*/checkpoint.json'))
    assert len(checkpoints)==1 and json.loads(checkpoints[0].read_text())['requests']==[PROMPT]
    status=json.loads(checkpoints[0].with_name('status.json').read_text())
    assert status['state']=='local_exited' and status['model']==args.model
    terminal=(directory/'terminal.log').read_text(errors='replace')
    assert 'Cloud connection unavailable' in terminal and 'Local only · '+args.model in terminal
    result={'passed':True,'model':args.model,'evidence_directory':str(directory),
            'network':json.loads((directory/'network.json').read_text()),'status':status}
    (directory/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
