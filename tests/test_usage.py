"""Usage evidence, identity, and approved terminal interactions."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def usage(tmp_path, monkeypatch):
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'CODEX_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    return load_script(ROOT / 'libexec/t_usage.py', 'usage_tests')


def rollout(path, sid='one', provider='openai'):
    rows = [{'type': 'session_meta', 'payload': {'id': sid, 'cwd': '/repo', 'model_provider': provider}},
            {'type': 'turn_context', 'payload': {'model': 'first'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 10}}}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 10}}}},
            {'type': 'turn_context', 'payload': {'model': 'second'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 150, 'output_tokens': 30, 'cached_input_tokens': 20}}}}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return path


def test_cumulative_dedup_and_exact_model_attribution(usage, tmp_path):
    p = rollout(tmp_path / 'one.jsonl')
    row = usage.session(p, 'codex')
    assert row['tokens'] == {'local': None, 'cloud': 180, 'unknown': None}
    assert row['local_cloud_percent'] == [0, 100]
    assert [r['usage']['input_tokens'] for r in row['requests']] == [100, 50]
    assert [r['model'] for r in row['requests']] == ['first', 'second']
    assert row['estimated_cloud_cost_usd'] is None
    assert row['savings_usd'] is None
    assert usage.session_rows([(p, 'codex'), (p, 'codex')])[0]['tokens']['cloud'] == 180
    with p.open('a') as f:
        f.write(json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':2,'output_tokens':1}}}})+'\n')
    row=usage.session(p,'codex')
    assert row['tokens']['cloud'] == 180 and row['requests'][-1]['usage'] is None


def test_unknown_provider_and_torn_records(usage, tmp_path):
    p=rollout(tmp_path/'one.jsonl',provider='unverified')
    with p.open('a') as f:f.write('invalid\n[]\n{"torn":')
    row=usage.session(p,'codex')
    assert row['local_cloud_percent'] is None and row['tokens']['unknown']==180
    assert usage.session(tmp_path/'missing','codex') is None
    assert usage.read(tmp_path/'missing',{})=={}
    assert usage.tokens({'input_tokens':True,'output_tokens':1}) is None
    assert usage.tokens({'input_tokens':2,'output_tokens':1,'cached_input_tokens':3}) is None
    assert usage.tokens(None) is None
    assert list(usage.events(tmp_path/'missing'))==[]
    p.write_text(json.dumps({'type':'session_meta','payload':{'id':'x','source':{'subagent':{'parent':'y'}}}})+'\n')
    assert usage.session(p,'codex') is None


def test_launch_snapshot_does_not_relabel_history(usage, tmp_path, monkeypatch):
    p=rollout(tmp_path/'one.jsonl')
    workflow=usage.helper('t_workflow')
    document=workflow['defaults']()
    monkeypatch.setattr(usage,'helper',lambda name: dict(workflow, load=lambda:(document,None)))
    usage.snapshot_start('one','codex',str(tmp_path),str(p))
    metadata=usage.read(usage.state_root()/'usage/codex-one.json')
    assert metadata['launches'][0]['offset']==p.stat().st_size
    row=usage.session(p,'codex',metadata)
    assert all(r['workflow'] is None for r in row['requests'])
    metadata['launches'][0]['offset']=0
    row=usage.session(p,'codex',metadata)
    groups=usage.workflow_rows([row])
    assert groups[0]['tokens']['cloud']==180
    assert groups[0]['sessions']==['session:codex:one']
    usage.snapshot_start('../bad','codex',str(tmp_path),str(p))
    assert len(list((usage.state_root()/'usage').glob('*.json')))==1


def test_worker_requires_runtime_and_parent_evidence(usage, tmp_path):
    root=usage.state_root()/'local-worker/task';root.mkdir(parents=True)
    record={'task_id':'task','provider':'lmstudio','resolved_model':'local-model','identity_verified':True,
            'parent_session_id':'one','parent_identity_verified':True,'status':'completed'}
    (root/'result.json').write_text(json.dumps(record))
    (root/'events.jsonl').write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':70,'output_tokens':10}})+'\n')
    p=rollout(tmp_path/'one.jsonl')
    row=usage.session_rows([(p,'codex')])[0]
    assert row['tokens']['local']==80 and row['local_cloud_percent']==[31,69]
    assert 'MCP local worker: completed' in '\n'.join(usage.detail(row))
    record['parent_identity_verified']=False;(root/'result.json').write_text(json.dumps(record))
    assert usage.session_rows([(p,'codex')])[0]['tokens']['local'] is None
    record['identity_verified']=False;(root/'result.json').write_text(json.dumps(record))
    assert usage.worker_rows()==[]


def test_claude_cache_and_partial_cost(usage,tmp_path):
    p=tmp_path/'c.jsonl'
    msg={'type':'assistant','sessionId':'c','cwd':'/repo','message':{'id':'msg','model':'claude','usage':{'input_tokens':2,'output_tokens':3,'cache_read_input_tokens':5,'cache_creation_input_tokens':7}}}
    p.write_text((json.dumps(msg)+'\n')*2)
    row=usage.session(p,'claude')
    assert row['tokens']['cloud']==17 and row['measured_records']==1
    row['requests'][0].update(seconds=2,cost_usd=.2)
    summary=usage.summarize(row['requests'])
    assert summary['estimated_cloud_cost_usd']==.2
    assert summary['observed_tokens_per_second']['cloud']==1.5
    summary=usage.summarize(row['requests']+[dict(location='cloud',usage=None)])
    assert summary['estimated_cloud_cost_usd'] is None and summary['known_cloud_cost_subtotal_usd']==.2


def test_usage_command_contract(usage, t_mod, monkeypatch, capsys, tmp_path):
    row=usage.session(rollout(tmp_path/'one.jsonl'),'codex')
    monkeypatch.setattr(usage,'snapshot',lambda:[row])
    parser=t_mod.build_parser()
    for action in ('list','show'):
        words=['system','usage',action]+([row['id']] if action=='show' else [])+['--json']
        assert t_mod._command_readonly(words)
        args=parser.parse_args(t_mod._canonical_argv(words))
        assert usage.command(args)==0
        data=json.loads(capsys.readouterr().out)
        assert (data if action=='show' else data[0])['tokens']['cloud']==180
    cli=usage.helper('t_eval_cli')
    monkeypatch.setattr(usage,'helper',lambda _:dict(cli,choose=lambda *a, **kw:row['id']))
    assert usage.command(SimpleNamespace(usage_action='show',usage_id=None,json=False))==0
    assert 'Tokens local/cloud: unavailable/180' in capsys.readouterr().out
    assert usage.command(SimpleNamespace(usage_action='show',usage_id=None,json=True))==1
    assert usage.command(SimpleNamespace(usage_action='show',usage_id='bad',json=False))==1
    assert usage.command(SimpleNamespace(usage_action='list',json=False))==0
    monkeypatch.setattr(usage,'helper',lambda _:dict(cli,choose=lambda *a, **kw:None))
    assert usage.command(SimpleNamespace(usage_action='show',usage_id=None,json=False))==130
    monkeypatch.setattr(usage,'snapshot',lambda:[])
    assert usage.command(SimpleNamespace(usage_action='list',json=False))==0


def test_eval_list_show_same_picker(t_mod, tmp_path, monkeypatch):
    for name in ('HOME','XDG_CONFIG_HOME','XDG_STATE_HOME','XDG_CACHE_HOME'):
        monkeypatch.setenv(name,str(tmp_path/name))
    m=load_script(ROOT/'libexec/t_eval.py','usage_eval')
    cli=load_script(ROOT/'libexec/t_eval_cli.py','usage_eval_cli')
    monkeypatch.setattr(cli,'core',lambda:vars(m));monkeypatch.setattr(cli,'tty',lambda:True)
    run_id=m.create_run(m.defaults('fixture'))
    labels=[]
    def choose(rows,*args):
        labels.append([r['label'] for r in rows]);return run_id
    monkeypatch.setattr(cli,'choose',choose)
    for action in ('list','show'):
        assert cli.command(None,t_mod.build_parser().parse_args(['eval','runs',action]),{})==0
    assert labels[0]==labels[1] and 'graded' in labels[0][0]


@pytest.mark.parametrize('width',[32,44,80,140,220])
def test_approved_session_layout(t_mod,monkeypatch,width):
    monkeypatch.setattr(t_mod.sys.stdout,'isatty',lambda:True);monkeypatch.setattr(t_mod,'_columns',lambda:width)
    row=dict(slot='t-13',state='attached',context='active',agent='codex',model='cloud-model',summary='Fix parser #1 merged, still in progress',
             usage={'lead_model':'cloud-model','models':[{'model':'cloud-model'},{'model':'local-model'}],'local_cloud_percent':[69,31]})
    lines=t_mod._session_table_lines([row])
    assert all(t_mod._display_width(v)<=width for v in lines)
    assert lines[0].index('STATUS')<lines[0].index('SESSION')<lines[0].index('WORKING')
    assert '● ✓ ↻' in lines[1]
    assert 'UI' not in lines[0] and 'EST' not in lines[0]
    if width>=220:
        assert '69/31' in lines[1] and '★ cloud-model, local-model' in lines[1]
        assert 'Codex' not in lines[1] and '⬡' in lines[1]
    legend=t_mod._header(t_mod.Style(tty=False),'')
    for mark in ('●','○','✓','↻','▣','★','✱','⬡','◆'):assert mark in legend


def test_updated_claude_message_replaces_streaming_usage(usage,tmp_path):
    p=tmp_path/'c.jsonl'
    def message(out):
        return {'type':'assistant','sessionId':'c','message':{'id':'msg','model':'claude','usage':{'input_tokens':2,'output_tokens':out}}}
    p.write_text('\n'.join(json.dumps(message(n)) for n in (0,10,10,2))+'\n')
    row=usage.session(p,'claude')
    assert row['tokens']['cloud']==12 and row['measured_records']==1
    assert row['requests'][0]['offset']==0


def test_eval_usage_keeps_missing_cost_and_benchmark_denominator(usage,tmp_path,monkeypatch):
    m=usage.helper('t_eval'); config=m['defaults']('fixture'); ident=m['create_run'](config)
    directory=m['run_directory'](ident)
    journal=m['Journal'](ident)
    journal.event('model_request_completed',trial='a',provider='frontier',seconds=2,usage={'input_tokens':10,'output_tokens':4},cost_usd=.1)
    journal.event('model_request_completed',trial='b',provider='frontier',seconds=1,usage=None,cost_usd=None)
    journal.event('model_request_completed',trial='c',provider='local',seconds=3,usage={'input_tokens':12,'output_tokens':6},cost_usd=0)
    rows=usage.eval_rows();row=rows[0]
    assert row['tokens']['local']==18 and row['tokens']['cloud']==14
    assert row['estimated_cloud_cost_usd'] is None and row['known_cloud_cost_subtotal_usd']==.1
    assert row['observed_tokens_per_second']['cloud']==2
    assert 'graded' in '\n'.join(usage.detail(row))
    assert row['benchmark']['total']>0
    monkeypatch.setattr(usage,'session_rows',lambda:[])
    assert any(r['id']==ident for r in usage.snapshot())
    assert any(r['kind']=='workflow' and r['tokens']['local'] is None for r in usage.snapshot())


def test_missing_empty_cancel_and_corrupt_stores(usage,tmp_path,monkeypatch,capsys):
    cli=usage.helper('t_eval_cli')
    monkeypatch.setattr(usage,'snapshot',lambda:[])
    def empty(*args, **kwargs):raise cli['EmptySelection']()
    monkeypatch.setattr(usage,'helper',lambda _:dict(cli,choose=empty))
    assert usage.command(SimpleNamespace(usage_action='show',usage_id=None,json=False))==0
    p=tmp_path/'big';p.write_text('x'*20)
    monkeypatch.setattr(usage,'LIMIT',10)
    assert usage.read(p,{})=={}
    p.write_text('{broken')
    assert usage.read(p,{})=={}
    assert usage.clean('\x1b[31mtest\n')==' [31mtest '


def test_usage_wire_retains_legacy_and_rejects_invalid_json(t_mod):
    wire='sid\t/repo\tt-13\tattached\tactive\tTask\tcodex\tmodel\t-'
    assert 'usage' not in t_mod._parse_rows(wire)[0]
    for suffix in ('broken','[]'):
        assert 'usage' not in t_mod._parse_rows(wire+'\t'+suffix)[0]
    row=t_mod._parse_rows(wire+'\t'+json.dumps({'local_cloud_percent':[69,31]}))[0]
    assert row['usage']['local_cloud_percent']==[69,31]


def test_reserved_eval_runtime_never_enters_manager(tmp_path,monkeypatch):
    from contextlib import contextmanager
    local=load_script(ROOT/'libexec/t_local_inference.py','usage_reserved_runtime')
    @contextmanager
    def runtime(*args):yield ('http://fixture','alias',{'revision':'pinned'})
    monkeypatch.setattr(local,'runtime',runtime)
    monkeypatch.setattr(local,'managed_runtime',lambda *a,**kw:pytest.fail('would deadlock on reservation'))
    with local.reserved_provider_runtime({'provider':'lmstudio','model':'model','context_tokens':32768}) as result:
        assert result[2]['revision']=='pinned'
    monkeypatch.setattr(local,'helper',lambda n:{'ollama_server':runtime,'check_model':lambda *a:None})
    monkeypatch.setattr(local,'request',lambda *a:{'models':[{'name':'model','digest':'revision'}]})
    @contextmanager
    def ollama(*args):yield ('http://fixture',{})
    monkeypatch.setattr(local,'helper',lambda n:{'ollama_server':ollama,'check_model':lambda *a:None})
    chosen={'provider':'ollama','model':'model','context_tokens':32768}
    with local.reserved_provider_runtime(chosen) as result:assert result[2]['revision']=='revision'
    monkeypatch.setattr(local,'request',lambda *a:{'models':[]})
    with pytest.raises(ValueError):
        with local.reserved_provider_runtime(chosen):pass


def test_cumulative_usage_spanning_unmetered_model_switch_is_unattributed(usage,tmp_path):
    p=tmp_path/'mixed.jsonl'
    rows=[{'type':'session_meta','payload':{'id':'mixed','model_provider':'openai'}},
          {'type':'turn_context','payload':{'model':'first'}},
          {'type':'turn_context','payload':{'model':'second'}},
          {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':100,'output_tokens':10}}}}]
    p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    row=usage.session(p,'codex')
    assert row['requests'][0]['model'] is None
    assert row['tokens']['cloud']==110


def test_live_worker_status_is_prompt_free_and_expires(usage,tmp_path,monkeypatch):
    worker=load_script(ROOT/'libexec/t_local_worker.py','usage_live_worker')
    job=usage.state_root()/'local-worker/live';job.mkdir(parents=True)
    task={'task_id':'live','status':'queued','model':'selected','provider':'lmstudio','started_at':1,
          'parent_session_id':'one','parent_identity_verified':True,'identity_verified':False,'assignment':'PRIVATE'}
    worker.usage_status(job,task)
    assert 'PRIVATE' not in (job/'usage.json').read_text()
    assert json.loads((job/'usage.json').read_text())['partial_patch_available'] is None
    before=(job/'usage.json').read_bytes();worker.usage_status(job,task)
    assert (job/'usage.json').read_bytes()==before
    rows=usage.worker_rows()
    assert rows[0]['status']=='queued' and rows[0]['models']==[]
    p=rollout(tmp_path/'one.jsonl');row=usage.session_rows([(p,'codex')])[0]
    assert row['delegations'][0]['status']=='queued' and row['tokens']['local'] is None
    assert 'requested selected' in '\n'.join(usage.detail(row))
    receipt=json.loads(before);receipt['updated_at']=0;(job/'usage.json').write_text(json.dumps(receipt))
    assert usage.worker_rows()[0]['status']=='unknown (stale activity)'


def test_savings_requires_paired_successful_evidence(usage):
    config=usage.helper('t_eval')['defaults']()
    config.update(profile='quick',repetitions=1,modes=['frontier-only','required-local'])
    trials=[dict(mode=mode,task=str(i),repetition=0,status='completed',resolved=True,workflow_valid=True,cost_usd=cost)
            for mode,cost in [('frontier-only',2),('required-local',1)] for i in range(3)]
    run={'manifest':{'config':config},'trials':trials}
    assert usage.benchmark_savings(run)=={'required-local':3}
    trials[-1]['workflow_valid']=False
    assert usage.benchmark_savings(run)=={'required-local':None}
    trials[-1]['workflow_valid']=True;trials[-1]['cost_usd']=None
    assert usage.benchmark_savings(run)=={'required-local':None}
    trials[-1]['cost_usd']=1;trials[-1]['task']='other'
    assert usage.benchmark_savings(run)=={'required-local':None}


def test_discovery_includes_saved_and_archived_without_side_effects(usage,tmp_path):
    import os
    home=Path(os.environ['HOME']); codex=Path(os.environ['CODEX_HOME'])
    paths=[rollout(codex/'sessions/2026/rollout-a.jsonl','active','lmstudio'),
           rollout(codex/'archived_sessions/rollout-b.jsonl','saved','openai')]
    claude=home/'.claude/projects/repo/c.jsonl';claude.parent.mkdir(parents=True);claude.write_text('')
    assert {p for p,a in usage.session_paths()}==set(paths+[claude])
    before=set(tmp_path.rglob('*'))
    rows=usage.session_rows()
    assert {r['sid'] for r in rows}=={'active','saved'}
    assert next(r for r in rows if r['sid']=='active')['local_cloud_percent']==[100,0]
    assert set(tmp_path.rglob('*'))==before


def test_launch_snapshot_resolves_canonical_checkout_without_git_process(usage,tmp_path):
    repo=tmp_path/'repo';(repo/'.git/worktrees/task').mkdir(parents=True)
    wt=tmp_path/'task';wt.mkdir();(wt/'.git').write_text('gitdir: '+str(repo/'.git/worktrees/task'))
    (repo/'.git/worktrees/task/commondir').write_text('../..')
    for cwd,sid in ((repo,'main'),(wt,'worktree')):
        usage.snapshot_start(sid,'codex',str(cwd),'')
        row=usage.read(usage.state_root()/('usage/codex-'+sid+'.json'))
        assert row['launches'][0]['repository']==str(repo)
    assert usage.launch_records({'launches':False})==[]
    assert usage.launch_records({'launches':[{},None]})==[]


def test_malformed_metadata_never_establishes_usage(usage,tmp_path):
    p=tmp_path/'broken.jsonl'
    records=[{'type':'turn_context','payload':[1]}, {'type':'assistant','message':[1]},
             {'type':'assistant','message':{}},
             {'type':'assistant','sessionId':'a','message':{'id':'m','model':'claude','usage':{'input_tokens':2,'output_tokens':1,'cache_read_input_tokens':-1}}}]
    p.write_text(''.join(json.dumps(r)+'\n' for r in records))
    row=usage.session(p,'claude');assert row['tokens']['cloud'] is None
    assert list(usage.events(None))==[]
    row['pricing']={'source':'fixture','as_of':'2026-11-10'}
    assert 'Pricing snapshot:' in '\n'.join(usage.detail(row))


def test_verified_local_only_gateway_does_not_start_nested_manager(monkeypatch):
    local=load_script(ROOT/'libexec/t_local_inference.py','usage_lead_lease')
    monkeypatch.setattr(local,'helper',lambda n:pytest.fail('nested runtime would bypass or block the eval gateway'))
    lease={'base':'http://verified-gateway','alias':'exact-alias','revision':'verified'}
    with local.lead_client({'model':'selected'},None,lease) as client:
        assert client.base==lease['base'] and client.alias=='exact-alias' and client.error is None


def test_large_tool_records_do_not_hide_later_usage(usage,tmp_path):
    p=rollout(tmp_path/'large.jsonl')
    with p.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':'x'*(3*1024*1024)})+'\n')
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':200,'output_tokens':40,'cached_input_tokens':20}}}})+'\n')
    row=usage.session(p,'codex')
    assert row['tokens']['cloud']==240


def test_usage_read_progress_does_not_change_output(usage,monkeypatch,capsys):
    from contextlib import contextmanager
    calls=[]
    @contextmanager
    def spinner(label):calls.append(label);yield
    monkeypatch.setattr(usage,'snapshot',lambda:[])
    assert usage.command(SimpleNamespace(usage_action='list',json=True),spinner)==0
    assert json.loads(capsys.readouterr().out)==[] and calls==['Reading recorded usage']


def test_isolated_local_lead_history_and_launch_policy(usage):
    home=usage.state_root()/'local-worker/lead-fixture';home.mkdir(parents=True)
    usage.local_launch(home,str(home))
    p=rollout(home/'.codex/sessions/2026/rollout-local.jsonl','local','lmstudio')
    rows=usage.session_rows()
    assert len(rows)==1 and rows[0]['tokens']['local']==180
    assert rows[0]['tokens']['cloud'] is None and rows[0]['local_cloud_percent']==[100,0]
    assert rows[0]['requests'][0]['workflow'] is not None
    assert rows[0]['launches'][0]['offset']==0


def test_unmetered_provider_switch_does_not_invent_routing_percentage(usage,tmp_path):
    p=tmp_path/'routing.jsonl'
    rows=[{'type':'session_meta','payload':{'id':'mixed','model_provider':'openai'}},
          {'type':'turn_context','payload':{'model':'first'}},
          {'type':'turn_context','payload':{'model':'second','model_provider':'lmstudio'}},
          {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':100,'output_tokens':10}}}}]
    p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    row=usage.session(p,'codex')
    assert row['tokens']['unknown']==110 and row['local_cloud_percent'] is None


def test_worker_uses_its_own_launch_revision_after_workflow_edit(usage,tmp_path):
    root=usage.state_root()/'local-worker/task';root.mkdir(parents=True)
    record={'task_id':'task','provider':'lmstudio','resolved_model':'local-model','identity_verified':True,
            'parent_session_id':'one','parent_identity_verified':True,'status':'completed',
            'workflow_snapshot':{'workflow':'changed','revision':'new','policy':{'enabled':True}}}
    (root/'result.json').write_text(json.dumps(record))
    (root/'events.jsonl').write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':70,'output_tokens':10}})+'\n')
    p=rollout(tmp_path/'one.jsonl')
    row=usage.session_rows([(p,'codex')])[0]
    assert row['requests'][-1]['workflow']=='codex:changed:new'
    grouped=usage.workflow_rows([row])
    changed=next(r for r in grouped if r['id']=='workflow:codex:changed:new')
    assert changed['tokens']['local']==80 and changed['tokens']['cloud'] is None
    record.pop('workflow_snapshot');(root/'result.json').write_text(json.dumps(record))
    assert usage.session_rows([(p,'codex')])[0]['requests'][-1]['workflow'] is None
