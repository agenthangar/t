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


def test_failure_deduplicates_replays_and_recovers(tmp_path, monkeypatch):
    messages, state = [], {}
    path = tmp_path / 'state.json'
    monkeypatch.setattr(health, 'send_discord', lambda url, content: messages.append(content))
    monkeypatch.setattr(health, 'api', lambda *a, **kw: [{'jobs': [dict(
        name='Linux tests', html_url='https://github.com/test/repo/jobs/1', conclusion='failure')]}])
    def notify(incident, item=None):
        health.notify('test/repo', 'webhook', 'release.yml', incident, 'Failed.', item or run(), state, path)
    notify('123-1-failure')
    assert len(messages) == 1 and 'Linux tests' in messages[0]['fields'][0]['value'] and run()['html_url'] == messages[0]['url']
    # Restore persisted state as a later workflow invocation would.
    state = health.json.loads(path.read_text())
    notify('123-1-failure')
    notify('pending')
    assert len(messages) == 1
    notify(None)
    assert len(messages) == 2 and 'recovered' in messages[-1]['title']
    notify(None)
    notify('123-1-failure')
    assert len(messages) == 2
    notify('123-2-failure', run(run_attempt=2))
    assert len(messages) == 3 and 'Attempt 2' in messages[-1]['footer']['text']


@pytest.mark.parametrize('incident', ['disabled', 'overdue-never'])
def test_condition_alerts_again_after_recovery(incident, tmp_path, monkeypatch):
    messages, state = [], {}
    monkeypatch.setattr(health, 'send_discord', lambda url, content: messages.append(content))
    for value in [incident, incident, None, incident]:
        health.notify('test/repo', 'webhook', 'scorecard.yml', value, 'Needs attention.', None,
                      state, tmp_path / 'state.json')
    assert len(messages) == 3
    assert messages[0]['url'] == 'https://github.com/test/repo/actions' and 'recovered' in messages[1]['title']


def test_failed_delivery_does_not_persist_or_suppress_retry(tmp_path, monkeypatch):
    state = {}
    path = tmp_path / 'state.json'
    def fail(*a):
        raise RuntimeError('delivery failed')
    monkeypatch.setattr(health, 'send_discord', fail)
    with pytest.raises(RuntimeError, match='delivery failed'):
        health.notify('test/repo', 'webhook', 'scorecard.yml', 'disabled', 'Disabled.', None, state, path)
    assert not state and not path.exists()


def test_many_failed_jobs_fit_discord_limit(tmp_path, monkeypatch):
    messages = []
    monkeypatch.setattr(health, 'send_discord', lambda url, content: messages.append(content))
    monkeypatch.setattr(health, 'api', lambda *a, **kw: [{'jobs': [dict(
        name='Long job name ' * 10, html_url='https://github.com/test/repo/jobs/1', conclusion='failure')] * 100}])
    health.notify('test/repo', 'webhook', 'release.yml', '123-1-failure', 'Failed.', run(), {}, tmp_path / 'state.json')
    assert len(messages[0]['fields'][0]['value']) <= 1024 and 'More failed jobs' in messages[0]['fields'][0]['value']


@pytest.mark.parametrize('url', [
    '', 'http://discord.com/api/webhooks/123/token', 'https://evil.example/api/webhooks/123/token',
    'https://discord.com.evil.example/api/webhooks/123/token',
    'https://user@discord.com/api/webhooks/123/token', 'https://discord.com:443/api/webhooks/123/token',
    'https://discord.com/api/webhooks/123/token/github',
    'https://discord.com/api/webhooks/123/token?wait=false', 'https://discord.com/api/webhooks/123/token#fragment',
])
def test_webhook_target_rejects_non_native_or_untrusted_url(url):
    with pytest.raises(ValueError, match='copied Discord webhook URL'):
        health.webhook_target(url)


def fake_transport(monkeypatch, responses):
    calls, sleeps = [], []
    class Connection:
        def __init__(self, host, timeout):
            assert host == 'discord.com' and timeout == 20
        def request(self, method, path, body, headers):
            calls.append((method, path, health.json.loads(body), headers))
        def getresponse(self):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        def close(self):
            pass
    monkeypatch.setattr(health.http.client, 'HTTPSConnection', Connection)
    monkeypatch.setattr(health.time, 'sleep', sleeps.append)
    return calls, sleeps


def response(status, body=b'{"id":"456"}', retry_after='2'):
    from types import SimpleNamespace
    return SimpleNamespace(status=status, read=lambda limit: body, getheader=lambda *a: retry_after)


def test_discord_waits_for_confirmation_and_disables_mentions(monkeypatch):
    calls, sleeps = fake_transport(monkeypatch, [response(200)])
    health.send_discord('https://discord.com/api/webhooks/123/token', {'title': '@everyone test'})
    assert calls[0][0:2] == ('POST', '/api/webhooks/123/token?wait=true')
    assert calls[0][2] == {'embeds': [{'title': '@everyone test'}], 'allowed_mentions': {'parse': []}}
    assert sleeps == []


@pytest.mark.parametrize('transient', [response(429, retry_after='120'), response(503), OSError('secret URL')])
def test_discord_retries_transient_failures(monkeypatch, transient):
    calls, sleeps = fake_transport(monkeypatch, [transient, response(200)])
    health.send_discord('https://discord.com/api/webhooks/123/token', {'title': 'test'})
    assert len(calls) == 2 and len(sleeps) == 1 and sleeps[0] <= 30


@pytest.mark.parametrize('replies,expected', [
    ([response(401)], 'HTTP 401'), ([response(302)], 'HTTP 302'),
    ([response(200, b'{}')], 'did not confirm'), ([response(200, b'not json')], 'invalid delivery response'),
    ([OSError('secret URL')] * 3, 'after three attempts'),
])
def test_discord_rejects_unconfirmed_delivery_without_leaking_secrets(monkeypatch, replies, expected):
    fake_transport(monkeypatch, replies)
    with pytest.raises(RuntimeError, match=expected) as caught:
        health.send_discord('https://discord.com/api/webhooks/123/token', {'title': 'test'})
    assert 'secret URL' not in str(caught.value) and 'token' not in str(caught.value)


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
    monkeypatch.setenv('CI_ALERT_WEBHOOK_URL', 'https://discord.com/api/webhooks/123/token')
    monkeypatch.setenv('CI_ALERT_STATE_PATH', str(tmp_path / 'state.json'))
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
