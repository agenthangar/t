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


def test_busy_request_is_terminal(ui_module, monkeypatch):
    state = view(); state['requests'][0]['state'] = 'busy'
    monkeypatch.setattr(ui_module, 'manager', lambda: {'status': lambda: state})
    ui = UI('request', 'back', 'back')
    ui_module.requests_menu(ui)
    detail = next(page for page in ui.pages if page[0].startswith('Request '))
    assert 'cancel' in detail[2]['disabled']
    assert ui_module.label('busy') == 'Busy'


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


def test_preflight_cancel_never_executes_and_reserved_action_is_quiet(ui_module,monkeypatch):
    m=ui_module;original=m.helper;closed=[];lease=SimpleNamespace(token='test',error=None,close=lambda:closed.append(True))
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:{'requests':[]}})
    monkeypatch.setattr(m,'helper',lambda name:{'prepare':lambda *a:False} if name=='t_model_preflight' else original(name))
    assert m.run_model_action(UI(),lambda c:pytest.fail('cancelled preflight ran'),'model',preflight=True) is None
    monkeypatch.setattr(m,'helper',lambda name:{'prepare':lambda *a:lease} if name=='t_model_preflight' else original(name))
    def operation(cancel):
        assert cancel.test_token=='test'
        assert original('t_progress')['_state'].local.quiet
        return {'ready':True}
    assert m.run_model_action(UI(),operation,'model',preflight=True)['ready']
    assert closed==[True]


@pytest.mark.parametrize('failed',[False,True])
def test_background_action_shows_result_without_refresh_key(ui_module,t_mod,monkeypatch,failed):
    import threading
    from test_tui import make_ui
    m=ui_module;finish=threading.Event();done=threading.Event();begun=threading.Event()
    ui=make_ui(t_mod,['enter'])  # Only the final result page consumes a key.
    def operation(cancel):
        cancel.client_id='owned-test';begun.set()
        try:
            assert finish.wait(2)
            if failed:raise ValueError('generic HTTP error')
            return {'ready':True}
        finally:done.set()
    def status():
        assert begun.wait(2)
        return {'requests':[dict(client='owned-test',created=m.time.time(),state='timed_out' if failed else 'running',
                                  queue_seconds=300 if failed else 0,error='Queue wait expired' if failed else None)]}
    monkeypatch.setattr(m,'manager',lambda:{'status':status})
    def poll(*args):
        finish.set();assert done.wait(2)
        return [],[],[]
    monkeypatch.setattr(t_mod.select,'select',poll)
    result=m.run_model_action(ui,operation,'model')
    frames='\n'.join(line for frame in ui.frames for line in frame)
    assert ('Queue wait expired' in frames) if failed else result['ready']
    assert ('Model action failed' if failed else 'Model test complete') in frames


def test_hub_all_branches_and_command_errors(ui_module,monkeypatch,capsys):
    m=ui_module;calls=[]
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    monkeypatch.setattr(m,'requests_menu',lambda ui:calls.append('requests'))
    monkeypatch.setattr(m,'machine_menu',lambda ui:calls.append('machine'))
    monkeypatch.setattr(m,'provider_menu',lambda ui,provider:calls.append(provider))
    monkeypatch.setattr(m,'monitor_cli',lambda ui:calls.append('cli'))
    monkeypatch.setattr(m,'monitor_gui',lambda:calls.append('gui'))
    original_helper=m.helper
    monkeypatch.setattr(m,'helper',lambda name:original_helper(name) if name=='t_model_display' else {'select_model':lambda *a,**k:{'model':'model','provider':'ollama'}} if name=='t_local_inference' else {'save_settings':lambda value:calls.append(value),'settings':lambda:{'enabled':True}} if name=='t_local_worker' else {'run':lambda *a:calls.append('worker')})
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
    original_helper=ui_module.helper
    monkeypatch.setattr(ui_module,'helper',lambda name:{'settings':lambda:{'enabled':True,'model':'mlx-community/Qwen3.8-27B-8bit','provider':'lmstudio'}} if name=='t_local_worker' else original_helper(name))
    ui=make_ui(t_mod,['q'],width,height)
    ui_module.hub(ui,vars(t_mod))
    assert 'Local model' in '\n'.join(ui.frames[0])
    if (width,height)==(80,24):
        assert 'Runtime details' in '\n'.join(ui.frames[0])
        assert 'Back to t config' in '\n'.join(ui.frames[0])
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


@pytest.mark.parametrize('browser_error',[False,True])
def test_gui_monitor_always_closes_its_server(ui_module,monkeypatch,browser_error):
    m=ui_module;calls=[]
    class Server:
        server_port=12345
        def __enter__(self):return self
        def __exit__(self,*a):calls.append('closed')
        def serve_forever(self):pass
        def shutdown(self):calls.append('shutdown')
    class Thread:
        def __init__(self,**kw):self.turn=0
        def start(self):calls.append('start')
        def is_alive(self):self.turn+=1;return self.turn==1
        def join(self,**kw):calls.append('join')
    def browser(url):
        assert url.startswith('http://127.0.0.1:12345/#')
        if browser_error:raise OSError('browser unavailable')
    monkeypatch.setattr(m,'ThreadingHTTPServer',lambda *a:Server())
    monkeypatch.setattr(m.threading,'Thread',Thread)
    monkeypatch.setattr(m.webbrowser,'open',browser)
    if browser_error:
        with pytest.raises(OSError):m.monitor_gui()
    else:m.monitor_gui()
    assert calls[-3:]==['shutdown','join','closed']


def test_hub_compact_overview_details_and_return_focus(ui_module,monkeypatch):
    m=ui_module;v=view();reads=[]
    v.update(endpoints={'lmstudio':'http://127.0.0.1:1234'},models=[
        {'model':'loaded-model','provider':'lmstudio','memory_bytes':37*1024**3,'estimated':True,'owned':True}])
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:reads.append('status') or v})
    original_helper=m.helper
    config={'enabled':True,'model':'mlx-community/Qwen3.8-27B-8bit','provider':'lmstudio'}
    monkeypatch.setattr(m,'helper',lambda name:{'settings':lambda:config} if name=='t_local_worker' else original_helper(name))
    ui=UI('runtime','back');m.hub(ui,{})
    title,rows,options=ui.pages[0]
    assert title=='Local inference'
    assert rows==[('models','Local model'),('provider','Providers'),('machine','Machine settings'),
        ('worker','Install / configure worker'),('monitor','Monitor usage'),('requests','Requests'),
        ('runtime','Runtime details'),('back','Back to t config')]
    assert options['sections']=={'models':'Configure','monitor':'Inspect'}
    assert options['values']['models']=='LM Studio'
    assert options['default']==0 and len(options['context'])==2
    overview=str(options['context'])
    assert 'Qwen3.8-27B-8bit' in overview and 'running' in overview
    assert 'http' not in overview and 'GiB' not in overview and 'Fair turns' not in overview
    assert ui.pages[1][0]=='Runtime details'
    details='\n'.join(ui.pages[1][1])
    assert 'http://127.0.0.1:1234' in details and 'Fair turns' in details
    assert '37.0 GiB estimated' in details and config['model'] in details
    assert ui.pages[2][2]['default']==6
    assert reads==['status','status']


@pytest.mark.parametrize('manager_failure',[False,True])
def test_hub_unavailable_keeps_settings_and_errors_reachable(ui_module,monkeypatch,manager_failure):
    m=ui_module;calls=[]
    def status():
        if manager_failure:raise OSError('Manager socket unavailable')
        return {'manager':'unavailable','error':'Manager socket unavailable'}
    monkeypatch.setattr(m,'manager',lambda:{'status':status})
    original_helper=m.helper
    monkeypatch.setattr(m,'helper',lambda name:{'settings':lambda:{'enabled':False,'model':'saved-model'}} if name=='t_local_worker' else original_helper(name))
    monkeypatch.setattr(m,'machine_menu',lambda ui:calls.append('machine'))
    monkeypatch.setattr(m,'provider_menu',lambda ui,provider:calls.append(provider))
    ui=UI('machine','provider','ollama','runtime','back');m.hub(ui,{})
    assert calls==['machine','ollama']
    assert 'saved-model' in str(ui.pages[0][2]['context'])
    assert 'unavailable' in str(ui.pages[0][2]['context'])
    assert any(p[0]=='Runtime details' and 'Manager socket unavailable' in p[1] for p in ui.pages)


@pytest.mark.parametrize('review',['back','save'])
def test_hub_model_selection_review_preserves_worker_settings(ui_module,monkeypatch,review):
    m=ui_module;saved=[];selections=[];selected={'model':'new-model','provider':'lmstudio'}
    config={'enabled':False,'model':'old-model','provider':'ollama','context_tokens':16384}
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    def helper(name):
        if name=='t_local_worker':return {'settings':lambda:config,'save_settings':lambda value:saved.append(value)}
        assert name=='t_local_inference'
        return {'select_model':lambda *a,**kw:selections.append(kw) or selected}
    monkeypatch.setattr(m,'helper',helper)
    ui=UI('models',review,'back');m.hub(ui,{})
    assert saved==([dict(config,**selected)] if review=='save' else [])
    assert selections == [{'use_label':'Select for local worker','current':'old-model','provider':'ollama','context':16384}]
    assert ui.pages[-1][2]['default']==0



def test_hub_invalid_worker_settings_exposes_error_without_implying_provider(ui_module,monkeypatch):
    m=ui_module
    monkeypatch.setattr(m,'manager',lambda:{'status':lambda:view()})
    original_helper=m.helper
    def settings():raise ValueError('invalid local worker configuration')
    monkeypatch.setattr(m,'helper',lambda name:{'settings':settings} if name=='t_local_worker' else original_helper(name))
    ui=UI('runtime','back');m.hub(ui,{})
    assert ui.pages[0][2]['values']['models']=='Unavailable'
    assert 'settings unavailable' in str(ui.pages[0][2]['context'])
    assert 'invalid local worker configuration' in ui.pages[1][1]
