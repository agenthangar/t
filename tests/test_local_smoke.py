"""A push must refer to the exact successful local snapshot, including merges."""
import copy
import json

import pytest

from scripts import local_smoke as local
from test_workflow import workflow  # noqa: F401 -- isolated account configuration


def evidence():
    snapshot = {'id': 'snapshot-1', 'source_head': 'a' * 40,
                'files': [{'path': 'bin/t', 'sha256': 'old', 'mode': 493, 'size': 1}]}
    receipt = dict(status='succeeded', exit_code=0, snapshot=snapshot,
                   command=['python', 'scripts/smoke.py'],
                   targets={'local': dict(type='local', status='succeeded', exit_code=0,
                                          snapshot_id='snapshot-1')})
    return receipt, copy.deepcopy(snapshot)


def test_changed_code_merge_and_failed_smoke_invalidate_evidence():
    receipt, current = evidence()
    local.check_evidence(receipt, current)
    for change in ('merge', 'contents', 'mode', 'deleted', 'failed', 'cancelled', 'remote', 'command', 'snapshot'):
        candidate, current = evidence()
        if change == 'merge':
            current['source_head'] = 'b' * 40  # Even a same-tree merge needs a new run.
        elif change in ('contents', 'mode'):
            current['files'][0]['sha256' if change == 'contents' else 'mode'] = 'changed'
        elif change == 'deleted':
            current['files'].clear()
        elif change in ('failed', 'cancelled'):
            candidate['status'] = change
        elif change == 'remote':
            candidate['targets']['local']['type'] = 'ssh'
        elif change == 'command':
            candidate['command'] = ['python', '-m', 'pytest']
        else:
            candidate['targets']['local']['snapshot_id'] = 'other'
        with pytest.raises(ValueError, match='stale local smoke'):
            local.check_evidence(candidate, current)


def test_push_gate_rejects_other_commits_dirty_work_and_failed_receipts(tmp_path, monkeypatch):
    receipt, current = evidence()
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(local, 'evidence_path', lambda: path)
    monkeypatch.setattr(local, 'snapshot', lambda: current)
    dirty = []
    monkeypatch.setattr(local, 'git', lambda *args: ('dirty' if dirty else '') if args[0] == 'status' else 'a' * 40)
    update = f"refs/heads/dev/test {'a'*40} refs/heads/dev/test {'b'*40}\n"
    local.check_push([update])
    with pytest.raises(ValueError, match='tested worktree'):
        local.check_push([update.replace('a'*40, 'c'*40)])
    dirty.append(True)
    with pytest.raises(ValueError, match='commit tracked edits'):
        local.check_push([update])
    dirty.clear()
    receipt['status'] = 'failed'
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='failed or stale'):
        local.check_push([update])
    path.unlink()
    with pytest.raises(FileNotFoundError):
        local.check_push([update])
    local.check_push([f"(delete) {'0'*40} refs/heads/old {'a'*40}"])
    local.check_push([f"refs/tags/v1 {'a'*40} refs/tags/v1 {'0'*40}"])


def test_failed_rerun_replaces_previous_success(tmp_path, monkeypatch):
    path = tmp_path / 'receipt.json'
    path.write_text('old success')
    monkeypatch.setattr(local, 'evidence_path', lambda: path)
    def start(root, config, command, **kwargs):
        assert not path.exists()
        assert command[1:] == ['scripts/smoke.py']
        assert kwargs['targets'] == ['local']
        return dict(id='failed-run', status='failed', exit_code=1)
    monkeypatch.setattr(local.runpy, 'run_path', lambda path: {'start': start})
    assert local.main([]) == 1
    assert json.loads(path.read_text())['status'] == 'failed'


def test_emitted_instructions_require_fresh_local_smoke(workflow):
    import copy
    for timing in ('before_pr', 'pr_ci'):
        policy = workflow.update_behavior(copy.deepcopy(workflow.DEFAULT), 'test_timing', timing)
        text = workflow._stage_text(policy)
        for phrase in ('shared essential-flow smoke selection', 'isolated local snapshot runner',
                       'resolving merge conflicts', 'A failing local smoke blocks the push',
                       'tested snapshot and source commit', 'never reuse pre-conflict evidence',
                       'Do not substitute a broad local regression suite'):
            assert phrase in text
    hook = (local.ROOT / '.githooks/pre-push').read_text()
    assert 'scripts/local_smoke.py" check-push' in hook
