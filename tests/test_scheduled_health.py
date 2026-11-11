"""Synthetic GitHub metadata verifies alert delivery without sending messages."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts import scheduled_health as health


NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def run(**changes):
    data = dict(id=123, head_branch='main', head_sha='a'*40, event='schedule',
                status='completed', conclusion='success', run_attempt=1,
                created_at=(NOW-timedelta(hours=1)).isoformat(), html_url='https://github.com/test/repo/actions/runs/123')
    data.update(changes)
    return data


@pytest.mark.parametrize('conclusion', sorted(health.FAILURES | {'skipped', 'neutral'}))
def test_unsuccessful_run_is_an_incident(conclusion):
    key, reason, item = health.assess('release.yml', [run(conclusion=conclusion)], NOW, NOW.isoformat())
    assert key == '123-1-' + conclusion
    assert conclusion in reason and item['id'] == 123


@pytest.mark.parametrize('workflow,hours', [('release.yml', 36), ('scorecard.yml', 192)])
def test_schedule_deadline_is_independent_of_recent_pushes(workflow, hours):
    old = run(created_at=(NOW-timedelta(hours=hours+1)).isoformat())
    recent = run(id=124, event='push')
    assert health.assess(workflow, [recent, old], NOW, NOW.isoformat())[0] == 'overdue-123'
    assert health.assess(workflow, [recent], NOW, old['created_at'])[0] == 'overdue-never'
    assert health.assess(workflow, [recent], NOW, NOW.isoformat())[0] is None


def test_incomplete_recovery_stays_open_and_stuck_run_alerts():
    pending = run(status='in_progress', conclusion=None)
    assert health.assess('release.yml', [pending], NOW, NOW.isoformat())[0] == 'pending'
    pending['created_at'] = (NOW-timedelta(hours=3)).isoformat()
    assert health.assess('release.yml', [pending], NOW, NOW.isoformat())[0] == '123-1-stuck'


def test_untrusted_or_pr_runs_do_not_change_health():
    assert health.assess('release.yml', [run(event='pull_request', conclusion='failure'),
                         run(head_branch='feature', conclusion='failure')], NOW, NOW.isoformat()) == (None, '', None)


def test_failure_assigns_mentions_deduplicates_reopens_and_recovers(monkeypatch):
    calls, issues = [], []
    def api(repo, path, **kw):
        calls.append((path, kw))
        if path.startswith('actions/'):
            return [{'jobs': [dict(name='Linux tests', html_url='https://github.com/test/repo/jobs/1', conclusion='failure')]}]
        if path == 'issues':
            return dict(number=7, state='open', **kw['payload'])
    monkeypatch.setattr(health, 'api', api)
    health.notify('test/repo', 'maintainer', 'release.yml', '123-1-failure', 'Failed.', run(), issues)
    assert issues[0]['assignees'] == ['maintainer']
    assert '@maintainer' in issues[0]['body'] and 'Linux tests' in issues[0]['body']
    count = len(calls)
    health.notify('test/repo', 'maintainer', 'release.yml', '123-1-failure', 'Failed.', run(), issues)
    assert len(calls) == count
    health.notify('test/repo', 'maintainer', 'release.yml', 'pending', '', run(), issues)
    assert len(calls) == count
    health.notify('test/repo', 'maintainer', 'release.yml', None, '', run(), issues)
    assert issues[0]['state'] == 'closed'
    count = len(calls)
    health.notify('test/repo', 'maintainer', 'release.yml', '123-1-failure', 'Failed.', run(), issues)
    assert len(calls) == count  # a replay cannot reopen an already recovered run
    health.notify('test/repo', 'maintainer', 'release.yml', '123-2-failure', 'Failed again.', run(run_attempt=2), issues)
    assert issues[0]['state'] == 'open'
    assert calls[-1][0] == 'issues/7/comments' and '@maintainer' in calls[-1][1]['payload']['body']


def test_disabled_or_overdue_without_run_still_notifies(monkeypatch):
    calls = []
    def api(repo, path, **kw):
        calls.append((path, kw))
        return dict(number=8, state='open', **kw['payload'])
    monkeypatch.setattr(health, 'api', api)
    health.notify('test/repo', 'maintainer', 'scorecard.yml', 'disabled', 'Disabled.', None, [])
    assert len(calls) == 1 and calls[0][1]['payload']['assignees'] == ['maintainer']


def test_api_payload_is_passed_on_stdin_not_interpolated(monkeypatch):
    calls = []
    monkeypatch.setattr(health.subprocess, 'check_output', lambda argv, **kw: calls.append((argv, kw)) or '{}')
    body = {'body': 'quote " and `command` and $HOME'}
    health.api('test/repo', 'issues', method='POST', payload=body)
    assert calls[0][0][-2:] == ['--input', '-']
    assert health.json.loads(calls[0][1]['input']) == body


def test_monitor_reports_trigger_failure_even_if_a_newer_run_recovered(tmp_path, monkeypatch):
    import json
    event = tmp_path / 'event.json'
    failed = run(conclusion='failure', workflow_id=5, head_repository={'full_name': 'test/repo'})
    event.write_text(json.dumps({'workflow_run': failed}))
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event))
    monkeypatch.setenv('GITHUB_REPOSITORY', 'test/repo')
    monkeypatch.setenv('CI_ALERT_ASSIGNEE', 'maintainer')
    monkeypatch.setattr('sys.argv', ['scheduled_health.py'])
    recent = run(id=124, created_at=datetime.now(timezone.utc).isoformat())
    def api(repo, path, **kwargs):
        if path.startswith('issues?'):
            return [[]]
        if '/runs?' in path:
            return {'workflow_runs': [recent]}
        return {'id': 5 if 'release.yml' in path else 6, 'state': 'active', 'created_at': recent['created_at']}
    calls = []
    monkeypatch.setattr(health, 'api', api)
    monkeypatch.setattr(health, 'notify', lambda *args: calls.append(args))
    health.main()
    assert [(args[2], args[3]) for args in calls] == [
        ('release.yml', '123-1-failure'), ('release.yml', None), ('scorecard.yml', None)]
