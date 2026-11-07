"""Run-scoped inference gateway: exact models, request timing, no credential export."""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit


def frontier_connection():
    key = os.environ.get('OPENAI_API_KEY')
    if key:
        return 'https://api.openai.com/v1', {'Authorization': 'Bearer ' + key}, 'api-estimate'
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    try:
        auth = json.loads((home / 'auth.json').read_text())
    except (OSError, ValueError):
        raise ValueError('frontier authentication unavailable; sign in with codex login or set OPENAI_API_KEY') from None
    if auth.get('OPENAI_API_KEY'):
        return 'https://api.openai.com/v1', {'Authorization': 'Bearer ' + auth['OPENAI_API_KEY']}, 'api-estimate'
    token = auth.get('tokens', {}).get('access_token')
    account = auth.get('tokens', {}).get('account_id')
    if not token or not account:
        raise ValueError('Codex credentials are unavailable; run codex login')
    return 'https://chatgpt.com/backend-api/codex', {'Authorization': 'Bearer ' + token, 'ChatGPT-Account-Id': account}, 'api-equivalent'


def usage_record(payload):
    response = payload.get('response', payload)
    usage = response.get('usage') or {}
    output = usage.get('output_tokens', usage.get('completion_tokens'))
    incoming = usage.get('input_tokens', usage.get('prompt_tokens'))
    cached = (usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)
    if type(output) is not int or type(incoming) is not int or min(output, incoming) < 0:
        return None
    if type(cached) is not int or not 0 <= cached <= incoming:
        return None
    return {'input_tokens': incoming, 'cached_input_tokens': cached, 'output_tokens': output,
            'reasoning_tokens': (usage.get('output_tokens_details') or {}).get('reasoning_tokens')}


def price(usage, rates):
    if usage is None or rates is None:
        return None
    return ((usage['input_tokens'] - usage['cached_input_tokens']) * rates['input_per_million'] +
            usage['cached_input_tokens'] * rates['cached_input_per_million'] +
            usage['output_tokens'] * rates['output_per_million']) / 1_000_000


def client_tools_only(payload):
    def allowed(tool):
        if not isinstance(tool, dict):
            return False
        if tool.get('type') == 'namespace':
            return isinstance(tool.get('tools'), list) and all(allowed(t) for t in tool['tools'])
        return tool.get('type') in ('function', 'custom')
    declared = list(payload.get('tools', []))
    for item in payload.get('input', []):
        if isinstance(item, dict) and item.get('type') == 'additional_tools':
            declared.extend(item.get('tools', []))
    return all(allowed(tool) for tool in declared)


class Gateway:
    def __init__(self, config, journal, local=None):
        self.config, self.journal, self.local = config, journal, local
        self.frontier = frontier_connection() if any(m != 'local-only' for m in config['modes']) else None
        self.routes, self.records = {}, []
        events = journal.directory / 'events.jsonl'
        if events.exists():
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # A torn final event cannot establish usage.
                if event.get('kind') == 'model_request_completed':
                    self.records.append(event)
        self.mutex, self.local_lock = threading.RLock(), threading.Lock()
        self.server = None

    def lease(self, trial_id, mode):
        token = secrets.token_urlsafe(32)
        with self.mutex:
            self.routes[token] = {'trial': trial_id, 'mode': mode, 'offline': False}
        port = self.server.server_port
        # Dedicated Colima profile uses Lima's documented host loopback gateway.
        base = f'http://192.168.5.2:{port}/{token}'
        result = {'frontier_base': base + '/frontier/v1', 'port': port, 'token': token}
        if self.local:
            result['local'] = {**self.local[2], 'model': self.config['local_model'], 'provider': self.config['local_provider'],
                               'alias': self.local[1], 'base': base + '/local', 'context_tokens': self.config['context_tokens']}
        return result

    def close_lease(self, token):
        with self.mutex:
            self.routes.pop(token, None)

    def totals(self, trial):
        with self.mutex:
            records = [r for r in self.records if r['trial'] == trial]
        usage = {}
        for provider in ('frontier', 'local'):
            measured = [r for r in records if r['provider'] == provider and r['usage'] is not None]
            usage[provider] = {'requests': len(measured), 'all_requests': sum(r['provider'] == provider for r in records),
                               'output_tokens': sum(r['usage']['output_tokens'] for r in measured),
                               'request_seconds': sum(r['seconds'] for r in measured)}
        cloud = [r for r in records if r['provider'] == 'frontier']
        cost = sum(r['cost_usd'] for r in cloud) if all(r['cost_usd'] is not None for r in cloud) else None
        return {'usage': usage, 'cost_usd': cost, 'cost_kind': self.frontier[2] if self.frontier else 'no-cloud-inference'}

    def budget_exhausted(self):
        limit = self.config['spend_limit_usd']
        if limit is None:
            return False
        if limit == 0:
            return True
        with self.mutex:
            return (any(r['provider'] == 'frontier' and r['cost_usd'] is None for r in self.records) or
                    sum(r['cost_usd'] or 0 for r in self.records) >= limit)

    def __enter__(self):
        gateway = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def setup(self):
                super().setup()
                self.connection.settimeout(60)

            def log_message(self, *_):
                pass

            def reply(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def route(self):
                parts = self.path.split('/', 3)
                if len(parts) != 4 or parts[2] not in ('frontier', 'local', 'control'):
                    return None
                with gateway.mutex:
                    record = gateway.routes.get(parts[1])
                return (record, parts[2], '/' + parts[3]) if record else None

            def do_GET(self):
                route = self.route()
                if route and route[1:] == ('local', '/identity') and gateway.local:
                    self.reply(200, {**gateway.local[2], 'model': gateway.config['local_model'], 'alias': gateway.local[1],
                                     'context_tokens': gateway.config['context_tokens']})
                elif route and route[1] == 'local' and route[2] in ('/v1/models', '/api/tags') and gateway.local:
                    self.reply(200, {'data': [{'id': gateway.local[1]}], 'models': [{'name': gateway.local[1]}]})
                else:
                    self.reply(404, {'error': 'unknown capability'})

            def do_POST(self):
                route = self.route()
                if not route:
                    self.reply(403, {'error': 'invalid capability'})
                    return
                record, provider, path = route
                if provider == 'control' and path == '/checkpoint':
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 4096:
                        self.reply(400, {'error': 'invalid checkpoint'})
                        return
                    try:
                        event = json.loads(self.rfile.read(length))
                        kind = event['kind']
                        if kind not in ('tool_completed', 'phase_changed'):
                            raise ValueError('unknown event')
                    except (ValueError, KeyError, TypeError):
                        self.reply(400, {'error': 'invalid checkpoint'})
                        return
                    gateway.journal.event(kind, trial=record['trial'])
                    self.reply(200, {'saved': True})
                    return
                if provider == 'control' and path == '/outage' and record['mode'] == 'outage':
                    with gateway.mutex:
                        record['offline'] = True
                    gateway.journal.event('outage_injected', trial=record['trial'])
                    self.reply(200, {'offline': True})
                    return
                if provider not in ('frontier', 'local') or path not in ('/v1/responses', '/v1/chat/completions', '/api/chat'):
                    self.reply(403, {'error': 'inference only'})
                    return
                if provider == 'frontier' and (record['mode'] == 'local-only' or not gateway.frontier):
                    self.reply(403, {'error': 'local-only'})
                    return
                if provider == 'frontier' and record['offline']:
                    # A typed connection failure, not an HTTP/auth/quota failure.
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if provider == 'frontier' and gateway.budget_exhausted():
                    self.reply(429, {'error': 'evaluation spend guardrail reached'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 32 * 1024 * 1024:
                        raise ValueError('request body exceeds limit')
                    body = self.rfile.read(length)
                    payload = json.loads(body)
                    selected = gateway.config['frontier_model'] if provider == 'frontier' else gateway.local[1] if gateway.local else None
                    if payload.get('model') != selected:
                        raise ValueError('selected model mismatch')
                    if provider == 'frontier' and record['mode'] == 'outage':
                        outputs = [item.get('output', '') for item in payload.get('input', [])
                                   if isinstance(item, dict) and item.get('type') in ('function_call_output', 'custom_tool_call_output')]
                        if any(isinstance(output, str) and 'Process exited with code 0' in output for output in outputs):
                            record['offline'] = True
                            gateway.journal.event('outage_injected', trial=record['trial'], boundary='successful command output before next model request')
                            self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                    if not client_tools_only(payload):
                        raise ValueError('server-side tools are disabled during evaluation')
                    self.forward(record, provider, path, body)
                except (ValueError, TypeError, KeyError) as error:
                    self.reply(400, {'error': str(error)})

            def forward(self, record, provider, path, body):
                with gateway.local_lock if provider == 'local' else contextlib.nullcontext():
                    started = time.monotonic()
                    usage = None
                    metadata = {}
                    first_byte = None
                    if provider == 'frontier':
                        base, headers, _ = gateway.frontier
                        headers = dict(headers)
                    else:
                        base, _, _ = gateway.local
                        headers = {}
                    parsed = urlsplit(base)
                    connection_type = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
                    connection = connection_type(parsed.hostname, parsed.port, timeout=gateway.config['task_seconds'])
                    headers.update({'Content-Type': 'application/json', 'Accept': self.headers.get('Accept', 'text/event-stream')})
                    target = parsed.path.rstrip('/') + (path[3:] if provider == 'frontier' else path)
                    try:
                        connection.request('POST', target, body=body, headers=headers)
                        response = connection.getresponse()
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        self.close_connection = True
                        buffer = b''
                        for line in response:
                            if first_byte is None:
                                first_byte = time.monotonic() - started
                            self.wfile.write(line)
                            self.wfile.flush()
                            if len(buffer) < 16 * 1024 * 1024:
                                buffer += line
                            candidate = line.removeprefix(b'data: ').strip()
                            try:
                                value = json.loads(candidate)
                                usage = usage_record(value) or usage
                                response_meta = value.get('response', value)
                                metadata.update({key: response_meta[key] for key in ('model', 'service_tier', 'reasoning') if key in response_meta})
                            except (ValueError, AttributeError, TypeError):
                                pass
                        if usage is None:
                            try:
                                usage = usage_record(json.loads(buffer))
                            except (ValueError, AttributeError, TypeError):
                                pass
                    except (OSError, http.client.HTTPException):
                        self.close_connection = True
                    finally:
                        connection.close()
                        event = {'trial': record['trial'], 'provider': provider, 'seconds': time.monotonic() - started,
                                 'first_byte_seconds': first_byte, 'usage': usage,
                                 'response_metadata': metadata,
                                 'cost_usd': price(usage, gateway.config['pricing']) if provider == 'frontier' else 0}
                        if provider == 'frontier' and gateway.config['pricing'] and metadata.get('service_tier', 'default') != gateway.config['pricing']['service_tier']:
                            event['cost_usd'] = None
                        with gateway.mutex:
                            gateway.records.append(event)
                        gateway.journal.event('model_request_completed', **event)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
