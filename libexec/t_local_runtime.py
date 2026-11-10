"""Owned provider instances and conservative resident-memory admission for local inference."""
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import re
import runpy
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
GIB = 1024**3


def helper(name):
    return runpy.run_path(str(ROOT / (name + '.py')))


def local_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('runtime URL must be a plain loopback HTTP address')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or
            parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('runtime URL must be an explicit loopback HTTP address')
    if not parsed.port:
        raise ValueError('runtime URL requires a port')
    return value.rstrip('/')


def request(base, path, payload=None, timeout=5):
    local_url(base)
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as response:
        data = response.read(16 * 1024**2 + 1)
    if len(data) > 16 * 1024**2:
        raise ValueError('provider metadata exceeds limit')
    return json.loads(data)


def available_memory():
    """Available physical memory only; never count swap as capacity."""
    if sys.platform.startswith('linux'):
        try:
            match = re.search(r'^MemAvailable:\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M)
            return int(match[1]) * 1024 if match else None
        except OSError:
            return None
    if sys.platform == 'darwin':
        try:
            raw = subprocess.check_output(['vm_stat'], text=True, timeout=3)
            page = int(re.search(r'page size of (\d+) bytes', raw)[1])
            values = [int(re.search(r'Pages ' + key + r':\s+(\d+)', raw)[1])
                      for key in ('free', 'inactive', 'speculative')]
            return sum(values) * page
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            return None
    return None


def gpu_available():
    try:
        raw = subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
                                      text=True, timeout=3)
        values = [int(x.strip()) * 1024**2 for x in raw.splitlines() if x.strip().isdigit()]
        return max(values) if values else None
    except (OSError, subprocess.SubprocessError):
        return None


def stopped_error(exc):
    reason = getattr(exc, 'reason', exc)
    return isinstance(reason, ConnectionRefusedError) or getattr(reason, 'errno', None) == errno.ECONNREFUSED


class WaitingForMemory(ValueError):
    pass


class Providers:
    def __init__(self, root, settings, persist=lambda: None):
        self.root, self.settings, self.persist = Path(root), settings, persist
        self.instances = {}
        self.stack = ExitStack()
        self.ollama_base = None
        self.ollama_private = False
        self.machine = helper('t_local_models')['hardware']()
        self.external = []
        self.problem = None

    def endpoint(self, provider, start=False):
        config = self.settings['providers'][provider]
        if provider == 'lmstudio':
            base = config['url']
            try:
                request(base, '/v1/models')
            except OSError as exc:
                if not stopped_error(exc):
                    raise ValueError('LM Studio endpoint could not be verified; check its server URL and authentication settings') from None
                if not start or config['mode'] == 'desktop':
                    raise ValueError('LM Studio server is stopped. Open the app and enable its local server.') from None
                if base != 'http://127.0.0.1:1234':
                    raise ValueError('Start the configured LM Studio server before using this endpoint') from None
                run = helper('t_local_inference')['run_lms']
                daemon = json.loads(run('daemon', 'up', '--json'))
                if daemon.get('isDaemon') is not True:
                    raise ValueError('Headless mode requires llmster; choose Desktop app to use the app server')
                run('server', 'start', '--port', '1234', '--bind', '127.0.0.1')
            return base
        if config['mode'] == 'desktop':
            request(config['url'], '/api/version')
            return config['url']
        if self.ollama_base:
            return self.ollama_base
        if not start:
            return None
        # One private daemon per manager, shared across all clients and models.
        w = helper('t_local_worker')
        binary = w['executable']('ollama')
        if not binary:
            raise ValueError('Ollama CLI is missing; use t config local')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = 'http://127.0.0.1:' + str(port)
        env = w['environment'](self.root / 'ollama-home')
        env.update(OLLAMA_HOST=base, OLLAMA_MODELS=str(Path.home() / '.ollama/models'), OLLAMA_NO_CLOUD='1',
                   OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='16', OLLAMA_KEEP_ALIVE='-1')
        proc = subprocess.Popen([binary, 'serve'], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.stack.callback(w['stop'], proc)
        self.ollama_process = proc
        self.ollama_base, self.ollama_private = base, True
        self.persist()
        deadline = time.monotonic() + 20
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                request(base, '/api/version', timeout=1)
                return base
            except OSError:
                time.sleep(.1)
        raise ValueError('The private Ollama server did not become available')

    def describe(self, chosen):
        provider, model, context = chosen['provider'], chosen['model'], chosen['context_tokens']
        base = self.endpoint(provider, start=True)
        local = helper('t_local_inference')
        if provider == 'lmstudio':
            info = local['receipt'](model)
            if not info:
                raise ValueError('Model files are missing; download the model first')
            row = local['report'](local['artifact'](model), self.machine, context)
            inventory = request(base, '/api/v1/models')['models']
            # CLI catalog keys establish artifact identity; no fuzzy display-name matching.
            catalog = json.loads(local['run_lms']('ls', '--json'))
            matches = [x for x in catalog if x.get('indexedModelIdentifier') == model and not x.get('deviceIdentifier')]
            if len(matches) != 1:
                raise ValueError('LM Studio cannot uniquely resolve the selected artifact')
            key = matches[0]['modelKey']
            if sum(x.get('modelKey') == key for x in catalog) != 1 or not any(x.get('key') == key for x in inventory):
                raise ValueError('The configured LM Studio endpoint does not contain the exact artifact')
            estimate = row['memory_bytes']
            revision = info['revision']
        else:
            tags = request(base, '/api/tags')['models']
            matches = [x for x in tags if x.get('name') == model or x.get('model') == model]
            if len(matches) != 1 or not matches[0].get('digest'):
                raise ValueError('Exact Ollama model is not installed; download it first')
            metadata = request(base, '/api/show', {'model': model})
            if metadata.get('remote_host') or metadata.get('remote_model') or 'tools' not in metadata.get('capabilities', []):
                raise ValueError('Select a local model with tool support')
            key, revision = model, matches[0]['digest']
            # This is explicitly an estimate, not a claim that the download size is RAM usage.
            estimate = int(matches[0]['size'] * 1.2 + 4 * GIB * context / 32768)
        identity = (provider, base, model, revision, context)
        return dict(provider=provider, base=base, model=model, revision=revision, context=context,
                    key=key, estimate=estimate, identity=identity)

    def observe(self):
        """Observe known endpoints without starting a provider or treating unknown memory as zero."""
        external = []
        owned = {(x['base'], x['alias']) for x in self.instances.values()}
        lm_base = self.settings['providers']['lmstudio']['url']
        try:
            for model in request(lm_base, '/api/v1/models')['models']:
                for instance in model.get('loaded_instances', []):
                    if (lm_base, instance['id']) not in owned:
                        external.append(dict(provider='lmstudio', model=model.get('display_name', model['key']),
                                             alias=instance['id'], memory_bytes=None, owned=False, base=lm_base))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if not stopped_error(exc):
                external.append(dict(provider='lmstudio', model='Unverified provider allocations', alias=None,
                                     memory_bytes=None, owned=False, base=lm_base))
        bases = {self.settings['providers']['ollama']['url']}
        if self.ollama_base:
            bases.add(self.ollama_base)
        for base in bases:
            try:
                for model in request(base, '/api/ps')['models']:
                    if (base, model['name']) not in owned:
                        external.append(dict(provider='ollama', model=model['name'], alias=model['name'],
                                             memory_bytes=model.get('size'), owned=False, base=base))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if not stopped_error(exc):
                    external.append(dict(provider='ollama', model='Unverified provider allocations', alias=None,
                                         memory_bytes=None, owned=False, base=base))
        self.external = external
        return external

    def room(self, description):
        ram, vram = self.machine.get('ram_bytes'), self.machine.get('vram_bytes')
        available = available_memory()
        if not ram or available is None:
            raise WaitingForMemory('Available physical memory could not be established')
        self.observe()
        if any(x['memory_bytes'] is None for x in self.external):
            raise WaitingForMemory('A model loaded outside t has unknown memory use; unload it in its provider app')
        required = description['estimate']
        used = sum(x['estimate'] for x in self.instances.values()) + sum(x['memory_bytes'] for x in self.external)
        if used + required > ram * self.settings['memory_percent'] / 100 or required > max(0, available - ram * .1):
            raise WaitingForMemory('Waiting for available memory within the machine budget')
        if 'NVIDIA' in self.machine.get('accelerator', ''):
            free_gpu = gpu_available()
            if not vram or free_gpu is None or required > min(vram * .85, free_gpu - vram * .1):
                raise WaitingForMemory('Waiting for GPU memory and headroom')

    def get(self, chosen, pinned=()):
        description = self.describe(chosen)
        identity = description['identity']
        if identity in self.instances:
            instance = self.instances[identity]
            if self.present(instance):
                instance['last_used'] = time.time()
                return instance
            del self.instances[identity]
            raise ValueError('Cached provider instance disappeared; start a new conversation')
        # Reuse an exact externally loaded instance without acquiring unload rights.
        if description['provider'] == 'lmstudio':
            borrowed = [x for row in request(description['base'], '/api/v1/models')['models']
                        if row.get('key') == description['key'] for x in row.get('loaded_instances', [])
                        if x.get('config', {}).get('context_length') == description['context']]
            aliases = [x['id'] for x in borrowed]
        else:
            borrowed = [x for x in request(description['base'], '/api/ps')['models']
                        if x.get('digest') == description['revision'] and x.get('context_length') == description['context']]
            aliases = [x['name'] for x in borrowed]
        if len(aliases) == 1:
            instance = dict(description, alias=aliases[0], owned=False, last_used=time.time())
            self.instances[identity] = instance
            self.persist()
            return instance
        candidates = sorted((x for x in self.instances.values() if x['owned'] and x['alias'] not in pinned), key=lambda x: x['last_used'])
        while True:
            try:
                self.room(description)
                break
            except WaitingForMemory:
                if not candidates:
                    raise
                self.unload(candidates.pop(0))
        provider, base = description['provider'], description['base']
        instance = dict(description, alias=None, owned=(description['provider'] == 'lmstudio' or self.ollama_private), last_used=time.time())
        if provider == 'lmstudio':
            before = {x['id'] for row in request(base, '/api/v1/models')['models'] for x in row.get('loaded_instances', [])}
            result = request(base, '/api/v1/models/load', {'model': description['key'],
                             'context_length': description['context'], 'echo_load_config': True}, timeout=300)
            alias = result['instance_id']
            if alias in before:
                raise ValueError('LM Studio reused an external instance; ownership was not established')
            instance['alias'] = alias
        else:
            # Desktop-server allocations cannot be assumed to belong to t even if names match.
            existing = request(base, '/api/ps')['models']
            if any(x.get('digest') == description['revision'] for x in existing):
                raise WaitingForMemory('This Ollama model is loaded outside t; unload it there before t takes ownership')
            alias = description['model']
            instance['alias'] = alias
            request(base, '/api/generate', {'model': alias, 'keep_alive': -1, 'stream': False,
                                           'options': {'num_ctx': description['context']}}, timeout=300)
        self.instances[identity] = instance
        self.persist()
        if not self.present(instance):
            raise ValueError('Provider did not confirm exact loaded model identity and context')
        return instance

    def present(self, instance):
        if instance['provider'] == 'lmstudio':
            return any(row.get('key') == instance['key'] and any(
                x.get('id') == instance['alias'] and x.get('config', {}).get('context_length') == instance['context']
                for x in row.get('loaded_instances', [])) for row in request(instance['base'], '/api/v1/models')['models'])
        return any(row.get('name') == instance['alias'] and row.get('digest') == instance['revision'] and
                   row.get('context_length') == instance['context'] for row in request(instance['base'], '/api/ps')['models'])

    def unload(self, instance):
        if not instance.get('owned'):
            raise ValueError('Unload this model in its provider app; t did not load it')
        if instance['provider'] == 'lmstudio':
            request(instance['base'], '/api/v1/models/unload', {'instance_id': instance['alias']}, timeout=30)
        else:
            request(instance['base'], '/api/generate', {'model': instance['alias'], 'keep_alive': 0, 'stream': False}, timeout=30)
        if self.present(instance):
            raise ValueError('Provider did not confirm that its owned model stopped')
        self.instances.pop(tuple(instance['identity']), None)
        self.persist()

    def expire(self, pinned=()):
        for instance in list(self.instances.values()):
            if instance['alias'] not in pinned and time.time() - instance['last_used'] > self.settings['idle_seconds']:
                if instance['owned']:
                    self.unload(instance)
                else:
                    self.instances.pop(tuple(instance['identity']), None)
                    self.persist()

    def close(self):
        for instance in list(self.instances.values()):
            if instance['owned']:
                self.unload(instance)
        self.stack.close()
