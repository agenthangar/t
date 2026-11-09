import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from conftest import load_script

@pytest.fixture
def ui_module():
    return load_script(Path(__file__).parents[1]/'libexec/t_local_runtime_ui.py','runtime_ui_test')

class UI:
    def __init__(self,*choices):self.choices=list(choices);self.pages=[]
    def pick(self,title,rows,**kw):
        self.pages.append((title,rows,kw));return self.choices.pop(0) if self.choices else 'back'
    def page(self,*args,**kwargs):self.pages.append(args)
    def raw(self):pass
    def restore(self):pass
    def cooked_input(self,*args,**kwargs):return 'http://127.0.0.1:9999'

def view():
    return {'manager':'running','models':[],'requests':[{'id':'request','model':'model','provider':'lmstudio','kind':'worker','state':'queued','queue_seconds':2,'execution_seconds':0}],'settings':{'memory_percent':80,'idle_seconds':300,'providers':{'lmstudio':{'mode':'headless','url':'http://127.0.0.1:1234'},'ollama':{'mode':'headless','url':'http://127.0.0.1:11434'}}}}

def test_runtime_commands(ui_module,monkeypatch,capsys,t_mod):
    calls=[];v=view()
    monkeypatch.setattr(ui_module,'manager',lambda:{'status':lambda:v,'rpc':lambda *a,**k:calls.append((a,k)) or {'state':'cancelled'}})
    for resource,verb in [('runtime','status'),('requests','list'),('requests','show')]:
        args=SimpleNamespace(models_action=resource,runtime_action=verb,json=True,request_id='request')
        assert ui_module.command(args,{'_RailUI':UI})==0
        assert json.loads(capsys.readouterr().out)
    assert not calls
    for words in [('runtime','status'),('runtime','monitor'),('runtime','edit'),('requests','list'),('requests','show','request'),('requests','cancel','request')]:
        assert t_mod.build_parser().parse_args(['config','models',*words]).models_action in ('runtime','requests')

def test_runtime_mutations(ui_module,monkeypatch):
    v=view();calls=[]
    monkeypatch.setattr(ui_module,'manager',lambda:{'status':lambda:v,'rpc':lambda *a,**k:calls.append((a,k)) or {'state':'cancelled'}})
    assert ui_module.cancel_request('request')['state']=='cancelled'
    v['requests'][0]['state']='running'
    with pytest.raises(ValueError,match='terminal'):ui_module.cancel_request('request')
    assert ui_module.cancel_request('request',UI('keep'))['state']=='running'
    assert ui_module.cancel_request('request',UI('cancel'))['state']=='cancelled'
    with pytest.raises(ValueError):ui_module.cancel_request('missing')

def test_runtime_monitor_modes(ui_module,monkeypatch):
    called=[]
    monkeypatch.setattr(ui_module.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(ui_module.sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(ui_module,'monitor_cli',lambda ui:called.append('cli'))
    monkeypatch.setattr(ui_module,'monitor_gui',lambda:called.append('gui'))
    for mode in ('cli','gui'):
        assert ui_module.command(SimpleNamespace(models_action='runtime',runtime_action='monitor',view=mode),{'_RailUI':UI})==0
    assert called==['cli','gui']

def test_machine_settings_review_and_discard(ui_module,monkeypatch):
    config=view()['settings'];saved=[]
    monkeypatch.setattr(ui_module,'manager',lambda:{'settings':lambda:json.loads(json.dumps(config))})
    monkeypatch.setattr(ui_module,'save_machine',lambda value:saved.append(value))
    ui_module.machine_menu(UI('memory','70','idle','60','save','save','back'))
    assert saved[0]['memory_percent']==70 and saved[0]['idle_seconds']==60
    ui_module.machine_menu(UI('memory','60','discard','back'))
    assert len(saved)==1


def test_requests_navigation_and_errors(ui_module,monkeypatch):
    m=ui_module;v=view();calls=[]
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:v,'rpc':lambda *a,**k:calls.append(a)})
    m.requests_menu(UI('refresh','request','refresh','cancel','back','back'))
    assert calls==[('cancel',)]
    monkeypatch.setattr(m,'cancel_request',lambda *a:(_ for _ in ()).throw(ValueError('unavailable')))
    ui=UI('request','cancel','back','back');m.requests_menu(ui)
    assert any(x[0]=='Cancellation unavailable' for x in ui.pages)
    v['requests']=[];m.requests_menu(UI('back'))
    v.update(error='unsafe\x1btext',endpoints={'lmstudio':'http://127.0.0.1:1234'},models=[{'model':'x','provider':'lmstudio','memory_bytes':100,'owned':True,'estimated':True},{'model':'y','provider':'ollama','memory_bytes':None,'owned':False}])
    assert 'unsafe text' in '\n'.join(m.status_lines(v))
    assert 'memory unknown' in '\n'.join(m.monitor_lines(v))


def test_settings_save_stopped_live_stale_and_provider(ui_module,monkeypatch,tmp_path):
    m=ui_module;calls=[];v=view()
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:v,'settings':lambda value=None:value or view()['settings'],
        'rpc':lambda *a,**k:calls.append(a),'root':lambda:tmp_path,'private_directory':lambda p:calls.append(p),
        'write_json':lambda *a:calls.append(a)})
    m.save_machine(v['settings']);assert calls[-1]==('settings',)
    v['manager']='stale'
    with pytest.raises(ValueError,match='stale'):m.save_machine(v['settings'])
    v['manager']='stopped';m.save_machine(v['settings']);assert calls[-1][0]==tmp_path/'settings.json'
    m.machine_menu(UI('mode','desktop','url','save','save','back'),'ollama')
    assert calls[-1][1]['providers']['ollama']['url'].endswith(':9999')
    m.machine_menu(UI('memory','60','back','keep','back','discard'))
    monkeypatch.setattr(m,'save_machine',lambda value:(_ for _ in ()).throw(ValueError('active sessions')))
    ui=UI('memory','60','save','save','discard','back');m.machine_menu(ui)
    assert any(x[0]=='Settings not saved' for x in ui.pages)


def test_provider_help_launch_failure_and_clipboard(ui_module,monkeypatch):
    m=ui_module;calls=[]
    apps=m.helper('t_local_apps')
    apps['open_desktop']=lambda name:calls.append(('open',name))
    monkeypatch.setattr(m,'helper',lambda name:apps)
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    monkeypatch.setattr(m.webbrowser,'open',lambda url:calls.append(('url',url)))
    monkeypatch.setattr(m,'copy_command',lambda text:calls.append(('copy',text)))
    monkeypatch.setattr(m,'machine_menu',lambda *a:calls.append(('settings',a[-1])))
    m.provider_menu(UI('runtime','open','guide','0','copy','docs','back','guide','back','back'),'ollama')
    assert ('copy','ollama list') in calls and ('settings','ollama') in calls
    apps['open_desktop']=lambda name:(_ for _ in ()).throw(ValueError('missing'))
    ui=UI('open','help','open','retry','back');m.provider_menu(ui,'lmstudio')
    assert any(x[0]=='Could not open app' for x in ui.pages)


def test_copy_command_platforms(ui_module,monkeypatch):
    import shutil
    m=ui_module;calls=[]
    monkeypatch.setattr(m.subprocess,'run',lambda command,**kw:calls.append((command,kw)))
    for platform,expected in [('darwin','pbcopy'),('win32','clip'),('linux','wl-copy')]:
        monkeypatch.setattr(m.sys,'platform',platform)
        monkeypatch.setattr(shutil,'which',lambda name:'/fake/'+name)
        m.copy_command('ollama ps');assert calls[-1][0][0]==expected
        assert calls[-1][1]['input']=='ollama ps'
    monkeypatch.setattr(shutil,'which',lambda name:'/fake/xclip' if name=='xclip' else None)
    m.copy_command('x');assert calls[-1][0][0]=='xclip'
    monkeypatch.setattr(shutil,'which',lambda name:None)
    with pytest.raises(ValueError):m.copy_command('x')


def test_monitor_http_auth_and_readonly(ui_module,monkeypatch):
    import http.client
    import threading
    m=ui_module;reads=[]
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:reads.append(True) or view()})
    with m.ThreadingHTTPServer(('127.0.0.1',0),m.MonitorHandler) as server:
        server.token='secret';thread=threading.Thread(target=server.serve_forever);thread.start()
        def call(path,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('GET',path,headers=headers or {});response=conn.getresponse()
            result=(response.status,response.read(),dict(response.getheaders()));conn.close();return result
        try:
            assert call('/')[0]==200 and not reads
            assert call('/status')[0]==403 and not reads
            assert call('/status',{'Authorization':'Bearer secret','Origin':'https://hostile.test'})[0]==403
            assert call('/',{'Host':'attacker.test'})[0]==403
            code,body,headers=call('/status',{'Authorization':'Bearer secret'})
            assert code==200 and json.loads(body)['manager']=='running'
            assert headers['Cache-Control']=='no-store' and headers['X-Frame-Options']=='DENY'
            assert len(reads)==1
        finally:server.shutdown();thread.join()


def test_actions_success_failure_cancel_and_monitor(ui_module,monkeypatch):
    import threading
    m=ui_module
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    assert m.run_model_action(UI(),lambda cancel:{'ready':True},'model')['ready']
    assert m.run_model_action(UI(),lambda cancel:{'median':20},'model')['median']==20
    assert m.run_model_action(UI(),lambda cancel:{},'model')=={}
    def fail(cancel):raise ValueError('failed test')
    assert m.run_model_action(UI(),fail,'model') is None
    def cancellable(cancel):cancel.wait(2);raise ValueError('cancelled')
    ui=UI('cancel');assert m.run_model_action(ui,cancellable,'model') is None
    class Monitor(UI):
        def cols(self):return 32
        def clip(self,text,width):return text[:width]
        def frame(self,*a):self.pages.append(a)
        def read_key(self):return 'q'
    monkeypatch.setattr(m.select,'select',lambda *a:([m.sys.stdin],[],[]))
    ui=Monitor();m.monitor_cli(ui);assert ui.pages[0][0]=='Local model usage'


def test_hub_all_branches_and_command_errors(ui_module,monkeypatch,capsys):
    m=ui_module;calls=[]
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    monkeypatch.setattr(m,'requests_menu',lambda ui:calls.append('requests'))
    monkeypatch.setattr(m,'machine_menu',lambda ui:calls.append('machine'))
    monkeypatch.setattr(m,'provider_menu',lambda ui,provider:calls.append(provider))
    monkeypatch.setattr(m,'monitor_cli',lambda ui:calls.append('cli'))
    monkeypatch.setattr(m,'monitor_gui',lambda:calls.append('gui'))
    monkeypatch.setattr(m,'helper',lambda name:{'select_model':lambda *a,**k:{'model':'model','provider':'ollama'}} if name=='t_local_inference' else {'save_settings':lambda value:calls.append(value),'settings':lambda:{'enabled':True}} if name=='t_local_worker' else {'run':lambda *a:calls.append('worker')})
    m.hub(UI('requests','models','save','machine','provider','ollama','worker','monitor','cli','monitor','gui','monitor','lmstudio','back'),{})
    assert 'worker' in calls and 'gui' in calls and {'enabled':True,'model':'model','provider':'ollama'} in calls
    monkeypatch.setattr(m.sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(m,'cancel_request',lambda *a,**k:{'state':'cancelled'})
    assert m.command(SimpleNamespace(models_action='requests',runtime_action='cancel',request_id='x'),{})==0
    with pytest.raises(ValueError,match='terminal'):m.command(SimpleNamespace(models_action='runtime',runtime_action='edit'),{})
    with pytest.raises(ValueError,match='Unknown'):m.command(SimpleNamespace(models_action='requests',runtime_action='show',request_id='bad'),{})
    for resource,action in [('runtime','status'),('requests','list'),('requests','show')]:
        assert m.command(SimpleNamespace(models_action=resource,runtime_action=action,request_id='request',json=False),{})==0


@pytest.mark.parametrize('width,height',[(80,24),(44,20),(32,12)])
def test_hub_and_model_test_fit_terminal(ui_module,t_mod,monkeypatch,width,height):
    from test_tui import make_ui
    monkeypatch.setattr(ui_module,'manager',lambda:{'status':lambda:view()})
    ui=make_ui(t_mod,['q'],width,height)
    ui_module.hub(ui,vars(t_mod))
    assert 'Requests' in '\n'.join(ui.frames[0])
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line,width) for line in frame)<=height
    ui=make_ui(t_mod,['q'],width,height)
    monkeypatch.setattr(ui_module.select,'select',lambda *a:([ui_module.sys.stdin],[],[]))
    ui_module.monitor_cli(ui)
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line,width) for line in frame)<=height


def test_machine_reset_stays_staged(ui_module,monkeypatch):
    m=ui_module;defaults=view()['settings'];saved=[]
    modified=json.loads(json.dumps(defaults));modified.update(memory_percent=60,idle_seconds=60)
    modified['providers']['ollama']['mode']='desktop'
    monkeypatch.setattr(m,'manager',lambda:{'settings':lambda value=None:json.loads(json.dumps(defaults if value=={} else modified))})
    monkeypatch.setattr(m,'save_machine',lambda value:saved.append(json.loads(json.dumps(value))))
    m.machine_menu(UI('reset','save','save','back'))
    assert saved[-1]['memory_percent']==80 and saved[-1]['providers']['ollama']['mode']=='desktop'
    m.machine_menu(UI('reset','save','save','back'),'ollama')
    assert saved[-1]['providers']['ollama']['mode']=='headless' and saved[-1]['memory_percent']==60


def test_ctrl_c_cancels_background_model_action(ui_module,monkeypatch):
    import threading
    m=ui_module;cancelled=threading.Event()
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    class Interrupt(UI):
        def pick(self,*a,**kw):raise KeyboardInterrupt
    def operation(cancel):
        if cancel.wait(2):cancelled.set()
        raise ValueError('cancelled')
    with pytest.raises(KeyboardInterrupt):m.run_model_action(Interrupt(),operation,'model')
    assert cancelled.is_set()
