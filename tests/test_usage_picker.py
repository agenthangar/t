"""Actual shared fzf usage browser, in an isolated terminal; never native windows."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.fzf_compat
@pytest.mark.parametrize('width,height', [(110,28),(80,24),(44,20),(32,12),(80,29),(110,34),(110,35),(80,40),(80,41),(110,44)])
def test_usage_picker_session_preview_and_resize(width,height):
    if not shutil.which('tmux') or not shutil.which('fzf'):
        pytest.skip('requires tmux and fzf')
    with tempfile.TemporaryDirectory(prefix='t-usage-ui-',dir='/tmp') as folder:
        home=Path(folder);socket=str(home/'s')
        env={k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_')}
        env.update(HOME=folder,XDG_STATE_HOME=folder,XDG_CONFIG_HOME=folder,XDG_CACHE_HOME=folder,
                   CODEX_HOME=folder,TMUX='',T_HOME=str(ROOT),TERM='xterm-256color',SHELL='/bin/sh',
                   FZF_DEFAULT_OPTS='',FZF_DEFAULT_OPTS_FILE='/dev/null')
        script=home/'run.py'
        script.write_text('''import runpy, pathlib, os
m=runpy.run_path(%r)
u=runpy.run_path(%r)
row=dict(id='session:codex:sample',kind='session',sid='sample',agent='codex',cwd='/sample',label='t-13 · Parser fix',lead_model='cloud',models=[dict(model='cloud')],tokens=dict(local=None,cloud=180,unknown=None),local_cloud_percent=None,requests=[],launches=[],measured_records=0,coverage='Observed only',observed_tokens_per_second=dict(local=None,cloud=None),estimated_cloud_cost_usd=None,known_cloud_cost_subtotal_usd=None,session_display=dict(slot='t-13',summary='Parser fix',state='attached',context='active'))
rows,header=m['picker_rows']([row],%r)
result=u['choose'](rows,'Usage · this machine\\n'+header,'show',prompt='usage',preview_script=%r,preview_window='down:50%%:wrap')
output=pathlib.Path(os.environ['HOME'],'result')
temporary=output.with_suffix('.tmp')
temporary.write_text(str(result))
temporary.replace(output)
''' % (str(ROOT/'libexec/t_usage.py'),str(ROOT/'libexec/t_eval_cli.py'),width-8,str(ROOT/'libexec/t_usage.py')))
        def tmux(*args):
            return subprocess.run(['tmux','-S',socket,*args],env=env,capture_output=True,text=True)
        def wait(text):
            end=time.monotonic()+8
            while time.monotonic()<end:
                frame=tmux('capture-pane','-p').stdout
                if text in frame:return frame
                time.sleep(.05)
            pytest.fail(f'Missing {text}:\n{frame}')
        try:
            tmux('-f','/dev/null','new-session','-d','-x',str(width),'-y',str(height),
                 shlex.join([sys.executable,str(script)])+'; sleep 30')
            frame=wait('SESSION · t-13')
            assert '▸' in frame and ('Esc' in frame or '⎋' in frame) and ('Return' in frame or 'Enter' in frame or '↩' in frame),frame
            assert '0/100' not in frame
            evidence=Path('artifacts/design');evidence.mkdir(parents=True,exist_ok=True)
            (evidence/f'usage-{width}x{height}.txt').write_text(frame)
            tmux('send-keys','-l','Parser');wait('usage> Parser');wait('1/1')
            tmux('resize-window','-x','110','-y','28')
            assert 'unrecorded' in wait('unrecorded')
            tmux('send-keys','Enter')
            end=time.monotonic()+5
            while not (home/'result').exists() and time.monotonic()<end:time.sleep(.05)
            assert (home/'result').read_text()=='session:codex:sample'
        finally:
            tmux('kill-server')
