#!/usr/bin/env python3
"""Opt-in vendor acceptance checks for t login; never run live accounts in PR CI."""

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ('claude', 'codex', 'cursor')
MARKER = 'T_LOGIN_ACCEPTANCE_OK'
PROMPT = 'Reply exactly ' + MARKER + '. Do not use tools or inspect files.'


def load_t():
    loader = SourceFileLoader('t_login_acceptance', str(ROOT / 'bin/t'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.update(T_NO_UPDATE_CHECK='1', T_RECOVERY_DISABLE='1')
    return env


def run(argv, *, cwd, interactive=False):
    """Captured output stays in memory; never include it in reports or errors."""
    try:
        if interactive:
            return subprocess.run(argv, cwd=cwd, env=environment()).returncode, ''
        result = subprocess.run(argv, cwd=cwd, env=environment(), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired:
        return 124, ''
    except (OSError, UnicodeError):
        return 127, ''


def request_argv(agent, executable, response):
    if agent == 'claude':
        return [executable, '-p', PROMPT, '--output-format', 'text', '--tools', '',
                '--disallowedTools', 'mcp__*', '--strict-mcp-config', '--mcp-config', '{}',
                '--no-session-persistence', '--setting-sources', '']
    if agent == 'codex':
        return [executable, 'exec', '--skip-git-repo-check', '--sandbox', 'read-only',
                '--ephemeral', '--ignore-user-config', '--ignore-rules',
                '--output-last-message', str(response), PROMPT]
    return [executable, '--print', '--mode', 'ask', '--sandbox', 'enabled',
            '--output-format', 'text', PROMPT]


def check(rows, name, status, reason):
    # name/status/reason are program constants, never arbitrary vendor output.
    rows.append({'check': name, 'status': status, 'reason': reason})


def expiry_bucket(probe):
    expiry = probe.get('expires')
    if expiry is None:
        return 'unknown'
    return 'expired' if expiry <= time.time() else 'within-24h' if expiry <= time.time() + 86400 else 'later'


def inspect_agent(module, agent, args, work):
    rows = []
    before = module._login_probe(agent)
    record = {'agent': agent, 'before': before['state'],
              'expiry_before': expiry_bucket(before), 'checks': rows}
    executable = before.get('bin')
    if not executable:
        check(rows, 'installed', 'blocked', 'expected_vendor_binary_missing')
        return record
    rc, output = run([executable, '--version'], cwd=work)
    # Never use a vendor's arbitrary fallback version string (could contain PII).
    match = re.search(r'(?<![\w@])\d+(?:\.\d+){1,3}(?![\w@])', output)
    record['version'] = match.group(0) if rc == 0 and match else 'unknown'
    check(rows, 'vendor_status', 'passed' if before['state'] in ('logged-in', 'logged-out') else 'failed',
          'recognized_status' if before['state'] in ('logged-in', 'logged-out') else 'unrecognized_or_failed_status')
    t = [sys.executable, str(ROOT / 'bin/t'), 'login', agent]
    rc, output = run(t + ['--dry-run'], cwd=work)
    planned, _ = module._login_decision(before, time.time(), 24)
    expected_rc = 1 if before['state'] == 'error' else 0
    ok = rc == expected_rc and output.startswith(agent + ': ') and ('\n  + ' in output) == planned
    check(rows, 'dry_run', 'passed' if ok else 'failed', 'plan_matches_observed_state' if ok else 'unexpected_plan_or_state_changed')
    rc, output = run(t + ['--ignore', agent, '--dry-run'], cwd=work)
    ok = rc == 0 and output.strip() == agent + ': ignored for this invocation'
    check(rows, 'ignore', 'passed' if ok else 'failed', 'ignored_plan' if ok else 'unexpected_ignore_result')
    if args.login:
        print(f'{agent}: starting explicitly authorized login; complete vendor prompts.', flush=True)
        rc, _ = run(t + ['--force', '-y'] + (['--headless'] if args.headless else []),
                    cwd=work, interactive=True)
        after = module._login_probe(agent)
        record['after'] = after['state']
        record['expiry_after'] = expiry_bucket(after)
        ok = rc == 0 and after['state'] == 'logged-in'
        check(rows, 'login', 'passed' if ok else 'failed', 'login_and_status_succeeded' if ok else 'login_or_status_failed')
        old, new = before.get('expires'), after.get('expires')
        # Even a later deadline alone does not prove a usable refreshed session.
        record['cached_deadline_advanced'] = new > old if old is not None and new is not None else None
    else:
        after = before
        check(rows, 'login', 'skipped', 'requires_login_flag_and_terminal')
    if args.request:
        if after['state'] != 'logged-in':
            check(rows, 'model_request', 'blocked', 'not_logged_in')
        else:
            response = work / 'response.txt'
            rc, output = run(request_argv(agent, executable, response), cwd=work)
            if agent == 'codex':
                try:
                    output = response.read_text()
                except (OSError, UnicodeError):
                    output = ''
            ok = rc == 0 and output.strip() == MARKER
            check(rows, 'model_request', 'passed' if ok else 'failed',
                  'expected_response_received' if ok else 'request_failed_or_unexpected_response')
    else:
        check(rows, 'model_request', 'skipped', 'requires_request_flag_may_use_paid_quota')
    return record


def report_exit(report):
    statuses = [c['status'] for agent in report['agents'] for c in agent['checks']]
    if 'failed' in statuses:
        return 1
    return 2 if 'blocked' in statuses else 0


def write_report(path, report):
    # Refuse overwrite/symlinks: accidental credential paths must remain untouched.
    with path.open('x', encoding='utf-8') as file:
        os.chmod(path, 0o600)
        json.dump(report, file, indent=2)
        file.write('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', choices=AGENTS, default=list(AGENTS))
    parser.add_argument('--report', type=Path, required=True, help='new sanitized JSON report file (never overwritten)')
    parser.add_argument('--login', action='store_true', help='authorize real re-login; requires a terminal and may need browser approval')
    parser.add_argument('--headless', action='store_true', help='use vendor headless flows with --login')
    parser.add_argument('--request', action='store_true', help='authorize one small model request per logged-in tool; may use paid quota')
    args = parser.parse_args(argv)
    if args.login and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.error('--login requires an interactive terminal')
    if args.headless and not args.login:
        parser.error('--headless requires --login')
    if args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir():
        parser.error('--report must name a new file in an existing directory')
    module = load_t()
    rc, revision = run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], cwd=ROOT)
    report = {'schema': 1, 'evidence': 'installed-vendor-clis', 'platform': sys.platform,
              'revision': revision.strip() if rc == 0 and re.fullmatch(r'[0-9a-f]{40}\n?', revision) else 'unknown',
              'agents': [], 'acceptance_complete': False,
              'remaining': ['natural_expiry_and_automatic_refresh_not_proven',
                            'browser_and_headless_flows_need_separate_runs',
                            'native_credential_store_requires_macos_run',
                            'vendor_cancellation_requires_operator_observation']}
    try:
        for agent in dict.fromkeys(args.agents):
            with tempfile.TemporaryDirectory(prefix='t-login-acceptance-') as temporary:
                report['agents'].append(inspect_agent(module, agent, args, Path(temporary)))
        code = report_exit(report)
    except KeyboardInterrupt:
        report['interrupted'] = True
        code = 130
    report['exit_code'] = code
    try:
        write_report(args.report, report)
    except OSError:
        print('Acceptance report could not be written; no raw output was saved.', file=sys.stderr)
        return 2
    print('Sanitized report written. Exit status covers requested automated checks, not full live acceptance.')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
