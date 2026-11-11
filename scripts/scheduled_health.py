"""Notify Discord of failed or overdue scheduled workflows.

Run from trusted main, with contents:read and actions:read. Only
GitHub API metadata is read; source-run artifacts and logs are never executed.
"""
import argparse
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
import re
import subprocess
from pathlib import Path
import time
from urllib.parse import urlsplit


WORKFLOWS = {
    'release.yml': ('Releases', timedelta(hours=36)),
    'scorecard.yml': ('Scorecard', timedelta(days=8)),
}
FAILURES = {'failure', 'cancelled', 'timed_out', 'action_required', 'stale', 'startup_failure'}


def api(repo, path, *, method='GET', payload=None, paginate=False):
    argv = ['gh', 'api', '--method', method, f'repos/{repo}/{path}']
    if paginate:
        argv += ['--paginate', '--slurp']
    if payload is not None:
        argv += ['--input', '-']
    output = subprocess.check_output(argv, input=json.dumps(payload) if payload is not None else None,
                                     text=True)
    return json.loads(output) if output.strip() else None


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def assess(workflow, runs, now, created_at):
    """Return a stable incident key and actionable reason, or healthy state."""
    _, max_age = WORKFLOWS[workflow]
    eligible = [r for r in runs if r['head_branch'] == 'main'
                and r['event'] in {'schedule', 'workflow_dispatch', 'push'}]
    eligible.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
    scheduled = next((r for r in eligible if r['event'] == 'schedule'), None)
    latest = eligible[0] if eligible else None
    if latest:
        key = f"{latest['id']}-{latest.get('run_attempt', 1)}"
        conclusion = latest.get('conclusion')
        if conclusion in FAILURES or (latest['status'] == 'completed' and conclusion != 'success'):
            return key + '-' + str(conclusion), f"Run concluded **{conclusion}**.", latest
        if latest['status'] != 'completed' and now - timestamp(latest['created_at']) > timedelta(hours=2):
            return key + '-stuck', 'Run has not completed within two hours.', latest
    last_schedule = scheduled['created_at'] if scheduled else created_at
    if now - timestamp(last_schedule) > max_age:
        return ('overdue-' + (str(scheduled['id']) if scheduled else 'never'),
                f'No scheduled run started within {int(max_age.total_seconds() / 3600)} hours.', latest)
    # A running recovery has not yet proved that the incident is resolved.
    if latest and latest['status'] != 'completed':
        return 'pending', '', latest
    return None, '', latest


def webhook_target(url):
    """Accept only a native Discord incoming webhook, never a redirect target."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc not in {'discord.com', 'discordapp.com'}
            or not re.fullmatch(r'/api/webhooks/[0-9]+/[A-Za-z0-9_-]+', parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError('Set CI_ALERT_WEBHOOK_URL to the copied Discord webhook URL')
    return parsed.netloc, parsed.path + '?wait=true'


def send_discord(url, content):
    host, path = webhook_target(url)
    body = json.dumps({'content': content, 'allowed_mentions': {'parse': []}}).encode()
    for attempt in range(3):
        connection = http.client.HTTPSConnection(host, timeout=20)
        try:
            connection.request('POST', path, body, {'Content-Type': 'application/json',
                                                  'User-Agent': 't-scheduled-health/1'})
            response = connection.getresponse()
            data = response.read(65536)
            if response.status == 200:
                message = json.loads(data)
                if isinstance(message, dict) and message.get('id'):
                    return
                raise RuntimeError('Discord did not confirm message creation')
            if response.status != 429 and response.status < 500:
                raise RuntimeError(f'Discord rejected alert delivery (HTTP {response.status})')
            delay = response.getheader('Retry-After', '2')
            delay = min(max(float(delay), 1), 30) if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', delay) else 2
        except (OSError, http.client.HTTPException):
            # Exception strings can contain request details; never expose the URL/token.
            delay = 2
        except (ValueError, TypeError):
            raise RuntimeError('Discord returned an invalid delivery response') from None
        finally:
            connection.close()
        if attempt < 2:
            time.sleep(delay)
    raise RuntimeError('Discord alert delivery failed after three attempts') from None


def save_state(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def notify(repo, webhook, workflow, incident, reason, run, state, state_path):
    previous = state.get(workflow, {})
    if incident == 'pending':
        return
    name = WORKFLOWS.get(workflow, ('Alert delivery test', None))[0]
    seen = previous.get('seen', [])
    if incident is None:
        if not previous.get('active'):
            return
        url = run['html_url'] if run else f'https://github.com/{repo}/actions'
        content = f'✅ **{repo}: {name} recovered.**\n[Latest successful run]({url})\nScheduled execution is within its deadline.'
    else:
        if incident == previous.get('active') or incident in seen:
            return
        lines = [f'⚠️ **{repo}: {name} needs attention.**', reason]
        if run:
            lines += [f"[Workflow run]({run['html_url']})", f"Commit: `{run['head_sha']}` · Attempt: {run.get('run_attempt', 1)}"]
            pages = api(repo, f"actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100", paginate=True)
            failed = [j for page in pages for j in page['jobs'] if j.get('conclusion') in FAILURES]
            for job in failed:
                line = f"- [{job['name']}]({job['html_url']}): {job['conclusion']}"
                if len('\n'.join(lines + [line])) > 1850:
                    lines.append('More failed jobs are listed in the workflow run.')
                    break
                lines.append(line)
        else:
            lines.append(f'[Workflow dashboard](https://github.com/{repo}/actions)')
        content = '\n'.join(lines)
    send_discord(webhook, content)
    # Remember run-specific events across recoveries. Stable disabled/overdue
    # conditions can recur, so only deduplicate those while actively unhealthy.
    if incident and re.match(r'^[0-9]+-', incident):
        seen = (seen + [incident])[-100:]
    state[workflow] = {'active': incident, 'seen': seen}
    save_state(state_path, state)
    print(f'{name}: Discord confirmed {"recovery" if incident is None else "alert"} delivery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='send a clearly labeled delivery test')
    args = parser.parse_args()
    repo = os.environ['GITHUB_REPOSITORY']
    webhook = os.environ.get('CI_ALERT_WEBHOOK_URL', '')
    webhook_target(webhook)
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('A GitHub repository is required')
    state_path = Path(os.environ.get('CI_ALERT_STATE_PATH', '.ci-alert-state.json'))
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if args.test:
        notify(repo, webhook, 'delivery-test', 'delivery-test-' + os.environ['GITHUB_RUN_ID'],
               '**Delivery test only.** No nightly or weekly failure is being reported.', None, state, state_path)
        return
    now = datetime.now(timezone.utc)
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    if event_path:
        with open(event_path) as stream:
            triggered = json.load(stream).get('workflow_run')
    else:
        triggered = None
    for workflow in WORKFLOWS:
        metadata = api(repo, f'actions/workflows/{workflow}')
        # Read schedules separately so frequent pushes cannot hide a weekly run.
        runs = api(repo, f'actions/workflows/{workflow}/runs?branch=main&per_page=20')['workflow_runs']
        runs += api(repo, f'actions/workflows/{workflow}/runs?branch=main&event=schedule&per_page=1')['workflow_runs']
        if (triggered and triggered['workflow_id'] == metadata['id']
                and triggered['head_branch'] == 'main'
                and triggered['head_repository']['full_name'] == repo
                and triggered.get('conclusion') in FAILURES):
            # Still notify a failed scheduled run if a newer push already passed
            # before this monitor started; recovery can follow in the same pass.
            key = f"{triggered['id']}-{triggered.get('run_attempt', 1)}-{triggered['conclusion']}"
            notify(repo, webhook, workflow, key, f"Run concluded **{triggered['conclusion']}**.", triggered, state, state_path)
        if metadata['state'] != 'active':
            incident, reason, run = 'disabled', 'Scheduled workflow is disabled.', None
        else:
            incident, reason, run = assess(workflow, runs, now, metadata['created_at'])
        notify(repo, webhook, workflow, incident, reason, run, state, state_path)


if __name__ == '__main__':
    main()
