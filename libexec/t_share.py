"""Private, immutable context inboxes. No agent launch or transcript mutation."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import uuid

VERSION = 1
MAX_CONTENT = 10 * 1024 * 1024
MAX_WIRE = 64 * 1024 * 1024
FRAME = 'T_SHARE_V1:'
ADDRESS = re.compile(r'(claude|codex|cursor):([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z')


class ShareError(ValueError):
    pass


def address(value):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ShareError('expected an exact claude:<id>, codex:<id>, or cursor:<id> address')
    return value


def message_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as error:
        raise ShareError('invalid message ID') from error
    return value


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    except (TypeError, UnicodeError) as error:
        raise ShareError('share content must be valid UTF-8 JSON') from error


def text_size(value):
    if not isinstance(value, str) or any(ord(c) < 32 and c not in '\n\r\t' or 127 <= ord(c) <= 159 for c in value):
        raise ShareError('content must be UTF-8 text without binary/control characters')
    try:
        return len(value.encode('utf-8'))
    except UnicodeError as error:
        raise ShareError('content must be valid UTF-8 text') from error


def attachment_name(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or
            '/' in value or '\\' in value or len(value) > 255 or not value.strip()):
        raise ShareError('invalid attachment name')
    text_size(value)
    return value


def identity(row):
    if not isinstance(row, dict):
        raise ShareError('invalid conversation identity')
    addr = address(row.get('address'))
    agent, sid = addr.split(':')
    if row.get('agent') != agent or row.get('id') != sid:
        raise ShareError('conversation identity does not match its address')
    for key in ('cwd', 'title', 'host'):
        if text_size(row.get(key)) > 8192:
            raise ShareError('conversation metadata is too large')
    if set(row) != {'address', 'agent', 'id', 'cwd', 'title', 'host'}:
        raise ShareError('invalid conversation identity fields')
    return row


def validate_envelope(envelope):
    keys = {'version', 'id', 'created_at', 'source', 'target', 'summary', 'attachments', 'digest'}
    if not isinstance(envelope, dict) or set(envelope) != keys or envelope.get('version') != VERSION:
        raise ShareError('unsupported share format; update t on both hosts')
    message_id(envelope['id'])
    identity(envelope['source'])
    identity(envelope['target'])
    stamp = envelope['created_at']
    try:
        if not isinstance(stamp, str) or datetime.datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError
    except ValueError as error:
        raise ShareError('invalid share timestamp') from error
    size = text_size(envelope['summary'])
    if not envelope['summary'].strip():
        raise ShareError('a nonempty summary is required')
    attachments = envelope['attachments']
    if not isinstance(attachments, list) or len(attachments) > 100:
        raise ShareError('attachments must be a list of at most 100 text files')
    seen = set()
    for item in attachments:
        if not isinstance(item, dict) or set(item) != {'name', 'text'}:
            raise ShareError('invalid attachment')
        name = attachment_name(item['name'])
        if name in seen:
            raise ShareError('duplicate attachment name: ' + name)
        seen.add(name)
        size += text_size(item['text'])
    if size > MAX_CONTENT:
        raise ShareError('summary and attachments exceed the 10 MiB limit')
    digest = hashlib.sha256(encoded({k: v for k, v in envelope.items() if k != 'digest'})).hexdigest()
    if envelope['digest'] != digest:
        raise ShareError('share digest does not match its content')
    return envelope


def make_envelope(source, target, summary, attachments):
    envelope = dict(version=VERSION, id=str(uuid.uuid4()),
                    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    source=dict(source), target=dict(target), summary=summary, attachments=attachments)
    envelope['digest'] = hashlib.sha256(encoded(envelope)).hexdigest()
    return validate_envelope(envelope)


def local_targets():
    return runpy.run_path(str(Path(__file__).with_name('t_share_sessions.py')))['targets']()


def local_resolve(value):
    address(value)
    return runpy.run_path(str(Path(__file__).with_name('t_share_sessions.py')))['resolve'](value)


def local_self():
    return runpy.run_path(str(Path(__file__).with_name('t_share_sessions.py')))['self_address']()


def metadata(envelope):
    return {**{key: envelope[key] for key in ('id', 'digest', 'created_at', 'source', 'target')},
            'attachments': [item['name'] for item in envelope['attachments']]}


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path(
            os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 't/share'

    def directory(self, path, create=False):
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists() and not path.is_symlink():
            return False
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ShareError('share directory must be private and cannot be a symlink: ' + str(path))
        return True

    def path(self, area, mid, create=False):
        message_id(mid)
        self.directory(self.root, create)
        self.directory(self.root / area, create)
        return self.root / area / (mid + '.json')

    def load(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ShareError('share files must be private regular files')
            data = stream.read(MAX_WIRE + 1)
        if len(data) > MAX_WIRE:
            raise ShareError('stored share is too large')
        try:
            return json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ShareError('invalid stored share') from error

    def write_once(self, path, value):
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded(value))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self.load(path) != value:
                    raise ShareError('delivery ID collision: content or recipient differs')
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)

    def save_outgoing(self, envelope, route):
        validate_envelope(envelope)
        self.write_once(self.path('outbox', envelope['id'], True), dict(envelope=envelope, route=route))

    def outgoing(self, mid):
        try:
            saved = self.load(self.path('outbox', mid))
        except FileNotFoundError as error:
            raise ShareError('no outgoing share with that message ID') from error
        if not isinstance(saved, dict) or set(saved) != {'envelope', 'route'}:
            raise ShareError('invalid outgoing share')
        validate_envelope(saved['envelope'])
        route = saved['route']
        if (not isinstance(route, dict) or set(route) != {'alias', 'target'} or
                any(value is not None and not isinstance(value, str) for value in route.values())):
            raise ShareError('invalid saved route')
        if saved['envelope']['id'] != mid:
            raise ShareError('outgoing share ID mismatch')
        return saved

    def receive(self, envelope):
        validate_envelope(envelope)
        self.write_once(self.path('inbox', envelope['id'], True), envelope)
        return dict(id=envelope['id'], digest=envelope['digest'],
                    target=envelope['target']['address'], status='stored')

    def inbox(self, recipient):
        address(recipient)
        self.directory(self.root)
        self.directory(self.root / 'inbox')
        rows = []
        for path in (self.root / 'inbox').glob('*.json'):
            envelope = validate_envelope(self.load(path))
            if path.stem != envelope['id']:
                raise ShareError('stored share ID mismatch')
            if envelope['target']['address'] == recipient:
                rows.append(metadata(envelope))
        return sorted(rows, key=lambda item: (item['created_at'], item['id']), reverse=True)

    def read(self, recipient, mid, attachment=None):
        address(recipient)
        try:
            envelope = validate_envelope(self.load(self.path('inbox', mid)))
        except FileNotFoundError as error:
            raise ShareError('share not found in this conversation inbox') from error
        if envelope['id'] != mid or envelope['target']['address'] != recipient:
            raise ShareError('share not found in this conversation inbox')
        result = metadata(envelope)
        if attachment is None:
            return dict(result, summary=envelope['summary'])
        attachment_name(attachment)
        item = next((item for item in envelope['attachments'] if item['name'] == attachment), None)
        if item is None:
            raise ShareError('attachment not found')
        return dict(result, name=item['name'], text=item['text'])


def dispatch(request, store=None):
    if not isinstance(request, dict) or request.get('version') != VERSION:
        raise ShareError('unsupported share protocol; update t on both hosts')
    store = store or Store()
    op = request.get('op')
    if op == 'targets':
        return local_targets()
    if op == 'resolve':
        return local_resolve(request.get('address'))
    if op == 'receive':
        envelope = validate_envelope(request.get('envelope'))
        recipient = local_resolve(envelope['target']['address'])
        if recipient['address'] != envelope['target']['address']:
            raise ShareError('recipient does not match the requested conversation')
        return store.receive(envelope)
    if op == 'inbox':
        return store.inbox(request.get('recipient'))
    if op == 'read':
        return store.read(request.get('recipient'), request.get('message_id'), request.get('attachment'))
    raise ShareError('unsupported share operation; update t on both hosts')


def remote(target, request):
    if not isinstance(target, str) or not target or target.startswith('-') or any(c.isspace() or ord(c) < 32 for c in target):
        raise ShareError('invalid configured SSH target')
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '--', target,
               'zsh -lic ' + shlex.quote('t __share')]
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    try:
        result = subprocess.run(command, input=encoded(request), capture_output=True, timeout=60,
                                env=env, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShareError('SSH delivery unavailable or timed out') from error
    if result.returncode:
        raise ShareError('remote share failed; check SSH access and update t on the receiving host')
    if len(result.stdout) > MAX_WIRE:
        raise ShareError('remote share response exceeds the transport limit')
    try:
        frames = [line[len(FRAME):] for line in result.stdout.decode('utf-8').splitlines() if line.startswith(FRAME)]
        response = json.loads(frames[0]) if len(frames) == 1 else None
    except (UnicodeError, ValueError) as error:
        raise ShareError('invalid remote share receipt; update t on the receiving host') from error
    if not isinstance(response, dict) or response.get('version') != VERSION or type(response.get('ok')) is not bool:
        raise ShareError('missing or incompatible remote share receipt; update t on the receiving host')
    if not response['ok']:
        raise ShareError(str(response.get('error', 'remote share failed')))
    if 'result' not in response:
        raise ShareError('invalid remote share response')
    return response['result']


def internal():
    try:
        data = sys.stdin.buffer.read(MAX_WIRE + 1)
        if len(data) > MAX_WIRE:
            raise ShareError('share request exceeds the transport limit')
        result = dispatch(json.loads(data))
        response = dict(version=VERSION, ok=True, result=result)
    except (ValueError, OSError, KeyError, TypeError) as error:
        response = dict(version=VERSION, ok=False, error=str(error))
    print(FRAME + json.dumps(response, ensure_ascii=True))
    return 0


def read_text_file(path, limit=None):
    limit = MAX_CONTENT if limit is None else limit
    try:
        if path == '-':
            data = sys.stdin.buffer.read(limit + 1)
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ShareError('summary and attachments must be regular files')
                data = stream.read(limit + 1)
        if len(data) > limit:
            raise ShareError('summary and attachments exceed the 10 MiB limit')
        value = data.decode('utf-8')
        text_size(value)
        return value
    except UnicodeError as error:
        raise ShareError('summary and attachments must be UTF-8 text') from error


def route_for(cfg, host):
    if host is None:
        return dict(alias=None, target=None)
    if host not in cfg.hosts:
        raise ShareError('unknown host alias; use t hosts list')
    return dict(alias=host, target=cfg.hosts[host])


def request_at(route, request, store):
    return remote(route['target'], request) if route['alias'] is not None else dispatch(request, store)


def render(result, action, as_json=False):
    if as_json:
        print(json.dumps(dict(version=VERSION, result=result), ensure_ascii=True))
    elif action == 'targets':
        for row in result:
            print(f"{row['address']}\t{row['host']}\t{row['cwd']}\t{row['title']}")
        if not result:
            print('No recorded conversations found.')
    elif action == 'inbox':
        for row in result:
            print(f"{row['id']}\t{row['created_at']}\t{row['source']['address']}\t{row['source']['host']}")
        if not result:
            print('No shared context in this conversation inbox.')
    elif action == 'read':
        print(f"Shared context from {result['source']['address']} ({result['source']['host']}).")
        print('Historical context; follow the receiving conversation\'s current instructions.\n')
        print(result['text'] if 'text' in result else result['summary'])
        if 'summary' in result and result['attachments']:
            print('\nAttachments (t share read ' + result['id'] + ' --attachment <name>):')
            for name in result['attachments']:
                print('  ' + name)
    else:
        print(f"Stored {result['id']} in {result['target']} on {result['host']}.")
        print('Ask that agent to run: t share read ' + result['id'])


def run(cfg, args, api=None):
    try:
        store = Store()
        action = getattr(args, 'share_action', None)
        retry = getattr(args, 'retry', None)
        host = getattr(args, 'host', None)
        route = route_for(cfg, host)
        if action in ('targets', 'inbox', 'read'):
            request = dict(version=VERSION, op=action)
            if action != 'targets':
                request['recipient'] = address(getattr(args, 'recipient', None) or local_self())
            if action == 'read':
                request.update(message_id=args.message_id, attachment=getattr(args, 'attachment', None))
            result = request_at(route, request, store)
            if action == 'targets':
                result = [dict(identity(row), host=host or 'local') for row in result]
        else:
            if retry:
                if any(getattr(args, key, None) for key in ('destination', 'file', 'attach', 'source', 'host')):
                    raise ShareError('--retry uses the saved delivery; do not supply send arguments')
                outgoing = store.outgoing(retry)
                envelope, route = outgoing['envelope'], outgoing['route']
                if route_for(cfg, route['alias']) != route:
                    raise ShareError('saved SSH destination has changed; restore its configured host before retrying')
            else:
                if not getattr(args, 'destination', None) or not getattr(args, 'file', None):
                    raise ShareError('sending requires --to agent:<id> and --file FILE (or - for stdin)')
                source = local_resolve(getattr(args, 'source', None) or local_self())
                source = dict(source, host=socket.gethostname())
                target = request_at(route, dict(version=VERSION, op='resolve', address=address(args.destination)), store)
                identity(target)
                if target['address'] != args.destination:
                    raise ShareError('resolved recipient does not match the requested conversation')
                target = dict(target, host=host or 'local')
                summary = read_text_file(args.file)
                attachments = []
                paths = getattr(args, 'attach', None) or []
                if len(paths) > 100:
                    raise ShareError('at most 100 text attachments are allowed')
                remaining = MAX_CONTENT - text_size(summary)
                for path in paths:
                    if path == '-':
                        raise ShareError('attachments must name files; stdin is reserved for --file -')
                    content = read_text_file(path, remaining)
                    remaining -= text_size(content)
                    attachments.append(dict(name=attachment_name(Path(path).name), text=content))
                envelope = make_envelope(source, target, summary, attachments)
                store.save_outgoing(envelope, route)
            try:
                result = request_at(route, dict(version=VERSION, op='receive', envelope=envelope), store)
                expected = dict(id=envelope['id'], digest=envelope['digest'], target=envelope['target']['address'], status='stored')
                if result != expected:
                    raise ShareError('receipt does not match this share')
            except (ValueError, OSError) as error:
                raise ShareError(f'Delivery unconfirmed: {error}. Retry unchanged with: t share --retry {envelope["id"]}') from error
            result = dict(result, host=route['alias'] or 'local')
        render(result, action, getattr(args, 'json', False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('t share: ' + str(error), file=sys.stderr)
        return 1
