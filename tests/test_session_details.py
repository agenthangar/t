"""Enrich display metadata without changing session identity or liveness."""
import json
from pathlib import Path
import runpy
import sqlite3

import pytest


@pytest.fixture
def details(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'libexec/t_session_details.py'))
    home = tmp_path / 'home'
    codex = home / '.codex'
    codex.mkdir(parents=True)
    db = sqlite3.connect(codex / 'state_5.sqlite')
    db.execute('create table threads (id, cwd, name, title, source, archived, updated_at, rollout_path, model)')
    db.executemany('insert into threads values (?,?,?,?,?,?,?,?,?)', [
        ('saved', '/wt/3', 'Investigate cache invalidation', 'old prompt', 'cli', 0, 2, '/missing', 'model-test'),
        ('helper', '/wt/3', 'Ignore subagent', '', '{"subagent":{}}', 0, 4, '/missing', 'wrong-model'),
        ('archived', '/wt/3', 'Ignore archived', '', 'cli', 1, 3, '/missing', 'wrong-model'),
    ])
    db.commit(); db.close()
    return module, home, codex


def test_inactive_desktop_gets_last_context_and_model_without_becoming_live(details):
    m, home, codex = details
    row = '-\t/wt/3\tweb-3\tapp\tnone\t(Codex desktop workspace — reopen)\tcodex'
    enriched = m['enrich']([row], home, codex)[0].split('\t')
    assert enriched[:5] == row.split('\t')[:5]
    assert enriched[5:] == ['Investigate cache invalidation', 'codex', 'model-test', '-']


@pytest.mark.parametrize('sid,state', [('missing', 'app'), ('-', 'attached')])
def test_does_not_borrow_another_threads_context(details, sid, state):
    m, home, codex = details
    row = f'{sid}\t/wt/3\tweb-3\t{state}\tactive\tCurrent session title\tcodex'
    assert m['enrich']([row], home, codex) == [row + '\t-\t-']


def test_no_conversation_is_explicit_and_legacy_rows_survive(details):
    m, home, codex = details
    row = '-\t/wt/4\tweb-4\tapp\tnone\t(Codex desktop workspace)\tcodex'
    for directory in [codex, home / 'missing']:
        result = m['enrich']([row, 'legacy\trow\n'], home, directory)
        assert result[0].split('\t')[5:] == ['No conversation recorded', 'codex', '-', '-']
        assert result[1] == 'legacy\trow'


def test_exact_thread_and_cwd_preserve_current_summary(details):
    m, home, codex = details
    row = 'saved\t/wt/3\tweb-3\tdetached\tactive\tCurrent title · #1 open\tcodex'
    assert m['enrich']([row], home, codex) == [row + '\tmodel-test\t-']
    wrong = row.replace('/wt/3', '/wt/9')
    assert m['enrich']([wrong], home, codex) == [wrong + '\t-\t-']


def test_model_falls_back_to_rollout_for_older_index(details, tmp_path):
    m, home, codex = details
    rollout = tmp_path / 'rollout.jsonl'
    rollout.write_text(json.dumps({'type':'turn_context','payload':{'model':'recorded-model'}})+'\n')
    db=sqlite3.connect(codex / 'state_5.sqlite')
    db.execute('alter table threads drop column model')
    db.execute('update threads set rollout_path=? where id=?', (str(rollout), 'saved'))
    db.commit(); db.close()
    row='saved\t/wt/3\tweb-3\tapp\tnone\t(no active session)\tcodex'
    assert m['enrich']([row],home,codex)[0].split('\t')[5:] == ['Investigate cache invalidation','codex','recorded-model','-']


def test_claude_model_comes_from_assistant_record(details):
    m, home, codex = details
    path=home / '.claude/projects/-wt-3/saved.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(map(json.dumps,[
        {'type':'assistant','message':{'model':'model-first'}},
        {'type':'user','model':'wrong'},
        {'type':'assistant','message':{'model':'model-latest'}},
        {'type':'assistant','message':{'model':'<synthetic>'}},
    ])))
    row='saved\t/wt/3\tweb-3\tattached\tactive\tActual title\tclaude'
    assert m['enrich']([row],home,codex) == [row+'\tmodel-latest\t-']


def test_model_tail_is_bounded_and_tolerates_bad_records(details, tmp_path):
    m, _, _ = details
    path=tmp_path/'large.jsonl'
    path.write_text('x'*(1024*1024)+'\n'+json.dumps({'type':'turn_context','payload':{'model':'last-model'}})+'\n'
                    +'["model"]\n{"model":broken}\n{"type":"assistant","message":null,"model":"wrong"}\n')
    assert m['transcript_model'](path)=='last-model'
    assert m['transcript_model'](None)=='-'
    path.write_text('empty\n')
    assert m['transcript_model'](path)=='-'


def test_corrupt_index_and_control_characters(details):
    m, home, codex = details
    row='saved\t/wt/3\tweb-3\tapp\tnone\tTitle\x1b[31m\tcodex'
    (codex/'state_5.sqlite').write_bytes(b'bad')
    assert '\x1b' not in m['enrich']([row],home,codex)[0]


@pytest.mark.parametrize('sid', ['saved', '-'])
def test_display_transcript_model_overrides_stale_index_without_changing_identity(details, tmp_path, sid):
    m, home, codex = details
    transcript = tmp_path / 'current.jsonl'
    transcript.write_text(json.dumps({'type': 'turn_context', 'payload': {'model': 'changed-model'}}) + '\n')
    row = f'{sid}\t/wt/3\tweb-3\tattached\tactive\tCurrent task\tcodex'
    assert m['enrich']([row + '\t' + str(transcript)], home, codex) == [row + '\tchanged-model\t-']
    transcript.unlink()
    assert m['enrich']([row + '\t' + str(transcript)], home, codex) == [row + ('\tmodel-test' if sid == 'saved' else '\t-') + '\t-']


def test_unknown_cli_model_can_use_index_of_verified_display_rollout(details, tmp_path):
    m, home, codex = details
    transcript = tmp_path / 'current.jsonl'
    transcript.write_text('large tool output without a recent model record\n')
    with sqlite3.connect(codex / 'state_5.sqlite') as db:
        db.execute('update threads set rollout_path=? where id=?', (str(transcript), 'saved'))
    db.close()
    row = '-\t/wt/3\tweb-3\tattached\tactive\tCurrent task\tcodex'
    assert m['enrich']([row + '\t' + str(transcript)], home, codex) == [row + '\tmodel-test\t-']
    assert m['enrich']([row + '\t/other-rollout'], home, codex) == [row + '\t-\t-']


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_last_turn_ignores_later_activity(details, tmp_path, agent):
    m, home, codex = details
    start = '2026-10-23T09:41:07-07:00'
    newer = '2026-10-23T17:00:00Z'
    if agent == 'codex':
        turn = {'type': 'event_msg', 'payload': {'type': 'task_started'}}
        records = [dict(turn, timestamp='2026-10-22T12:00:00Z'), dict(turn, timestamp=start),
                   {'timestamp': newer, 'type': 'turn_context', 'payload': {'model': 'current'}},
                   {'timestamp': newer, 'type': 'event_msg', 'payload': {'type': 'task_complete'}}]
    else:
        turn = {'type': 'user', 'message': {'content': 'Continue work'}}
        records = [dict(turn, timestamp='2026-10-22T12:00:00Z'), dict(turn, timestamp=start),
                   {'timestamp': newer, 'type': 'assistant', 'message': {'model': 'current'}},
                   {'timestamp': newer, 'type': 'user', 'message': {'content': [
                       {'type': 'tool_result', 'content': 'Result'}, {'type': 'text', 'text': 'context'}]}},
                   *[{'timestamp': newer, 'type': 'user', 'message': {'content': content}} for content in (
                       '<local-command-stdout>Done</local-command-stdout>',
                       [{'type': 'text', 'text': '<system-reminder>Injected</system-reminder>'}],
                       [{'type': 'text', 'text': ''}])],
                   *[dict(turn, timestamp=newer, **{key: True}) for key in ('isMeta', 'isSidechain', 'isCompactSummary')]]
    path = tmp_path / 'turns.jsonl'
    path.write_text('\n'.join(map(json.dumps, records)) + '\n')
    row = f'saved\t/wt/3\tweb-3\tdetached\tactive\tCurrent work\t{agent}'
    enriched = m['enrich']([row + '\t' + str(path)], home, codex)[0].split('\t')
    assert enriched[:7] == row.split('\t')
    assert enriched[7:] == ['current', '2026-10-23T16:41:07Z']
    # A large output must not hide the start of a long-running turn (> old model tail).
    with path.open('a') as fh:
        fh.write(json.dumps({'type': 'output', 'timestamp': newer, 'text': 'x' * (2 * 1024 * 1024)}) + '\n')
    assert m['transcript_last_turn'](path, agent) == '2026-10-23T16:41:07Z'


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_last_turn_uses_verified_fallback_transcript(details, agent):
    m, home, codex = details
    path = home / '.claude/projects/-wt-3/saved.jsonl'
    path.parent.mkdir(parents=True)
    record = {'type': 'event_msg', 'payload': {'type': 'task_started'}} if agent == 'codex' else {
        'type': 'user', 'message': {'content': [{'type': 'image', 'source': {}}]}}
    path.write_text(json.dumps(dict(record, timestamp='2026-10-23T16:41:07Z')))
    with sqlite3.connect(codex / 'state_5.sqlite') as db:
        db.execute('update threads set rollout_path=? where id=?', (str(path), 'saved'))
    db.close()
    row = f'saved\t/wt/3\tweb-3\tapp\tnone\tSaved work\t{agent}'
    assert m['enrich']([row], home, codex)[0].split('\t')[-1] == '2026-10-23T16:41:07Z'
    # A supplied current transcript always wins, even when it has no evidence yet.
    empty = home / 'current.jsonl'
    empty.write_text('')
    assert m['enrich']([row + '\t' + str(empty)], home, codex)[0].split('\t')[-1] == '-'


@pytest.mark.parametrize('boundary', ['task_started', 'task_complete', 'turn_aborted', 'user_message', None])
def test_codex_user_event_fallback_and_explicit_start(details, tmp_path, boundary):
    m, _, _ = details
    path = tmp_path / 'fallback.jsonl'
    records = [] if boundary is None else [
        {'timestamp': '2026-10-23T16:41:06Z', 'type': 'event_msg', 'payload': {'type': boundary}}]
    records.append({'timestamp': '2026-10-23T16:41:07Z', 'type': 'event_msg', 'payload': {'type': 'user_message'}})
    path.write_text('\n'.join(map(json.dumps, records)))
    expected = '2026-10-23T16:41:06Z' if boundary == 'task_started' else '2026-10-23T16:41:07Z'
    assert m['transcript_last_turn'](path, 'codex') == expected


def test_last_turn_unknown_and_malformed_records(details, tmp_path):
    m, _, _ = details
    path = tmp_path / 'bad.jsonl'
    path.write_text('\n'.join([
        '{"timestamp":broken}', '["timestamp"]',
        *map(json.dumps, [
            {'timestamp': '2026-10-23T16:41:07Z', 'type': 'event_msg', 'payload': None},
            {'timestamp': '2026-10-23T16:41:07Z', 'type': 'assistant', 'message': {}},
            {'timestamp': '2026-10-23T16:41:07Z', 'type': 'user', 'message': None},
            {'timestamp': '2026-10-23T16:41:07Z', 'type': 'user', 'message': {'content': [None]}},
            *[{'timestamp': value, 'type': 'event_msg', 'payload': {'type': 'task_started'}}
              for value in (None, 123, 'bad', '2026-10-23T16:41:07')]])]))
    for agent in ('codex', 'claude', 'cursor'):
        assert m['transcript_last_turn'](path, agent) == '-'
        assert m['transcript_last_turn'](None, agent) == '-'
        assert m['transcript_last_turn'](tmp_path / 'missing', agent) == '-'


def test_format_last_turn_converts_to_viewer_timezone(details, monkeypatch):
    import time
    m, _, _ = details
    with monkeypatch.context() as patch:
        patch.setenv('TZ', 'America/Los_Angeles')
        time.tzset()
        assert m['format_last_turn']('2026-10-23T16:41:07Z') == '2026-10-23 09:41:07 PDT'
        assert m['format_last_turn']('2026-01-23T16:41:07Z') == '2026-01-23 08:41:07 PST'
        assert m['format_last_turn']('bad') == '-'
    time.tzset()
