"""Synthetic retained evidence: no providers, real sessions or configuration."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]
ID = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def inspection(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    m = load_script(ROOT / 'libexec/t_run_inspect.py', 'run_inspect_test')
    folder = tmp_path / 't/runners/jobs' / ID
    (folder / 'local').mkdir(parents=True)
    receipt = dict(id=ID, status='failed', exit_code=1, snapshot={'id':'snapshot', 'source_head':'abc'},
                   targets={'local': dict(type='local', status='failed', exit_code=1, snapshot_id='snapshot',
                                          result={'log_truncated':False, 'source_head':'abc'})})
    (folder / 'receipt.json').write_text(json.dumps(receipt))
    return m, folder, receipt


def test_large_failing_log_keeps_status_failure_lines_and_retrieval(inspection):
    m, folder, _ = inspection
    output = 'ordinary progress\n' * 30000 + '___ test_example ___\n> assert actual == expected\nE AssertionError: mismatch\n\nFAILED test_sample.py::test_example\n=== 1 failed, 92 passed in 2s ===\n'
    (folder / 'local/output.log').write_text(output)
    result = m.inspect_run(ID)
    target = result['targets'][0]
    assert result['status'] == 'failed' and result['exit_code'] == 1
    assert target['storage_content_lost'] is False and target['response_content_omitted']
    assert any(e['line'] == 30003 and 'AssertionError' in e['text'] for e in target['evidence'])
    assert 'logs ' + ID in target['retrieval']
    rendered = m.render(result)
    assert len(rendered) < 8000 and rendered.index('Omitted') < rendered.index('Evidence ·')
    assert (folder / 'local/output.log').read_text() == output


@pytest.mark.parametrize('status,log_state', [('running','pending'), ('queued','pending'), ('cancelled','missing'), ('unknown','missing'), ('succeeded','missing')])
def test_missing_log_preserves_execution_state(inspection, status, log_state):
    m, folder, receipt = inspection
    receipt['status'] = receipt['targets']['local']['status'] = status
    receipt['targets']['local'].pop('result')
    (folder / 'receipt.json').write_text(json.dumps(receipt))
    result = m.inspect_run(ID)
    target = result['targets'][0]
    assert result['status'] == status and target['log_state'] == log_state
    assert target['storage_content_lost'] is None
    assert 'no outcome inferred' in m.render(result)


def test_empty_unrecognized_clipped_and_storage_loss(inspection):
    m, folder, receipt = inspection
    log = folder / 'local/output.log'
    log.write_text('')
    assert m.inspect_run(ID)['targets'][0]['log_state'] == 'empty'
    log.write_text('custom checker output\n' + 'long' * 5000)
    receipt['targets']['local']['result']['log_truncated'] = True
    (folder / 'receipt.json').write_text(json.dumps(receipt))
    target = m.inspect_run(ID)['targets'][0]
    assert not target['recognized'] and target['storage_content_lost']
    assert target['response_content_omitted'] and target['evidence'][-1]['shortened']
    assert target['evidence'][0]['kind'] == 'unrecognized output'
    assert m.extract('OK\n')['response_content_omitted'] is False
    assert m.extract('TOTAL    100  0  100%\n')['evidence'][0]['kind'] == 'summary'
    many = 'TOTAL    100  0  100%\n' + 'FAILED sample.py::test_case\n' * 20 + '20 failed in 2s\n'
    assert any('TOTAL' in e['text'] for e in m.extract(many)['evidence'])
    assert m.extract('a\nb', 0)['evidence'] == []


def test_scan_and_unavailable_log(inspection, monkeypatch):
    m, folder, _ = inspection
    log = folder / 'local/output.log'
    log.write_bytes(b'hello\n' * 20 + b'\xff')
    monkeypatch.setattr(m, 'SCAN_LIMIT', 24)
    target = m.inspect_run(ID)['targets'][0]
    assert target['scan_limited'] and target['response_content_omitted']
    assert 'Scan limited' in m.render(m.inspect_run(ID))
    log.unlink()
    log.symlink_to(folder / 'receipt.json')
    assert m.inspect_run(ID)['targets'][0]['log_state'] == 'unavailable'
    assert 'Evidence ·' in m.render(m.inspect_run(ID))
    log.unlink()
    log.mkdir()
    assert m.inspect_run(ID)['targets'][0]['log_state'] == 'unavailable'
    log.rmdir()
    os.mkfifo(log)
    assert m.inspect_run(ID)['targets'][0]['log_state'] == 'unavailable'
    log.unlink()
    (folder / 'local').rmdir()
    (folder / 'local').symlink_to(folder, target_is_directory=True)
    assert m.inspect_run(ID)['targets'][0]['log_state'] == 'unavailable'


def test_invalid_and_missing_receipts(inspection):
    m, folder, receipt = inspection
    for value in ('bad', '../bad'):
        with pytest.raises(ValueError):
            m.inspect_run(value)
    for target in ('../bad', 'missing'):
        with pytest.raises(ValueError):
            m.inspect_run(ID, target=target)
    receipt['targets']['../bad'] = receipt['targets'].pop('local')
    (folder / 'receipt.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        m.inspect_run(ID)
    (folder / 'receipt.json').unlink()
    result = m.inspect_run(ID)
    assert not result['available'] and result['status'] == 'unavailable'
    assert 'No outcome inferred' in m.render(result)
    (folder / 'receipt.json').write_text('[1]')
    assert m.inspect_run(ID)['status'] == 'unavailable'


def test_target_selection_and_controls(inspection):
    m, folder, receipt = inspection
    receipt['targets']['second'] = copy.deepcopy(receipt['targets']['local'])
    (folder / 'receipt.json').write_text(json.dumps(receipt))
    (folder / 'local/output.log').write_text('\x1b[31mERROR failure\x1b[0m\x00\n')
    assert len(m.inspect_run(ID)['targets']) == 2
    result = m.inspect_run(ID, 'local')
    assert len(result['targets']) == 1
    assert '\x1b' not in m.render(result) and '\x00' not in m.render(result)


def test_cli_contract(t_mod, inspection, capsys):
    _, folder, _ = inspection
    parser = t_mod.build_parser()
    for prefix in (['runners', 'runs'], ['config', 'workflow', 'runs']):
        words = prefix + ['inspect', ID, '--target', 'local', '--json']
        args = parser.parse_args(words)
        assert args.run_action == 'inspect' and args.runner_target == 'local'
        assert t_mod._command_readonly(words, terminal=True)
    assert t_mod._command_readonly(['remote','runners','runs','inspect',ID], terminal=True)
    args = parser.parse_args(['runners', 'runs', 'inspect', ID, '--json'])
    assert t_mod._cmd_workflow_runs(SimpleNamespace(), args) == 0
    assert json.loads(capsys.readouterr().out)['targets'][0]['log_state'] == 'missing'
    args.json = False
    assert t_mod._cmd_workflow_runs(SimpleNamespace(), args) == 0
    assert 'Full retained log' in capsys.readouterr().out
    (folder / 'receipt.json').unlink()
    args.json = True
    assert t_mod._cmd_workflow_runs(SimpleNamespace(), args) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'unavailable'
    with pytest.raises(SystemExit):
        parser.parse_args(['runners','runs','inspect'])
    assert 'inspect ID' in t_mod._group_help('runners runs')


@pytest.mark.parametrize('tool', ['codex','claude','cursor'])
def test_guidance_scope_without_setting_changes(tool):
    w = load_script(ROOT / 'libexec/t_workflow.py', 'workflow_context_use_test')
    doc = w.tool_defaults(tool)
    before = copy.deepcopy(doc)
    text = w.render(doc, tool)
    assert 'roughly 2,000 tokens' in text and 'all project-required rendered-state checks' in text
    assert 'only when the user invokes' in text and 'runs inspect ID' in text
    assert doc == before
    assert 'roughly 2,000' not in w.fallback(tool)
    doc['workflows'][doc['default']]['enabled'] = False
    assert 'roughly 2,000' not in w.render(doc, tool)
    doc = w.tool_defaults(tool)
    doc['repositories']['/sample'] = {'excluded':True}
    assert 'roughly 2,000' not in w.render(doc, tool, repository='/sample')
