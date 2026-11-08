#!/usr/bin/env python3
"""Container-side native workflow entrypoint, including the production outage supervisor."""
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
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def configure(job, workspace, logs):
    home = logs / 'runtime'
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(HOME=str(home), CODEX_HOME=str(home / '.codex'), XDG_CONFIG_HOME=str(home / 'config'),
                      XDG_STATE_HOME=str(home / 'state'), XDG_CACHE_HOME=str(home / 'cache'), TERM='xterm-256color')
    Path(os.environ['CODEX_HOME']).mkdir(exist_ok=True)
    local = helper('t_local_inference')
    config = job['config']
    chosen = local['policy']({'mode': 'local-only' if job['mode'] == 'local-only' else 'automatic',
        'model': config['local_model'], 'provider': config['local_provider'], 'context_tokens': config['context_tokens'],
        'timeout_seconds': min(1800, config['task_seconds']), 'on_cloud_failure': 'local' if job['mode'] == 'outage' else 'pause'})
    document = helper('t_workflow')['defaults']()
    document['workflows']['standard'].update(inference=chosen, stages_enabled=False, instructions='')
    target = Path(os.environ['XDG_CONFIG_HOME']) / 't'
    target.mkdir(parents=True, exist_ok=True)
    (target / 'workflow.json').write_text(json.dumps(document))
    (target / 'local-worker.json').write_text(json.dumps({'enabled': True, 'model': config['local_model'], 'provider': config['local_provider']}))
    if 'local' in job['capability']:
        receipt = home / 'runtime-lease.json'
        receipt.write_text(json.dumps(job['capability']['local']))
        receipt.chmod(0o600)
        os.environ['T_LOCAL_RUNTIME_RECEIPT'] = str(receipt)
    base = job['capability']['frontier_base']
    # Only the deliberate outage needs an immediate transport failure. Ordinary
    # modes retain the native harness's retry behavior for transient failures.
    retries = 'request_max_retries = 0\nstream_max_retries = 0\n' if job['mode'] == 'outage' else ''
    native = f'''model_provider = "evaluation"
model = {json.dumps(config['frontier_model'])}
approval_policy = "never"
sandbox_mode = "danger-full-access"
[model_providers.evaluation]
name = "Evaluation gateway"
base_url = {json.dumps(base)}
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
{retries}[features]
multi_agent = false
[projects.{json.dumps(str(workspace))}]
trust_level = "trusted"
'''
    if config['reasoning_effort'] != 'automatic':
        native = 'model_reasoning_effort = ' + json.dumps(config['reasoning_effort']) + '\n' + native
    (Path(os.environ['CODEX_HOME']) / 'config.toml').write_text(native)
    return local, chosen


def native_command(job, workspace, logs, chosen):
    config = job['config']
    command = [shutil.which('codex') or 'codex', 'exec', '--ignore-rules', '--json',
               '--sandbox', 'danger-full-access', '-c', 'approval_policy="never"',
               '-c', 'features.multi_agent=false', '-c', 'web_search="disabled"',
               '-C', str(workspace), '--output-last-message', str(logs / 'answer.txt')]
    if job['mode'] in ('automatic', 'required-local'):
        local = helper('t_local_inference')
        instructions = local['instructions'](chosen)
        if job['mode'] == 'required-local':
            instructions += '\nYou must delegate a bounded patch assignment through the local worker before editing that assignment yourself. Collect its result, inspect the patch and run validation before accepting it. You remain the lead.'
        command += local['approval_args'](chosen)
        command += ['-c', 'developer_instructions=' + json.dumps(instructions),
                    '-c', 'mcp_servers.t-local-worker.command=' + json.dumps(str(ROOT / 'bin/t')),
                    '-c', 'mcp_servers.t-local-worker.args=["mcp","--local-worker"]']
        for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'T_LOCAL_RUNTIME_RECEIPT'):
            if key in os.environ:
                command += ['-c', 'mcp_servers.t-local-worker.env.' + key + '=' + json.dumps(os.environ[key])]
    else:
        command += ['-c', 'mcp_servers={}']
    return command


def collect_events(logs):
    events = []
    for path in logs.glob('**/events.jsonl'):
        for line in path.read_text(errors='replace').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def checkpoint(job, kind):
    url = job['capability']['frontier_base'].rsplit('/frontier/v1', 1)[0] + '/control/checkpoint'
    with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({'kind': kind}).encode(),
                                headers={'Content-Type': 'application/json'}), timeout=10) as response:
        json.load(response)


def native_exec(command, instruction, stream, job):
    proc = subprocess.Popen(command + ['-'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    def consume():
        for line in proc.stdout:
            try:
                event = json.loads(line)
                event['observed_at'] = time.time()
                line = json.dumps(event) + '\n'
                if event.get('type') == 'item.completed':
                    checkpoint(job, 'tool_completed')
            except (ValueError, OSError):
                pass
            stream.write(line)
            stream.flush()
    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.stdin.write(instruction)
        proc.stdin.close()
        proc.wait(timeout=job['config']['task_seconds'])
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Also terminate descendants left behind by a cancelled local lead.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        reader.join(timeout=15)
        proc.stdout.close()
    return 124 if timed_out else proc.returncode, timed_out


def outage_child(job, workspace, logs, chosen):
    injected = False
    def observe(message):
        nonlocal injected
        item = message.get('params', {}).get('item', {})
        if message.get('method') == 'item/completed':
            checkpoint(job, 'tool_completed')
        if not injected and message.get('method') == 'item/completed' and item.get('type') == 'commandExecution' and item.get('exitCode') == 0:
            url = job['capability']['frontier_base'].rsplit('/frontier/v1', 1)[0] + '/control/outage'
            with urllib.request.urlopen(urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'}), timeout=10) as response:
                json.load(response)
            injected = True
            (logs / 'outage.json').write_text(json.dumps({'injected': True, 'after_command': item.get('id')}))
    command = [shutil.which('codex') or 'codex', '--no-daemon', '--sandbox', 'danger-full-access',
               '--ask-for-approval', 'never', '--', job['instruction']]
    return helper('t_offline')['supervise'](command, chosen, str(workspace), observer=observe)


def outage(job_file, job, workspace, logs):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
    def session():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    proc = subprocess.Popen([sys.executable, __file__, str(job_file), '--outage-child'], stdin=slave, stdout=slave,
                            stderr=slave, cwd=workspace, preexec_fn=session)
    os.close(slave)
    deadline = time.monotonic() + job['config']['task_seconds']
    completed = False
    try:
        with (logs / 'terminal.log').open('wb') as stream:
            while proc.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], .2)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    stream.write(data)
                    stream.flush()
                    if b'\x1b[6n' in data:
                        os.write(master, b'\x1b[1;1R')
                for path in logs.glob('runtime/state/t/local-worker/lead-*/.codex/sessions/**/*.jsonl'):
                    if '"task_complete"' in path.read_text(errors='replace'):
                        completed = True
                        os.write(master, b'\x03')
                        break
                if completed:
                    proc.wait(timeout=15)
                    break
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        os.close(master)
    return proc.returncode, completed


def execute(job_file, child=False):
    job = json.loads(Path(job_file).read_text())
    workspace, logs = Path(job['workspace']), Path(job['logs'])
    logs.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)
    local, chosen = configure(job, workspace, logs)
    if child:
        return outage_child(job, workspace, logs, chosen)
    started = time.time()
    timed_out = False
    completed = False
    if job['mode'] == 'outage':
        code, completed = outage(job_file, job, workspace, logs)
    else:
        command = native_command(job, workspace, logs, chosen)
        if job['mode'] == 'local-only':
            command = [sys.executable, str(ROOT / 'libexec/t_local_inference.py'), '--lead', *command]
        with (logs / 'events.jsonl').open('w') as stream:
            code, timed_out = native_exec(command, job['instruction'], stream, job)
    workers = []
    for path in logs.glob('runtime/state/t/local-worker/*/result.json'):
        workers.append({**json.loads(path.read_text()), 'finished_at': path.stat().st_mtime})
    events = collect_events(logs)
    commands = [e for e in events if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'command_execution']
    valid = code == 0 and not timed_out if job['mode'] != 'outage' else completed
    if job['mode'] == 'required-local':
        patches = [w for w in workers if w.get('status') == 'completed' and w.get('identity_verified') and w.get('patch')]
        reviewed = [e['item'].get('command', '') for e in commands
                    if patches and e.get('observed_at', 0) > min(w['finished_at'] for w in patches)
                    and e['item'].get('exit_code') == 0]
        valid = valid and bool(patches) and any('diff' in c or '.patch' in c for c in reviewed) and any(
            any(check in c for check in ('test', 'pytest', 'check', 'lint', 'build', 'verify')) for c in reviewed)
    elif job['mode'] == 'outage':
        valid = completed and (logs / 'outage.json').exists()
    result = {'exit_code': code, 'timed_out': timed_out, 'workflow_valid': valid,
              'delegations': len(workers), 'elapsed_seconds': time.time()-started,
              'model': job['config']['local_model'] if job['mode'] == 'local-only' else job['config']['frontier_model'],
              'reasoning_effort': job['config']['reasoning_effort'],
              'effective_effort': job['config']['reasoning_effort'] if job['config']['reasoning_effort'] != 'automatic' else None}
    (logs / 'workflow.json').write_text(json.dumps(result))
    # Native logs may include capability URLs in config diagnostics. Scrub all retained text.
    token = job['capability']['token']
    for path in logs.rglob('*'):
        if path.is_file() and path.suffix in ('.json', '.jsonl', '.txt', '.log', '.toml', '.md'):
            text = path.read_text(errors='replace')
            if token in text:
                path.write_text(text.replace(token, '[redacted-capability]'))
    return code


if __name__ == '__main__':
    raise SystemExit(execute(sys.argv[1], '--outage-child' in sys.argv[2:]))
