"""Real byte-pipe WebSocket interoperability, boundaries and protocol failures."""
from pathlib import Path
import os
import runpy
import struct
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / 'libexec/t_codex_socket.py'))


@pytest.fixture
def peer(tmp_path):
    children = []
    def start(raw=None):
        path = tmp_path / (str(len(children)) + '.bin')
        if raw is not None: path.write_bytes(raw)
        args = [sys.executable,'-u',str(ROOT / 'tests/_websocket_peer.py'),'echo'] if raw is None else [
            sys.executable,'-u','-c',"import sys;sys.stdout.buffer.write(open(sys.argv[1],'rb').read());sys.stdout.buffer.flush();sys.stdin.buffer.read()",str(path)]
        process = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                   env={k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_')})
        children.append(process)
        return MODULE['WebSocket'](process.stdout,process.stdin,time.monotonic()+3)
    yield start
    for process in children:
        process.terminate();process.communicate(timeout=3)


@pytest.mark.parametrize('size',[0,1,125,126,65535,65536])
def test_http_upgrade_and_client_masked_length_boundaries(peer,size):
    connection = peer();connection.handshake()
    payload = b'x'*size
    connection.send(payload)
    assert connection.receive() == payload.decode()


def test_fragmented_text_ping_and_pong(peer):
    connection = peer(b'\x01\x05hello\x89\x01p\x8a\x00\x80\x05world')
    assert connection.receive() == 'helloworld'


@pytest.mark.parametrize('raw,match',[
    (b'\xc1\x00','unsupported'),(b'\x81\x80','unsupported'),
    (b'\x80\x00','continuation'),(b'\x82\x00','continuation'),
    (b'\x01\x01a\x81\x01b','continuation'),(b'\x09\x00','invalid'),
    (b'\x89\x7e\x00\x7e','invalid'),(b'\x88\x00','disconnected'),
    (b'\x81\x7f'+struct.pack('!Q',1048577),'too large'),
])
def test_invalid_server_frames_are_refused(peer,raw,match):
    connection = peer(raw)
    with pytest.raises(ValueError,match=match):connection.receive()


def test_aggregate_message_bound_and_request_bound(peer):
    limit = MODULE['LIMIT']
    connection = peer(b'\x01\x7f'+struct.pack('!Q',limit)+b'x'*limit+b'\x80\x01x')
    with pytest.raises(ValueError,match='message too large'):connection.receive()
    with pytest.raises(ValueError,match='request too large'):connection.send(b'x'*(limit+1))
    with pytest.raises(ValueError,match='request too large'):connection.send(b'x'*126,opcode=10)


def test_expired_write_deadline_is_bounded(peer):
    connection=peer(b'');connection.deadline=time.monotonic()-1
    with pytest.raises(ValueError,match='write timed out'):connection.send(b'{}')


def test_nonblocking_write_retry(peer,monkeypatch):
    connection=peer();connection.handshake()
    original=os.write
    calls=[]
    def write(fd,data):
        calls.append(fd)
        if len(calls)==1:raise BlockingIOError
        return original(fd,data)
    monkeypatch.setattr(os,'write',write)
    connection.send(b'payload')
    assert connection.receive()=='payload' and len(calls)>=2
