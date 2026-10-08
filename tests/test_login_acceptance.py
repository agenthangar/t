"""Acceptance runner contract tests use fake vendor CLIs, never real accounts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_script
from test_login import cli_sandbox

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner():
    return load_script(ROOT / 'scripts/login-acceptance.py', 'login_acceptance_tests')


def test_inspection_isolated_cli_report_has_no_vendor_output(runner, tmp_path, monkeypatch):
    env = cli_sandbox(tmp_path)
    binary = tmp_path / 'bin' / 'codex'
    binary.write_text(binary.read_text().replace("if 'status' in sys.argv:",
        "if '--version' in sys.argv: print('codex-cli 1.2.3'); sys.exit(0)\nif 'status' in sys.argv:"))
    # Load t only after replacing HOME; no access to the executing account.
    for key in list(runner.os.environ):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    report = tmp_path / 'report.json'
    assert runner.main(['--agents', 'codex', '--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['acceptance_complete'] is False
    checks = {c['check']: c['status'] for c in data['agents'][0]['checks']}
    assert checks == {'vendor_status': 'passed', 'dry_run': 'passed', 'ignore': 'passed',
                      'login': 'skipped', 'model_request': 'skipped'}
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert calls == ['codex login status', 'codex --version', 'codex login status']
    assert str(tmp_path) not in report.read_text()
    assert report.stat().st_mode & 0o777 == 0o600


def test_missing_binary_is_blocked_not_success(runner, tmp_path, monkeypatch):
    module = SimpleNamespace(_login_probe=lambda _: {'state': 'missing', 'bin': None})
    record = runner.inspect_agent(module, 'codex', SimpleNamespace(login=False, request=False), tmp_path)
    assert runner.report_exit({'agents': [record]}) == 2
    assert record['checks'][0]['reason'] == 'expected_vendor_binary_missing'


def test_report_never_overwrites_credentials_or_follows_symlink(runner, tmp_path):
    private = tmp_path / 'auth.json'
    private.write_text('fixture')
    link = tmp_path / 'report'
    link.symlink_to(private)
    for path in (private, link):
        with pytest.raises(FileExistsError):
            runner.write_report(path, {})
    assert private.read_text() == 'fixture'


def test_no_interactive_login_without_terminal(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(runner, 'load_t', lambda: pytest.fail('must reject before probes'))
    with pytest.raises(SystemExit) as error:
        runner.main(['--login', '--report', str(tmp_path / 'report.json')])
    assert error.value.code == 2


def test_failed_requests_and_partial_coverage_are_explicit(runner, tmp_path, monkeypatch):
    probes = iter([{'state': 'logged-in', 'bin': '/fixture/codex', 'expires': 100},
                   {'state': 'logged-in', 'bin': '/fixture/codex', 'expires': 200}])
    module = SimpleNamespace(_login_probe=lambda _: next(probes),
                             _login_decision=lambda *a: (True, ''))
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        if '--version' in argv:
            return 0, 'codex-cli 1.2.3 private@example.invalid'
        if '--ignore' in argv:
            return 0, 'codex: ignored for this invocation'
        if '--dry-run' in argv:
            return 0, 'codex: expiring\n  + codex login'
        return 0, 'secret raw vendor output'
    monkeypatch.setattr(runner, 'run', run)
    record = runner.inspect_agent(module, 'codex', SimpleNamespace(login=True, headless=True, request=True), tmp_path)
    assert record['cached_deadline_advanced'] is True
    assert record['version'] == '1.2.3'
    assert record['checks'][-1]['status'] == 'failed'  # no final-response file
    assert runner.report_exit({'agents': [record]}) == 1
    assert 'secret' not in json.dumps(record) and '@' not in json.dumps(record)
    assert any('--headless' in argv and kw.get('interactive') for argv, kw in calls)
    assert any('--sandbox' in argv and 'read-only' in argv for argv, kw in calls)


@pytest.mark.parametrize('agent', ['claude', 'codex', 'cursor'])
def test_request_commands_do_not_bypass_permissions(runner, tmp_path, agent):
    argv = runner.request_argv(agent, '/fixture/' + agent, tmp_path / 'response')
    assert runner.PROMPT in argv
    assert not any('dangerously' in flag or flag in ('--force', '--yolo') for flag in argv)


def test_probe_timeout_is_sanitized(runner, tmp_path, monkeypatch):
    def timeout(*a, **kw):
        raise runner.subprocess.TimeoutExpired('private-command', 120, output='secret')
    monkeypatch.setattr(runner.subprocess, 'run', timeout)
    assert runner.run(['fixture'], cwd=tmp_path) == (124, '')


def test_coverage_variables_are_not_forwarded(runner, monkeypatch):
    monkeypatch.setenv('COV_CORE_SOURCE', 'fixture')
    assert 'COV_CORE_SOURCE' not in runner.environment()


def test_interruption_still_writes_incomplete_report(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'load_t', lambda: object())
    monkeypatch.setattr(runner, 'run', lambda *a, **kw: (0, 'a' * 40))
    def interrupt(*a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, 'inspect_agent', interrupt)
    report = tmp_path / 'interrupted.json'
    assert runner.main(['--report', str(report)]) == 130
    data = json.loads(report.read_text())
    assert data['interrupted'] and not data['acceptance_complete']


def test_request_success_checks_final_response_not_echoed_prompt(runner, tmp_path, monkeypatch):
    probe = {'state': 'logged-in', 'bin': '/fixture/codex', 'expires': None}
    module = SimpleNamespace(_login_probe=lambda _: probe, _login_decision=lambda *a: (False, ''))
    def run(argv, **kw):
        if '--version' in argv:
            return 0, 'codex-cli 1.2.3'
        if '--ignore' in argv:
            return 0, 'codex: ignored for this invocation'
        if '--dry-run' in argv:
            return 0, 'codex: logged in; expiry unknown'
        Path(argv[argv.index('--output-last-message') + 1]).write_text(runner.MARKER)
        return 0, 'unrecorded transcript'
    monkeypatch.setattr(runner, 'run', run)
    result = runner.inspect_agent(module, 'codex', SimpleNamespace(login=False, request=True), tmp_path)
    assert result['checks'][-1]['status'] == 'passed'
    assert 'unrecorded' not in json.dumps(result)
