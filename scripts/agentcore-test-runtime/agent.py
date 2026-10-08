#!/usr/bin/env python3
"""Disposable AgentCore HTTP acceptance target. Standard library only; no model calls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

MAX_BODY = 4096


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.boot_id = None
        self.count = 0

    def invoke(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'prompt'}:
            raise ValueError('expected only a prompt field')
        prompt = payload['prompt']
        if not isinstance(prompt, str) or len(prompt) > 256:
            raise ValueError('prompt must be a string of at most 256 characters')
        if prompt == 'fixture-error':
            raise ValueError('requested fixture error')
        with self.lock:
            # Lazy initialization avoids embedding a shared UUID in a runtime snapshot.
            if self.boot_id is None:
                self.boot_id = str(uuid.uuid4())
            self.count += 1
            return {'fixture': 't-agentcore-acceptance-v1', 'boot_id': self.boot_id,
                    'count': self.count, 'reply': 'ack:' + prompt}


def server(address=('0.0.0.0', 8080)):
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass  # Never log request bodies, headers, or account metadata.

        def reply(self, status, value):
            data = json.dumps(value, sort_keys=True).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200, {'status': 'Healthy'}) if self.path == '/ping' else self.reply(404, {'error': 'not found'})

        def do_POST(self):
            if self.path != '/invocations':
                self.reply(404, {'error': 'not found'})
                return
            # No header values are reflected into responses.
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or len(lengths[0]) > 6 or not lengths[0].isascii() or not lengths[0].isdigit():
                self.reply(400, {'error': 'invalid framing'})
                return
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                self.reply(413, {'error': 'payload size'})
                return
            self.connection.settimeout(5)
            try:
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError('incomplete body')
                value = state.invoke(json.loads(data))
            except (ValueError, UnicodeError, TimeoutError):
                self.reply(400, {'error': 'invalid fixture request'})
                return
            self.reply(200, value)

    return ThreadingHTTPServer(address, Handler)


if __name__ == '__main__':
    server().serve_forever()
