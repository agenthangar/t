"""Model selection stages only verified identities; all runtimes are fake."""
from contextlib import nullcontext
import copy
import pytest
from test_local_inference import local, hardware
from test_tui import make_ui


class Menu:
    def __init__(self, choices): self.choices=iter(choices); self.views=[]; self.pages=[]; self.restored=False
    def pick(self, title, rows, **kwargs):
        self.views.append((title, rows, kwargs)); return next(self.choices)
    def page(self, title, rows, **kwargs): self.pages.append((title, rows)); return 'q'
    def restore(self): self.restored=True
    def raw(self): self.restored=False


@pytest.fixture
def rows(local, monkeypatch):
    values=[local.report(r, hardware()) for r in local.artifacts()]
    values[0].update(installed=True, ready=True, fit=True)
    values[1].update(installed=True, ready=False, verification_allowed=True)
    values[2].update(installed=False, ready=False, fit=True)
    monkeypatch.setattr(local,'catalogue',lambda *a, **kw: {'models':values})
    monkeypatch.setattr(local,'helper',lambda name: {'reservation':nullcontext})
    return values


def test_ready_selects_directly_and_compares_all_published_scores(local, rows):
    ui=Menu([rows[0]['model']])
    assert local.select_model(ui)==local.selection(rows[0])
    assert len(ui.views)==1 and not ui.pages
    tools=Menu([('tools',rows[0]['model']),'use'])
    assert local.select_model(tools)==local.selection(rows[0])
    _, entries, opts=ui.views[0]
    assert not any('benchmark' in label.lower() for _,label in entries)
    assert len(opts['columns'])==5
    assert opts['cells'][rows[0]['model']][:4]==['61.7','42.2','—','89.2']
    assert opts['cells'][rows[3]['model']][2]=='36.2'
    assert 'Configured' in opts['notes'][rows[0]['model']]


@pytest.mark.parametrize('installed', [True,False])
def test_prepare_is_explicit_and_stages_only_after_verification(local, rows, monkeypatch, installed):
    row=rows[1]; row['installed']=installed; called=[]
    def install(name, download_only, context):
        called.append((name,download_only,context));row.update(installed=True,ready=True,fit=True)
    monkeypatch.setattr(local,'install',install)
    ui=Menu([row['model'],'back','__back'])
    assert local.select_model(ui) is None and not called
    ui=Menu([row['model'],'prepare'])
    assert local.select_model(ui,context=8192)==local.selection(row)
    assert called==[(row['model'],False,8192)] and not ui.restored
    prepare=ui.views[1]
    assert dict(prepare[1])['prepare']==('Verify & use' if installed else 'Download, verify & use')
    assert ('On disk' in dict(prepare[2]['context'])['Files']) is installed
    assert ('Location' in dict(prepare[2]['context'])) is not installed


@pytest.mark.parametrize('failure', ['busy','not_ready','missing','scan_error','interrupt'])
def test_preparation_failure_does_not_select_or_retry_implicitly(local, rows, monkeypatch, failure):
    row=rows[1]; calls=[]
    def install(*args):
        calls.append(args)
        if failure=='busy':raise ValueError('Local worker busy')
        if failure=='interrupt':raise KeyboardInterrupt
        if failure=='missing':rows.remove(row)
        if failure=='scan_error':monkeypatch.setattr(local,'catalogue',lambda **kw: (_ for _ in ()).throw(OSError('Cannot read catalogue')))
    monkeypatch.setattr(local,'install',install)
    ui=Menu(['prepare','back'])
    if failure=='interrupt':
        with pytest.raises(KeyboardInterrupt):local.prepare_model(ui,row,32768)
    else:
        assert local.prepare_model(ui,row,32768) is None
        assert 'Last check' in dict(ui.views[-1][2]['context'])
    assert len(calls)==1 and not ui.restored


def test_provider_identity_sources_empty_and_unavailable(local, rows, monkeypatch):
    duplicate=dict(rows[0],provider='ollama',precision='8-bit');rows.append(duplicate)
    key='ollama::'+duplicate['model']
    ui=Menu([('gpqa',key),key])
    assert local.select_model(ui,provider='ollama')==local.selection(duplicate)
    assert ui.views[0][1][ui.views[0][2]['default']][0]==key
    assert any(duplicate['benchmarks']['gpqa']['source'] in line for line in ui.pages[0][1])
    local.benchmark_source(ui,rows[2],'gpqa');assert 'No published' in ui.pages[-1][1][0]
    row=rows[2];row.update(fit=False,verification_allowed=False,reason='Too little memory')
    ui=Menu([row['model'],'back','__back']);assert local.select_model(ui) is None
    assert dict(ui.views[1][2]['context'])['Reason']=='Too little memory'
    row.update(fit=True,verification_allowed=True,disk_fit=False)
    assert 'disk space' in local.preparation_block(row)
    row['installed']=True;assert local.preparation_block(row) is None
    rows.clear();ui=Menu(['__refresh','__back']);assert local.select_model(ui) is None
    assert 'No local models found.' in ui.views[0][2]['context']
    monkeypatch.setattr(local,'catalogue',lambda **kw: (_ for _ in ()).throw(OSError('unavailable')))
    ui=Menu(['retry','back']);assert local.select_model(ui) is None and len(ui.views)==2


def test_ollama_preparation_preserves_exact_provider(local, rows, monkeypatch):
    row=dict(rows[0],provider='ollama',ready=False,installed=False);rows[:]=[row];calls=[]
    def install(name,action,*,context):calls.append((name,action,context));row.update(ready=True,installed=True)
    monkeypatch.setattr(local,'ollama_action',install)
    assert local.prepare_model(Menu(['prepare']),row,16384)==local.selection(row)
    assert calls==[(row['model'],'install',16384)]


@pytest.mark.parametrize('width,height',[(140,40),(100,24),(80,24),(44,20),(32,12)])
def test_comparison_picker_layout_navigation_and_source_keys(local,rows,t_mod,width,height):
    ui=make_ui(t_mod,['1','q','j','enter','q','q'],width,height)
    assert local.select_model(ui) is None
    all_text='\n'.join(t_mod._ANSI_RE.sub('',line) for frame in ui.frames for line in frame)
    assert '61.7' in all_text and 'All benchmarks' not in all_text
    assert 'Benchmark source' in all_text
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line,width) for line in frame)<=height
        assert all(t_mod._vis_len(line)<=width for line in frame)
    picker_frames=[f for f in ui.frames if any('Choose a local model' in l for l in f)]
    assert all(any('▸' in l for l in f) for f in picker_frames)


def test_context_and_actions_align(t_mod):
    ui=make_ui(t_mod,['q'],100,30)
    ui.pick('Prepare',[('prepare','Verify & use'),('back','Cancel')],context=[('Model','Qwen'),('Files','On disk')])
    lines=[t_mod._ANSI_RE.sub('',line) for line in ui.frames[0]]
    assert next(l.index('Model') for l in lines if 'Model' in l)==next(l.index('Verify') for l in lines if 'Verify' in l)
    assert next(l.index('Qwen') for l in lines if 'Qwen' in l)==next(l.index('On disk') for l in lines if 'On disk' in l)
