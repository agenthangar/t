"""Model tables retain comparison evidence without invoking any runtime."""
from pathlib import Path
import runpy

import pytest
from test_tui import make_ui


@pytest.fixture
def display():
    return runpy.run_path(str(Path(__file__).parents[1] / 'libexec/t_model_display.py'))


def records(display):
    return display['local_models']([
        dict(model='org/model-long-name-8bit', bytes=29500000000, provider='lmstudio', fit=True, ready=True,
             benchmarks={'suite': dict(score=61.7, version='v2', kind='base model')}),
        dict(model='org/模型-large', fit=False), dict(model='unknown', fit=None, benchmark=None)], 'suite', 'org/model-long-name-8bit')


@pytest.mark.parametrize('columns', [24, 36, 72, 92, 120])
def test_columns_fit_and_preserve_score_and_states(display, columns):
    models = records(display)
    header, rows = display['table'](models, columns)
    assert all(display['width'](line) <= columns for line in [header, *rows.values()])
    assert all('\n' not in row for row in rows.values())
    assert '61.7' in rows[models[0]['key']] and '★' in rows[models[0]['key']]
    assert '✕' in rows[models[1]['key']] and '○' in rows[models[1]['key']]
    assert '?' in rows['unknown'] and '—' in rows['unknown']
    assert display['width'](rows[models[0]['key']]) == display['width'](rows[models[1]['key']])
    assert 'v2 · base model' in display['legend'](models, 'suite')
    assert len(display['lines'](models, 'suite', columns)) > 4


def test_native_capabilities_missing_evidence_and_safe_text(display):
    models = display['native_models']([('', 'Default'), ('a','a'), ('b','b'), ('c','c'), ('__custom__','Custom')],
        [dict(id='a', description='Reasoning\nmodel'), dict(id='b', efforts=['low','high'])], 'codex', 'a')
    assert len(models) == 3 and models[0]['selected']
    assert 'low/high' in models[1]['description']
    assert 'unavailable' in models[2]['description']
    header, rows = display['table'](models, 72)
    assert 'DETAILS' in header and all(display['width'](r)<=72 for r in rows.values())
    assert display['legend'](models) == ['★ selected · — not applicable / no sourced benchmark']
    assert display['clip']('ab界c', 4) == 'ab…'
    assert display['width']('e\u0301') == 1
    assert display['clean']('x\x1by\n') == 'xy'
    assert display['table']([], 24)[1] == {}


@pytest.mark.parametrize('columns,height', [(100,44),(80,24),(44,20),(32,12)])
def test_shared_picker_one_row_focus_and_actions(display, t_mod, columns, height):
    models = records(display)
    rows = [('', 'Use default'), *[(m['key'],m['model']) for m in models], ('back','Back')]
    for focus in range(len(rows)):
        ui = make_ui(t_mod, ['enter'], columns, height)
        assert ui.pick('Local model', rows, default=focus, model_table=models, benchmark_suite='suite') == rows[focus][0]
        plain = [t_mod._ANSI_RE.sub('', line) for line in ui.frames[-1]]
        assert all(t_mod._display_width(line)<=columns for line in plain)
        assert sum('▸' in line for line in plain) == 1


def test_extreme_width_and_monitor_evidence(display):
    header, rows=display['table'](records(display),12)
    assert all(display['width'](x)<=12 for x in [header,*rows.values()])
    benchmarks=display['benchmark_summaries']()
    assert 'swe-bench-pro 61.7' in benchmarks['mlx-community/Qwen3.8-27B-8bit']
    assert benchmarks['mlx-community/Qwen3.5-122B-A10B-4bit']=='Benchmark unavailable'
