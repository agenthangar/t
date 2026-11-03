"""Independent raw WebSocket peer for app-server proxy protocol tests."""
import base64
import hashlib
import json
import struct
import sys


def exact(n):
    data = bytearray()
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def frame(payload, opcode=1, final=True):
    size = len(payload)
    length = bytes([size]) if size < 126 else b'\x7e' + struct.pack('!H', size) if size <= 65535 else b'\x7f' + struct.pack('!Q', size)
    sys.stdout.buffer.write(bytes([(128 if final else 0) | opcode]) + length + payload)
    sys.stdout.buffer.flush()


def read_frame():
    first, second = exact(2)
    assert first & 128 and second & 128, 'client frames must be final and masked'
    size = second & 127
    if size == 126: size = struct.unpack('!H', exact(2))[0]
    elif size == 127: size = struct.unpack('!Q', exact(8))[0]
    mask = exact(4)
    data = bytes(c ^ mask[i % 4] for i, c in enumerate(exact(size)))
    return first & 15, data


def main():
    mode = sys.argv[1]
    if mode == 'timeout':
        sys.stdin.buffer.read()
        return
    request = bytearray()
    while not request.endswith(b'\r\n\r\n'): request.extend(exact(1))
    assert request.startswith(b'GET / HTTP/1.1\r\n')
    assert b'Origin:' not in request
    key = next(line.split(b':', 1)[1].strip() for line in request.split(b'\r\n') if line.startswith(b'Sec-WebSocket-Key:'))
    accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
    if mode == 'http-large':
        sys.stdout.buffer.write(b'x' * 17000); sys.stdout.buffer.flush(); return
    header = b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n'
    if mode == 'http-invalid': header = header.replace(accept, b'invalid')
    sys.stdout.buffer.write(header); sys.stdout.buffer.flush()
    if mode == 'echo':
        while True:
            _, data = read_frame(); frame(data)
    while True:
        opcode, data = read_frame()
        assert opcode == 1
        message = json.loads(data)
        if 'id' not in message: continue
        assert message['method'] in ('initialize', 'thread/loaded/list')
        if mode == 'disconnect': return
        if mode == 'bad-json': frame(b'{bad}'); return
        if mode == 'bad-utf8': frame(b'\xff'); return
        if mode == 'too-large':
            sys.stdout.buffer.write(b'\x81\x7f' + struct.pack('!Q', 1048577)); sys.stdout.buffer.flush(); return
        if mode == 'refused': frame(json.dumps({'id': message['id'], 'error': {}}).encode()); return
        result = {} if message['method'] == 'initialize' else {'data': [], 'nextCursor': None}
        frame(b'{"method":"notification"}')
        reply = json.dumps({'id': message['id'], 'result': result}).encode()
        frame(reply[:4], final=False)
        frame(b'hello', opcode=9)
        pong, ping_data = read_frame()
        assert pong == 10 and ping_data == b'hello'
        frame(reply[4:], opcode=0)


if __name__ == '__main__':
    try:
        main()
    except EOFError:
        pass
