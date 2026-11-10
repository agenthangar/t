from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest
from conftest import load_script

@pytest.fixture
def apps():
    return load_script(Path(__file__).parents[1]/'libexec/t_local_apps.py','apps_test')

@pytest.mark.parametrize('provider,command',[('lmstudio','lms ps'),('ollama','ollama ps')])
def test_guides(apps,provider,command):
    guide=apps.cli_guide(provider,'http://127.0.0.1:9')
    assert guide[1]['command']==command and len(guide)==4
    assert 'queue' in guide[2]['note']
    with pytest.raises(ValueError): apps.provider_info('unknown')

@pytest.mark.parametrize('platform,provider,path',[('darwin','lmstudio','/Applications/LM Studio.app'),('darwin','ollama','/Applications/Ollama.app'),('linux','lmstudio','/usr/bin/lm-studio')])
def test_desktop_launch_is_explicit(apps,monkeypatch,platform,provider,path):
    monkeypatch.setattr(apps.sys,'platform',platform)
    monkeypatch.setattr(apps.shutil,'which',lambda _:path)
    monkeypatch.setattr(apps.Path,'exists',lambda p:str(p)==path)
    calls=[]
    monkeypatch.setattr(apps.subprocess,'run',lambda argv,**kw:calls.append(argv))
    monkeypatch.setattr(apps.subprocess,'Popen',lambda argv,**kw:calls.append(argv))
    assert apps.desktop_app(provider)['installed'] and not calls
    apps.open_desktop(provider)
    assert calls==[['/usr/bin/open','-a',path] if platform=='darwin' else [path]]

def test_missing_and_launch_failure(apps,monkeypatch):
    monkeypatch.setattr(apps.sys,'platform','darwin')
    monkeypatch.setattr(apps.Path,'exists',lambda _:False)
    with pytest.raises(ValueError,match='not found'): apps.open_desktop('ollama')
    monkeypatch.setattr(apps.Path,'exists',lambda _:True)
    def fail(*a,**k):raise subprocess.TimeoutExpired('open',15)
    monkeypatch.setattr(apps.subprocess,'run',fail)
    with pytest.raises(ValueError,match='Could not open'): apps.open_desktop('ollama')
