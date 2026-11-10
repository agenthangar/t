"""Owned outage supervision uses local fake processes, never cloud or model downloads."""
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import pytest
from conftest import load_script

ROOT = Path(__file__).parents[1]

@pytest.fixture
def offline():
    return load_script(ROOT/'libexec/t_offline.py', 'offline_test')


def error(kind='responseStreamDisconnected', status=None):
    return {'message':'fixture', 'codexErrorInfo':{kind:{'httpStatusCode':status}}}


def started(m, tmp_path):
    c = m.Checkpoint(tmp_path, tmp_path)
    c.client({'id':1,'method':'thread/start','params':{}})
    c.server({'id':1,'result':{'thread':{'id':'main','cwd':str(tmp_path)},'model':'cloud-fixture'}})
    c.client({'method':'turn/start','params':{'threadId':'main','input':[{'type':'text','text':'Fix the bug; preserve tests.'}]}})
    c.server({'method':'turn/started','params':{'threadId':'main','turn':{'id':'turn'}}})
    return c


def event(c, method, **params):
    c.server({'method':method,'params':{'threadId':'main','turnId':'turn',**params}})


def test_typed_errors_only(offline):
    m=offline
    for kind in ('httpConnectionFailed','responseStreamConnectionFailed','responseStreamDisconnected'):
        assert m.connection_failure(error(kind))
        for status in (401,403,429,500,503):assert not m.connection_failure(error(kind,status))
    for value in (None,{}, {'message':'network disconnected'}, {'codexErrorInfo':'usageLimitExceeded'},
                  {'codexErrorInfo':'unauthorized'}, {'codexErrorInfo':'rateLimitExceeded'},
                  {'codexErrorInfo':{'httpConnectionFailed':{}}}, {'codexErrorInfo':{'httpConnectionFailed':None}}):
        assert not m.connection_failure(value)
    exhausted=error('responseTooManyFailedAttempts')
    assert not m.exhausted_connection(exhausted,False)
    assert m.exhausted_connection(exhausted,True)


def test_primary_checkpoint_and_retry(offline,tmp_path,monkeypatch):
    m=offline;c=started(m,tmp_path);now=[100]
    monkeypatch.setattr(m.time,'monotonic',lambda:now[0])
    for params in ({'ephemeral':True,'threadSource':'thread_title'}, {'threadSource':'subAgent'}):
        c.client({'id':2,'method':'thread/start','params':params})
        c.server({'id':2,'result':{'thread':{'id':'aux','cwd':str(tmp_path)}}})
    assert c.thread=='main'
    event(c,'error',threadId='aux',error=error())
    event(c,'error',turnId='old',error=error())
    assert not c.due()
    event(c,'error',error=error());assert not c.due()
    now[0]+=31;assert c.due()
    event(c,'item/agentMessage/delta',delta='working');assert not c.due()
    event(c,'error',error=error())
    event(c,'error',error=error(status=401));assert c.network_since is None
    event(c,'error',error=error())
    event(c,'turn/completed',turn={'id':'turn','status':'failed','error':error('responseTooManyFailedAttempts')})
    assert c.due()
    c.client({'method':'turn/interrupt','params':{'threadId':'main'}})
    event(c,'error',error=error());now[0]+=100
    event(c,'turn/completed',turn={'id':'turn','status':'failed','error':error()})
    assert not c.due()
    c.client({'method':'turn/steer','params':{'threadId':'main','input':[{'type':'text','text':'Also preserve comments.'}]}})
    event(c,'error',error=error());now[0]+=31;assert c.due()
    event(c,'turn/completed',turn={'id':'turn','status':'interrupted'})
    assert not c.due()
    event(c,'item/completed',item={'type':'commandExecution','command':'python tests.py','status':'completed','exitCode':0,'aggregatedOutput':None})
    for i in range(12):event(c,'item/completed',item={'type':'agentMessage','text':str(i)*4000})
    event(c,'item/completed',item={'type':'fileChange','status':'completed','changes':[{'path':'a.py'}]})
    event(c,'item/completed',item={'type':'plan','text':'inspect then test'})
    event(c,'item/completed',item={'type':'reasoning','text':'PRIVATE REASONING'})
    prompt=c.prompt();assert 'Fix the bug' in prompt and 'Also preserve' in prompt and 'PRIVATE REASONING' not in prompt
    assert len(c.evidence)==8 and len(prompt)<30000
    data=json.loads((tmp_path/'checkpoint.json').read_text())
    assert data['cloud_model']=='cloud-fixture' and data['thread_id']=='main'
    assert (tmp_path/'checkpoint.json').stat().st_mode & 0o777 == 0o600


def test_incomplete_checkpoint_pauses(offline,tmp_path):
    m=offline;c=m.Checkpoint(tmp_path,tmp_path)
    with pytest.raises(ValueError,match='No complete'):c.prompt()
    c=started(m,tmp_path);c.user([{'type':'image','imageUrl':'x'}])
    with pytest.raises(ValueError,match='non-text'):c.prompt()
    c=started(m,tmp_path);c.user([{'type':'text','text':'a'*m.USER_LIMIT}])
    with pytest.raises(ValueError,match='budget'):c.prompt()
    c=started(m,tmp_path)
    c.client({'id':3,'method':'thread/resume','params':{}})
    c.server({'id':3,'result':{'thread':{'id':'another','cwd':'/other','turns':[{'items':[{'type':'userMessage','content':[{'type':'text','text':'history'}]}]}]}}})
    with pytest.raises(ValueError,match='workspace'):c.prompt()
    assert c.users==['history']


def frame(data=b'',opcode=1,final=True):
    n=len(data);mask=b'abcd'
    size=bytes([n|128]) if n<126 else b'\xfe'+struct.pack('!H',n) if n<=65535 else b'\xff'+struct.pack('!Q',n)
    return bytes([(128 if final else 0)|opcode])+size+mask+bytes(b^mask[i%4] for i,b in enumerate(data))


class Wire:
    def __init__(self,data):self.data=bytearray(data);self.sent=b''
    def recv(self,n):v=self.data[:n];del self.data[:n];return bytes(v)
    def sendall(self,data):self.sent+=data


def test_websocket_handshake_and_frames(offline):
    m=offline
    header=b'GET /rpc HTTP/1.1\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: '+base64.b64encode(b'a'*16)+b'\r\n\r\n'
    wire=Wire(header);m.WebSocket(wire).handshake();assert b'101 Switching' in wire.sent
    for invalid in (b'x'*16384,header.replace(b'GET /rpc',b'GET /bad'),header.replace(b'Upgrade: websocket',b'Upgrade websocket'),header.replace(b'Connection:',b'Upgrade:')):
        with pytest.raises(ValueError):m.WebSocket(Wire(invalid)).handshake()
    for size in (1,126,65536):
        data=b'x'*size;wire=Wire(frame(data));ws=m.WebSocket(wire)
        assert ws.receive()==data;ws.send(data);assert wire.sent.endswith(data)
    wire=Wire(frame(b'ping',9)+frame(b'',10)+frame(b'a',final=False)+frame(b'b',opcode=0))
    assert m.WebSocket(wire).receive()==b'ab' and wire.sent==b'\x8a\x04ping'
    for invalid in (b'\x81\x00',b'\xc1\x80',frame(b'',0),frame(b'',2),frame(b'a',final=False)+frame(b'b'),frame(b'',9,False),frame(b'x'*126,9)):
        with pytest.raises(ValueError):m.WebSocket(Wire(invalid)).receive()
    for invalid in (b'',frame(b'',8)):
        with pytest.raises(EOFError):m.WebSocket(Wire(invalid)).receive()


def test_websocket_limits(offline,monkeypatch):
    m=offline;monkeypatch.setattr(m,'FRAME_LIMIT',3)
    with pytest.raises(ValueError):m.WebSocket(Wire(b'')).send(b'four')
    with pytest.raises(ValueError):m.WebSocket(Wire(frame(b'four'))).receive()
    with pytest.raises(ValueError):m.WebSocket(Wire(frame(b'abc',final=False)+frame(b'd',0))).receive()


def test_launch_options(offline,tmp_path):
    m=offline
    server,local=m.launch_options(['codex','--model','cloud','--sandbox','workspace-write','-a','on-request','--add-dir','/extra','-c','approval_policy="never"','--enable','feature','-C',str(tmp_path),'--','--profile'],tmp_path)
    assert 'model="cloud"' in server and 'cloud' not in local and '--add-dir' in local
    assert 'approval_policy="never"' in local
    for option in ('exec','e','review','resume','fork','--remote','--profile','--oss','--worktree','--model=foo'):
        with pytest.raises(ValueError):m.launch_options(['codex',option],tmp_path)
    with pytest.raises(ValueError,match='missing'):m.launch_options(['codex','-m'],tmp_path)
    with pytest.raises(ValueError,match='workspace'):m.launch_options(['codex','-C','/other'],tmp_path)
    assert 'approvals_reviewer="auto_review"' in m.launch_options(['codex','--approve-for-me'],tmp_path)[0]
    assert '--dangerously-bypass-approvals-and-sandbox' in m.launch_options(['codex','--dangerously-bypass-approvals-and-sandbox'],tmp_path)[1]


def test_stop_escalation(offline,monkeypatch):
    m=offline;calls=[]
    def wait(timeout):
        calls.append('wait')
        if calls.count('wait')==1:raise subprocess.TimeoutExpired('fixture',timeout)
    p=SimpleNamespace(pid=123,poll=lambda:None,wait=wait,terminate=lambda:calls.append('term'),kill=lambda:calls.append('kill'))
    m.stop(None);m.stop(p);assert calls==['term','wait','kill','wait']
    calls.clear();monkeypatch.setattr(m.os,'killpg',lambda pid,sig:calls.append(sig))
    m.stop(p,True);assert m.signal.SIGKILL in calls
    monkeypatch.setattr(m.os,'killpg',lambda *a:(_ for _ in ()).throw(ProcessLookupError()))
    m.stop(p,True)


@pytest.fixture
def fake_codex(tmp_path):
    script=tmp_path/'codex'
    script.write_text('#!'+sys.executable+'\n'+'''import json,os,sys,socket,runpy,time
from pathlib import Path
assert not any(key.startswith('COV_CORE_') for key in os.environ)
root=Path(os.environ['FIXTURE_ROOT']);mode=os.environ.get('FIXTURE_MODE','outage')
role='backend' if sys.argv[1]=='app-server' else 'frontend'
(root/(role+'.pid')).write_text(str(os.getpid()))
def send(v):print(json.dumps(v),flush=True)
if role=='backend':
 for raw in sys.stdin:
  msg=json.loads(raw)
  if msg.get('method')=='thread/start':send({'id':1,'result':{'thread':{'id':'main','cwd':str(root)},'model':'cloud'}})
  elif msg.get('method')=='turn/start':
   send({'method':'turn/started','params':{'threadId':'main','turn':{'id':'turn'}}})
   send({'id':'approve','method':'item/commandExecution/requestApproval','params':{'threadId':'main','command':'fixture'}})
  elif msg.get('id')=='approve':
   (root/'approval.json').write_text(json.dumps(msg))
   err={'codexErrorInfo':{'responseStreamDisconnected':{'httpStatusCode':None}}}
   if mode=='auth':err={'codexErrorInfo':'unauthorized'}
   if mode=='transport':sys.exit(1)
   send({'method':'error','params':{'threadId':'main','turnId':'turn','error':err,'willRetry':True}})
   if mode!='retry':send({'method':'turn/completed','params':{'threadId':'main','turn':{'id':'turn','status':'failed','error':err}}})
else:
 if mode=='quit':sys.exit(0)
 sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.connect(sys.argv[2].removeprefix('unix://'))
 ws=runpy.run_path(os.environ['SOCKET_HELPER'])['WebSocket'](sock.makefile('rb'),sock.makefile('wb'),time.monotonic()+15)
 ws.handshake();ws.send(json.dumps({'id':1,'method':'thread/start','params':{}}).encode());ws.receive()
 ws.send(json.dumps({'id':2,'method':'turn/start','params':{'threadId':'main','input':[{'type':'text','text':'Fix fixture; preserve tests.'}]}}).encode())
 ws.receive();approval=json.loads(ws.receive())
 assert approval['id']=='approve'
 ws.send(json.dumps({'id':'approve','result':{'decision':'accept'}}).encode())
 if mode=='cancel':ws.send(json.dumps({'method':'turn/interrupt','params':{'threadId':'main'}}).encode())
 if mode in ('auth','cancel'):time.sleep(.4);sys.exit(0)
 while True:
  message=json.loads(ws.receive())
  if message.get('method')=='warning':(root/'warning.json').write_text(json.dumps(message));sys.exit(0)
''')
    script.chmod(0o700);return script


@pytest.mark.parametrize('mode', ['outage','retry','auth','cancel','quit','missing','transport','local_failure'])
def test_owned_supervisor(offline,tmp_path,monkeypatch,fake_codex,mode):
    m=offline;calls=[]
    monkeypatch.setenv('FIXTURE_ROOT',str(tmp_path));monkeypatch.setenv('FIXTURE_MODE',mode)
    monkeypatch.setenv('SOCKET_HELPER',str(ROOT/'libexec/t_codex_socket.py'))
    monkeypatch.setenv('COV_CORE_T_OFFLINE_FIXTURE','must not reach generated executables')
    monkeypatch.setattr(m,'RETRY_WINDOW',.1)
    real_popen=m.subprocess.Popen;processes=[]
    def popen(*a,**kw):
        kw['env']={k:v for k,v in kw.get('env',os.environ).items() if not k.startswith('COV_CORE_')}
        p=real_popen(*a,**kw);processes.append(p);return p
    monkeypatch.setattr(m.subprocess,'Popen',popen)
    def lead(argv,**kwargs):
        assert all(p.poll() is not None for p in processes)
        assert kwargs['chosen']['mode']=='local-only' and kwargs['chosen']['on_cloud_failure']=='pause'
        assert 'Fix fixture' in argv[-1] and 'untrusted' in argv[-1]
        calls.append(argv)
        if mode=='local_failure':raise ValueError('fixture local failure')
        return 0
    local={'leased_runtime':lambda chosen:None,'helper':lambda n:{'state_root':lambda:tmp_path/'state'},'catalogue':lambda **kw:{'models':[{'model':'local-fixture','provider':'lmstudio','ready':mode!='missing'}]},'lead':lead}
    monkeypatch.setattr(m,'inference',lambda:local)
    chosen={'context_tokens':32768,'model':'local-fixture','provider':'lmstudio','mode':'automatic','on_cloud_failure':'local'}
    if mode in ('transport','local_failure'):
        with pytest.raises(ValueError):m.supervise([str(fake_codex),'--no-daemon','--','task'],chosen,str(tmp_path))
    else:assert m.supervise([str(fake_codex),'--no-daemon','--','task'],chosen,str(tmp_path))==0
    assert bool(calls)==(mode in ('outage','retry','local_failure'))
    assert all(p.poll() is not None for p in processes)
    if mode!='quit':assert json.loads((tmp_path/'approval.json').read_text())=={'id':'approve','result':{'decision':'accept'}}
    root=next((tmp_path/'state').iterdir());assert root.stat().st_mode & 0o777 == 0o700
    if mode=='missing':assert json.loads((root/'status.json').read_text())['state']=='paused'
    if mode=='local_failure':assert json.loads((root/'status.json').read_text())['state']=='local_stopped'


def test_owned_detached_descendant_stops(offline,tmp_path):
    m=offline
    code='import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)'
    path=tmp_path/'pid'
    p=subprocess.Popen([sys.executable,'-c',code,str(path)],start_new_session=True,
                       env={k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_')})
    try:
        deadline=time.monotonic()+5
        while not path.exists() and time.monotonic()<deadline:time.sleep(.01)
        pid=int(path.read_text());m.stop_runtime(p)
        row=m.process_table().get(pid)
        assert not row or row[2].startswith('Z')
        assert p.poll() is not None
    finally:
        m.stop(p,True)
        if path.exists():
            try:os.kill(int(path.read_text()),9)
            except ProcessLookupError:pass


def test_shutdown_verification_fails_closed(offline,monkeypatch):
    m=offline;p=SimpleNamespace(pid=3)
    calls=[];monkeypatch.setattr(m,'stop',lambda *a,**kw:calls.append('stopped'))
    monkeypatch.setattr(m,'process_table',lambda:{3:(1,3,'R','stamp'),4:(3,4,'R','child')})
    monkeypatch.setattr(m.os,'kill',lambda *a:None)
    times=iter([0,10]);monkeypatch.setattr(m.time,'monotonic',lambda:next(times))
    with pytest.raises(ValueError,match='withheld'):m.stop_runtime(p)
    assert calls==['stopped']
    monkeypatch.setattr(m,'process_table',lambda:(_ for _ in ()).throw(OSError('ps failed')))
    with pytest.raises(OSError):m.stop_runtime(p)
    assert len(calls)==2
    m.stop_runtime(None)


def test_process_table_parsers(offline,monkeypatch):
    m=offline
    monkeypatch.setattr(m.sys,'platform','linux')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='12 1 12 S Thu Nov 5 00:00:00 2026\n'))
    assert m.process_table()[12]==(1,12,'S','Thu Nov 5 00:00:00 2026')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='bad row'))
    with pytest.raises(ValueError):m.process_table()
    class Function:
        def __init__(self,fn):self.fn=fn
        def __call__(self,*a):return self.fn(*a)
    def listing(buffer,size):
        if buffer is None:return 2
        buffer[0]=m.os.getpid();buffer[1]=123
        return 2
    def info(pid,flavor,arg,pointer,size):
        if pid==123:return 0
        b=pointer._obj;b.header[4]=1;b.header[1]=5;b.tail[1]=456;b.seconds=123;b.microseconds=45
        return size
    lib=SimpleNamespace(proc_listallpids=Function(listing),proc_pidinfo=Function(info))
    monkeypatch.setattr(m.ctypes,'CDLL',lambda *a,**kw:lib)
    monkeypatch.setattr(m.sys,'platform','darwin')
    assert m.process_table()[m.os.getpid()]==(1,456,'Z',(123,45))
    lib.proc_pidinfo=Function(lambda *a:0)
    with pytest.raises(ValueError,match='identity'):m.mac_process_table()
    lib.proc_listallpids=Function(lambda *a:0)
    with pytest.raises(ValueError,match='enumerate'):m.mac_process_table()


def test_interactive_resume_cannot_use_partial_history(offline,tmp_path):
    c=started(offline,tmp_path)
    c.client({'id':3,'method':'thread/resume','params':{}})
    c.server({'id':3,'result':{'thread':{'id':'old','cwd':str(tmp_path),'turns':[]}}})
    c.user([{'type':'text','text':'Continue'}])
    with pytest.raises(ValueError,match='history may be incomplete'):c.prompt()
