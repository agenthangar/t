"""One-shot audit correctness using entirely synthetic transcript events."""
import json
from pathlib import Path

import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def audit():
    return load_script(ROOT / 'scripts/audit_context.py', 'audit_context_test')


def test_metrics_and_candidates(audit):
    rows = [dict(kind='result',text='x'*n) for n in (0,1999,2000,8000,20000,20001)]
    rows += [dict(kind='call',name='exec',input=cmd) for cmd in (
        'cat source.py','cat source.py','edit source.py','cat source.py',
        't runners runs status abc','t runners runs status abc','sleep 2',
        't runners runs watch abc --timeout 60','t runners runs inspect abc','t runners runs logs abc')]
    rows += [dict(kind='image',payload_hash='same')]*2
    rows += [dict(kind='workflow',revision='fixture'),dict(kind='usage',value={'total_tokens':123})]
    report = audit.analyze(rows)
    m = report['measured']
    assert m['text_outputs'] == 6 and m['outputs_over_20000'] == 1
    assert m['total_output_characters'] == 52000
    assert m['bounded_watch_calls'] == 1 and m['short_sleep_calls'] == 1
    assert m['identical_image_payload_repeats'] == 1
    assert m['workflow_revision'] == 'fixture' and m['usage_last_observed']['total_tokens'] == 123
    review = report['manual_review']
    assert len(review['repeated_source_read_candidates']) == 2
    assert all(c['necessity'] == 'unreviewed' for c in review['repeated_source_read_candidates'])
    assert len(review['repeated_status_candidates']) == 1
    assert len(review['followup_retrieval_candidates']) == 1


def test_native_formats_images_and_unavailable(audit):
    rows = [dict(type='response_item',payload=dict(type='function_call',name='exec',call_id='1',arguments={'cmd':'screenshot'})),
            dict(type='response_item',payload=dict(type='custom_tool_call',name='exec',call_id='2',input='cat x')),
            dict(type='response_item',payload=dict(type='function_call_output',output='abc')),
            dict(type='event_msg',payload=dict(type='token_count', info={'total_token_usage':{'total_tokens':4}})),
            dict(type='response_item',payload=dict(type='message',content=[dict(type='input_image',image_url='https://example.invalid/a')])),
            dict(message=dict(usage={'output_tokens':2},content=[dict(type='tool_use',id='x',name='read_file',input={}),
                dict(type='tool_result',content='done'),dict(type='image',source={'type':'base64','data':'AAAA'})])),
            dict(message='ignored'), dict(message=dict(content='plain text'))]
    report = audit.analyze(rows)['measured']
    assert report['image_attachments'] == 2 and report['screenshot_calls'] == 1
    assert report['text_outputs'] == 2 and report['total_output_characters'] == 7
    assert report['workflow_revision'] is None
    assert audit.analyze([])['measured']['output_characters']['max'] == 0
    assert list(audit.images('text')) == []
    assert list(audit.images([dict(type='image_url', image_url='data:image/png;base64,AAAA')]))[0]['payload_hash']
    structured = dict(type='response_item', payload=dict(type='custom_tool_call_output', output=[
        dict(type='input_text', text='recorded text'),
        dict(type='input_image', image_url='data:image/png;base64,AAAA')]))
    measured = audit.analyze([structured])['measured']
    assert measured['total_output_characters'] == 13 and measured['image_attachments'] == 1
    native = [dict(kind='call', name=name, input=value) for name, value in [
        ('mcp__worker__task_status', {}), ('mcp__worker__wait_task', {}),
        ('clock.sleep', {'duration_ms':2000}), ('clock.sleep', 'invalid'),
        ('clock.sleep', {'duration_ms':60000}), ('clock.sleep', [])]]
    measured = audit.analyze(native)['measured']
    assert measured['status_queries'] == measured['bounded_watch_calls'] == measured['short_sleep_calls'] == 1
    repeated = list(audit.images([dict(type='image', data='AAAA', mimeType='image/png')]*2))
    assert audit.analyze(repeated)['measured']['identical_image_payload_repeats'] == 1
    assert list(audit.images([dict(type='image_url', image_url={'url':'data:image/png;base64,AAAA'})]))[0]['payload_hash']


def test_report_is_private_exclusive_and_validates_input(audit, tmp_path, capsys):
    source = tmp_path / 'synthetic.jsonl'
    source.write_text(json.dumps(dict(kind='result',text='secret sample text'))+'\n')
    output = tmp_path / 'report.json'
    audit.main([str(source),'--output',str(output)])
    assert output.stat().st_mode & 0o777 == 0o600
    assert 'secret sample text' not in output.read_text()
    assert 'Wrote 1' in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        audit.main([str(source),'--output',str(output)])
    for invalid in ('not json', '[]'):
        source.write_text(invalid+'\n')
        with pytest.raises(ValueError, match='record 1'):
            list(audit.read_rows(source))
