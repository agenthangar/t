"""Bounded RFC 6455 client over the app-server proxy's raw byte pipes.

Codex's Unix control socket uses HTTP Upgrade plus text WebSocket frames,
not the JSONL transport of a newly started stdio app-server.
"""
import base64
import hashlib
import os
from pathlib import Path
import runpy
import select
import struct
import time

LIMIT = 1024 * 1024
_spinner = runpy.run_path(str(Path(__file__).with_name('t_progress.py')))['spinner']


class WebSocket:
    def __init__(self, reader, writer, deadline):
        self.reader, self.writer, self.deadline = reader, writer, deadline
        self.buffer = b''
        os.set_blocking(writer.fileno(), False)

    @_spinner('Waiting for the conversation service')
    def _read(self, size):
        while len(self.buffer) < size:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.reader], [], [], remaining)[0]:
                raise ValueError('runtime timed out')
            data = os.read(self.reader.fileno(), min(65536, size - len(self.buffer)))
            if not data:
                raise ValueError('runtime disconnected')
            self.buffer += data
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    @_spinner('Contacting the conversation service')
    def _write(self, data):
        offset = 0
        while offset < len(data):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.writer], [], remaining)[1]:
                raise ValueError('runtime write timed out')
            try:
                offset += os.write(self.writer.fileno(), data[offset:offset + 65536])
            except BlockingIOError:
                continue

    def handshake(self):
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = ('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key +
                   '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self._write(request.encode('ascii'))
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            if len(header) >= 16384:
                raise ValueError('runtime HTTP upgrade headers too large')
            header.extend(self._read(1))
        lines = header.decode('ascii').split('\r\n')
        headers = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(':')
            name = name.lower()
            if not separator or name in headers:
                raise ValueError('invalid runtime HTTP upgrade headers')
            headers[name] = value.strip()
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if (lines[0].split()[:2] != ['HTTP/1.1', '101'] or
                headers.get('upgrade', '').lower() != 'websocket' or
                'upgrade' not in headers.get('connection', '').lower().replace(' ', '').split(',') or
                headers.get('sec-websocket-accept') != accept):
            raise ValueError('runtime refused WebSocket upgrade')

    def send(self, payload, opcode=1):
        if len(payload) > LIMIT or (opcode >= 8 and len(payload) > 125):
            raise ValueError('runtime request too large')
        length = len(payload)
        size = bytes([0x80 | length]) if length < 126 else (
            b'\xfe' + struct.pack('!H', length) if length <= 65535 else b'\xff' + struct.pack('!Q', length))
        mask = os.urandom(4)
        data = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        self._write(bytes([0x80 | opcode]) + size + mask + data)

    def receive(self):
        message, fragmented = bytearray(), False
        while True:
            first, second = self._read(2)
            final, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80:
                raise ValueError('unsupported runtime WebSocket frame')
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self._read(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8))[0]
            if length > LIMIT or (opcode >= 8 and (not final or length > 125)):
                raise ValueError('runtime WebSocket frame too large or invalid')
            payload = self._read(length)
            if opcode == 8:
                raise ValueError('runtime disconnected')
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if (opcode == 1 and fragmented) or (opcode == 0 and not fragmented) or opcode not in (0, 1):
                raise ValueError('invalid runtime WebSocket continuation')
            message.extend(payload)
            if len(message) > LIMIT:
                raise ValueError('runtime message too large')
            if final:
                return message.decode('utf-8')
            fragmented = True
