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
    assert enriched[5:] == ['Investigate cache invalidation', 'codex', 'model-test']


@pytest.mark.parametrize('sid,state', [('missing', 'app'), ('-', 'attached')])
def test_does_not_borrow_another_threads_context(details, sid, state):
    m, home, codex = details
    row = f'{sid}\t/wt/3\tweb-3\t{state}\tactive\tCurrent session title\tcodex'
    assert m['enrich']([row], home, codex) == [row + '\t-']


def test_no_conversation_is_explicit_and_legacy_rows_survive(details):
    m, home, codex = details
    row = '-\t/wt/4\tweb-4\tapp\tnone\t(Codex desktop workspace)\tcodex'
    for directory in [codex, home / 'missing']:
        result = m['enrich']([row, 'legacy\trow\n'], home, directory)
        assert result[0].split('\t')[5:] == ['No conversation recorded', 'codex', '-']
        assert result[1] == 'legacy\trow'


def test_exact_thread_and_cwd_preserve_current_summary(details):
    m, home, codex = details
    row = 'saved\t/wt/3\tweb-3\tdetached\tactive\tCurrent title · #1 open\tcodex'
    assert m['enrich']([row], home, codex) == [row + '\tmodel-test']
    wrong = row.replace('/wt/3', '/wt/9')
    assert m['enrich']([wrong], home, codex) == [wrong + '\t-']


def test_model_falls_back_to_rollout_for_older_index(details, tmp_path):
    m, home, codex = details
    rollout = tmp_path / 'rollout.jsonl'
    rollout.write_text(json.dumps({'type':'turn_context','payload':{'model':'recorded-model'}})+'\n')
    db=sqlite3.connect(codex / 'state_5.sqlite')
    db.execute('alter table threads drop column model')
    db.execute('update threads set rollout_path=? where id=?', (str(rollout), 'saved'))
    db.commit(); db.close()
    row='saved\t/wt/3\tweb-3\tapp\tnone\t(no active session)\tcodex'
    assert m['enrich']([row],home,codex)[0].split('\t')[5:] == ['Investigate cache invalidation','codex','recorded-model']


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
    assert m['enrich']([row],home,codex) == [row+'\tmodel-latest']


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
    assert m['enrich']([row + '\t' + str(transcript)], home, codex) == [row + '\tchanged-model']
    transcript.unlink()
    assert m['enrich']([row + '\t' + str(transcript)], home, codex) == [row + ('\tmodel-test' if sid == 'saved' else '\t-')]


def test_unknown_cli_model_can_use_index_of_verified_display_rollout(details, tmp_path):
    m, home, codex = details
    transcript = tmp_path / 'current.jsonl'
    transcript.write_text('large tool output without a recent model record\n')
    with sqlite3.connect(codex / 'state_5.sqlite') as db:
        db.execute('update threads set rollout_path=? where id=?', (str(transcript), 'saved'))
    db.close()
    row = '-\t/wt/3\tweb-3\tattached\tactive\tCurrent task\tcodex'
    assert m['enrich']([row + '\t' + str(transcript)], home, codex) == [row + '\tmodel-test']
    assert m['enrich']([row + '\t/other-rollout'], home, codex) == [row + '\t-']
