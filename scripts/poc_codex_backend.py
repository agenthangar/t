"""Disposable Codex background-server RPC helpers for CLI compatibility probes."""
import base64
import json
import os
import socket
import struct
import subprocess
import time

class Rpc:
    def __init__(self, sock):
        self.sock = sock
        self.seq = 0

    def send(self, message):
        payload = json.dumps(message).encode()
        mask = os.urandom(4)
        size = len(payload)
        header = bytearray([0x81])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.extend([0x80 | 126])
            header.extend(struct.pack("!H", size))
        else:
            header.extend([0x80 | 127])
            header.extend(struct.pack("!Q", size))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def exact(self, size):
        data = b""
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            if not part:
                raise RuntimeError("server closed the RPC connection")
            data += part
        return data

    def receive(self):
        first, second = self.exact(2)
        opcode = first & 0x0f
        length = second & 0x7f
        if length == 126:
            length = struct.unpack("!H", self.exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.exact(8))[0]
        if second & 0x80:
            mask = self.exact(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self.exact(length)))
        else:
            payload = self.exact(length)
        if opcode == 9:
            return self.receive()
        if opcode != 1:
            raise RuntimeError(f"unexpected WebSocket opcode {opcode}")
        return json.loads(payload)

    def request(self, method, params=None, timeout=8):
        self.seq += 1
        req_id = self.seq
        message = {"id": req_id, "method": method, "params": params if params is not None else {}}
        self.send(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            response = self.receive()
            if response.get("id") != req_id:
                continue
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            return response["result"]
        raise TimeoutError(method)

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

def connect(path, name):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: " + key +
                  "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    headers = b""
    while b"\r\n\r\n" not in headers:
        headers += sock.recv(4096)
    if not headers.startswith(b"HTTP/1.1 101 ") and not headers.startswith(b"HTTP/1.1 101\r"):
        raise RuntimeError("WebSocket upgrade failed: " + repr(headers[:300]))
    client = Rpc(sock)
    client.request("initialize", {"clientInfo": {"name": name, "title": name, "version": "0"}})
    client.notify("initialized", {})
    return client

def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
