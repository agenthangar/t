"""Notify the configured maintainer of failed or overdue scheduled workflows.

Run from trusted main, with contents:read, actions:read and issues:write. Only
GitHub API metadata is read; source-run artifacts and logs are never executed.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess


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


def issue_marker(workflow):
    return f'<!-- t scheduled health: {workflow} -->'


def notify(repo, recipient, workflow, incident, reason, run, issues):
    marker = issue_marker(workflow)
    existing = next((i for i in issues if not i.get('pull_request')
                     and i.get('user', {}).get('login') == 'github-actions[bot]'
                     and marker in (i.get('body') or '')), None)
    if incident == 'pending':
        return
    if incident is None:
        if existing and existing['state'] == 'open':
            url = run['html_url'] if run else f'https://github.com/{repo}/actions'
            api(repo, f"issues/{existing['number']}/comments", method='POST', payload={
                'body': f'Recovered: [latest successful run]({url}). Scheduled execution is within its deadline.'})
            api(repo, f"issues/{existing['number']}", method='PATCH', payload={'state': 'closed', 'state_reason': 'completed'})
            existing['state'] = 'closed'
        return
    event_marker = f'<!-- t health event: {incident} -->'
    if existing and event_marker in (existing.get('body') or ''):
        return
    name = WORKFLOWS.get(workflow, ('Alert delivery test', None))[0]
    lines = [marker, event_marker, '', f'@{recipient} — **{name} needs attention.**', '', reason]
    if run:
        lines += ['', f"[Workflow run]({run['html_url']})", f"Commit: `{run['head_sha']}`",
                  f"Attempt: {run.get('run_attempt', 1)}"]
        jobs = api(repo, f"actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs?per_page=100", paginate=True)
        failed = [j for page in jobs for j in page['jobs'] if j.get('conclusion') in FAILURES]
        lines += [f"- [{j['name']}]({j['html_url']}): {j['conclusion']}" for j in failed]
    lines += ['', 'This issue is updated once per incident/run attempt and closes after recovery.',
              'GitHub notification delivery follows your personal notification settings.']
    body = '\n'.join(lines)
    if existing:
        api(repo, f"issues/{existing['number']}", method='PATCH', payload={
            'state': 'open', 'body': body, 'assignees': [recipient]})
        api(repo, f"issues/{existing['number']}/comments", method='POST', payload={'body': body})
        existing.update(state='open', body=body)
    else:
        created = api(repo, 'issues', method='POST', payload={
            'title': f'[CI alert] {name}', 'body': body, 'assignees': [recipient]})
        issues.append(created)
    print(f'{name}: alert delivered to @{recipient}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='send a clearly labeled delivery test')
    args = parser.parse_args()
    repo = os.environ['GITHUB_REPOSITORY']
    recipient = os.environ['CI_ALERT_ASSIGNEE']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', recipient):
        raise ValueError('A repository and one GitHub alert recipient are required')
    pages = api(repo, 'issues?state=all&per_page=100', paginate=True)
    issues = [issue for page in pages for issue in page]
    if args.test:
        notify(repo, recipient, 'delivery-test', 'delivery-test-' + os.environ['GITHUB_RUN_ID'],
               '**Delivery test only.** No nightly or weekly failure is being reported.', None, issues)
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
            notify(repo, recipient, workflow, key, f"Run concluded **{triggered['conclusion']}**.", triggered, issues)
        if metadata['state'] != 'active':
            incident, reason, run = 'disabled', 'Scheduled workflow is disabled.', None
        else:
            incident, reason, run = assess(workflow, runs, now, metadata['created_at'])
        notify(repo, recipient, workflow, incident, reason, run, issues)


if __name__ == '__main__':
    main()
