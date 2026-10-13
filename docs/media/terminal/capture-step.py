#!/usr/bin/env python3
"""Capture an actual command in a prepared disposable fixture.
Use a private output directory: this writes unredacted PTY output.
--state has cwd, root (t source), and isolated env. The operator prepares a real
agent task and verifies one-owner transfer and desktop delivery separately.
"""
import argparse,json,pathlib,os,pty,fcntl,termios,struct,subprocess,time,select,codecs
p=argparse.ArgumentParser();p.add_argument('name');p.add_argument('command');p.add_argument('--detach',type=int,default=0);p.add_argument('--select-after',type=int,default=0,help='Press Enter once after this delay to select the observed first picker row');p.add_argument('--timeout',type=int,default=90);p.add_argument('--state',required=True,type=pathlib.Path);p.add_argument('--output',required=True,type=pathlib.Path);a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=True)
s=json.loads(a.state.read_text());env={**os.environ,**s['env']};m,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,120,0,0))
import shlex
script='source '+shlex.quote(s['root']+'/t.plugin.zsh')+'\nprint -r -- '+shlex.quote('$ '+a.command)+'\nsleep 2\n'+a.command+'\nresult=$?\nsleep 3\nexit $result\n'
proc=subprocess.Popen(['zsh','-fc',script],cwd=s['cwd'],env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave)
start=time.monotonic();ev=[];d=codecs.getincrementaldecoder('utf-8')('replace');detached=False;selected=False
while time.monotonic()-start<a.timeout:
 ready,_,_=select.select([m],[],[],.1)
 if ready:
  try:data=os.read(m,65536)
  except OSError:break
  if not data:break
  ev.append([round(time.monotonic()-start,3),'o',d.decode(data)])
 if a.select_after and time.monotonic()-start>a.select_after and not selected:os.write(m,b'\r');selected=True
 if a.detach and time.monotonic()-start>a.detach and not detached:os.write(m,b'\x02d');detached=True
 if proc.poll() is not None:break
if proc.poll() is None:proc.terminate()
proc.wait(timeout=5);os.close(m)
(a.output/('flow-'+a.name+'.cast')).write_text('\n'.join(json.dumps(x) for x in [{'version':2,'width':120,'height':30,'title':a.command},*ev])+'\n')
print(a.name,'exit',proc.returncode,'seconds',round(time.monotonic()-start,1));print('Raw private capture saved. Redact and verify it before adding repository media.')

raise SystemExit(proc.returncode)
