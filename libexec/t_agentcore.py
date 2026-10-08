"""Submit JSON to an existing IAM-authenticated AgentCore runtime via AWS CLI v2."""

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

MAX_PAYLOAD = 100_000_000
ARN = re.compile(r'arn:(aws(?:-[a-z]+)*):bedrock-agentcore:([a-z0-9-]+):[0-9]{12}:runtime/[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}')


def configure(args):
    arn = args.runtime_arn or os.environ.get('T_AGENTCORE_RUNTIME_ARN', '')
    match = ARN.fullmatch(arn)
    if not match:
        raise ValueError('set --runtime-arn or T_AGENTCORE_RUNTIME_ARN to a runtime ARN')
    sid = args.session_id or str(uuid.uuid4())
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{33,256}', sid):
        raise ValueError('--session-id must contain 33–256 letters, digits, _, ., :, or -')
    if args.qualifier and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', args.qualifier):
        raise ValueError('--qualifier must be an endpoint name (up to 48 letters, digits, or _)')
    if not 1 <= args.timeout <= 28800:
        raise ValueError('--timeout must be between 1 and 28800 seconds')
    return {'runtime_arn': arn, 'region': match[2], 'session_id': sid,
            'qualifier': args.qualifier or 'DEFAULT', 'operation': args.action}


def payload_bytes(args):
    if args.prompt is not None:
        payload = json.dumps({'prompt': args.prompt}, ensure_ascii=False).encode('utf-8')
    else:
        try:
            with open(args.payload, 'rb') as file:
                payload = file.read(MAX_PAYLOAD + 1)
        except OSError:
            raise ValueError('--payload must be a readable JSON file') from None
    if not payload or len(payload) > MAX_PAYLOAD:
        raise ValueError('payload must contain 1–100000000 bytes')
    try:
        value = json.loads(payload, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise ValueError('payload must be valid JSON') from None
    if not isinstance(value, dict):
        raise ValueError('payload must be a JSON object matching the deployed agent schema')
    return payload


def reject_constant(value):
    raise ValueError('non-JSON numeric literal')


@contextmanager
def session_lock(config):
    """Prevent concurrent invokes from this local user; no cross-host claim."""
    root = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 't' / 'agentcore'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = '\0'.join(config[k] for k in ('runtime_arn', 'qualifier', 'session_id'))
    path = root / (hashlib.sha256(key.encode()).hexdigest() + '.lock')
    with path.open('a') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('this session already has a local invocation in progress') from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def command(executable, config, args, temporary):
    operation = 'invoke-agent-runtime' if args.action == 'invoke' else 'stop-runtime-session'
    argv = [executable, 'bedrock-agentcore', operation,
            '--agent-runtime-arn', config['runtime_arn'], '--runtime-session-id', config['session_id'],
            '--qualifier', config['qualifier'], '--region', config['region'],
            '--output', 'json', '--no-cli-pager', '--no-cli-auto-prompt',
            '--cli-connect-timeout', '10', '--cli-read-timeout', str(args.timeout)]
    if args.profile:
        argv += ['--profile', args.profile]
    if args.action == 'invoke':
        argv += ['--content-type', 'application/json', '--accept', 'application/json',
                 '--payload', 'fileb://' + str(temporary / 'payload.json'), str(temporary / 'response')]
    else:
        argv += ['--client-token', str(uuid.uuid4())]
    return argv


def call_aws(argv, config, timeout):
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(AWS_MAX_ATTEMPTS='1', AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
    # Never retry ambiguous submission failures: an agent may already be working.
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=timeout + 15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('AWS CLI failed or timed out; remote outcome may be unknown. No retry was made.') from None
    if result.returncode:
        raise RuntimeError('AWS CLI request failed; check CLI support, authentication, and runtime access. '
                           'Remote outcome may be unknown. No retry was made.')
    try:
        metadata = json.loads(result.stdout)
        status = metadata['statusCode']
        if not isinstance(status, int) or not 200 <= status < 300:
            raise ValueError('non-success status')
        if metadata.get('runtimeSessionId') != config['session_id']:
            raise ValueError('unexpected session')
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise RuntimeError('AWS CLI returned unrecognized response metadata; remote outcome may be unknown.') from None
    return status


def execute(args):
    try:
        config = configure(args)
        payload = payload_bytes(args) if args.action == 'invoke' else None
        if args.dry_run:
            print(json.dumps({**config, 'dry_run': True, 'payload_bytes': len(payload) if payload else 0}))
            return 0
        executable = shutil.which('aws')
        if not executable:
            raise ValueError('install AWS CLI v2 with bedrock-agentcore invoke-agent-runtime and stop-runtime-session support')
        lock = session_lock(config) if args.action == 'invoke' else nullcontext()
        with lock, tempfile.TemporaryDirectory(prefix='t-agentcore-') as directory:
            temporary = Path(directory)
            output = None
            if payload is not None:
                (temporary / 'payload.json').write_bytes(payload)
                # Reserve before network I/O. Never overwrite a response or follow a symlink.
                fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                output = os.fdopen(fd, 'wb')
            with output if output is not None else nullcontext():
                print('AgentCore session: ' + config['session_id'], file=sys.stderr, flush=True)
                status = call_aws(command(executable, config, args, temporary), config, args.timeout)
                if output is not None:
                    with (temporary / 'response').open('rb') as response:
                        shutil.copyfileobj(response, output)
                print(json.dumps({**config, 'status_code': status,
                                  'output': str(Path(args.output).absolute()) if output is not None else None}))
        return 0
    except ValueError as error:
        print('t agentcore: ' + str(error), file=sys.stderr)
        return 2
    except FileExistsError:
        print('t agentcore: --output must name a new file; nothing was submitted.', file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as error:
        message = str(error) if isinstance(error, RuntimeError) else 'local I/O failed; check output storage. Remote outcome may be unknown.'
        print('t agentcore: ' + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('t agentcore: interrupted; the remote session may still be active. Use agentcore stop if needed.', file=sys.stderr)
        return 130
