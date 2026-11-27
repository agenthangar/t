"""Routine smoke and exact-commit full validation have separate gates."""
import json
import re
from pathlib import Path

import pytest

from scripts import ci_policy as policy

ROOT = Path(__file__).resolve().parents[1]


def test_routine_scope_ignores_changed_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['ci_policy.py', 'scope'])
    output = tmp_path / 'output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('FORCE_FULL', 'false')
    monkeypatch.setenv('PR_BASE', 'base')
    # A changed-path query would fail: only HEAD identity is needed now.
    def git(argv, **kwargs):
        assert argv == ['git', 'rev-parse', 'HEAD']
        return 'a' * 40 + '\n'
    monkeypatch.setattr(policy.subprocess, 'check_output', git)
    for path in ('bin/t', 'libexec/t_history.py', '.github/workflows/ci.yml', 'tests/conftest.py'):
        monkeypatch.setenv('CHANGED_PATH', path)
        policy.main()
        assert output.read_text().endswith('full=false\n')
    assert 'Essential smoke checks' in capsys.readouterr().out
    monkeypatch.setenv('FORCE_FULL', 'true')
    policy.main()
    assert output.read_text().endswith('full=true\n')


def results(full=True, macos=False, reference=False):
    data = {key: {'result': 'success'} for key in (
        'scope', 'cli-contract', 'design-adherence', 'shell', 'pytest', 'smoke', 'fzf-legacy', 'coverage',
        'reference', 'verify-shards', 'macos', 'playground', 'gitleaks', 'pip-audit', 'codeql')}
    skipped = {'smoke'} if full else {'pytest', 'fzf-legacy', 'coverage', 'design-adherence'}
    if not macos:
        skipped.add('macos')
    if not reference:
        skipped.update(('reference', 'verify-shards'))
    for key in skipped:
        data[key]['result'] = 'skipped'
    return data


def test_required_gate_rejects_failures_and_skips():
    for full, macos, reference in ((False, False, False), (True, False, False), (True, True, True), (False, True, False)):
        data = results(full, macos, reference)
        policy.check_results(data, full=full, macos=macos, reference=reference)
        for job in data:
            if data[job]['result'] == 'skipped':
                continue
            for outcome in ('failure', 'cancelled', 'skipped'):
                changed = {**data, job: {'result': outcome}}
                with pytest.raises(ValueError):
                    policy.check_results(changed, full=full, macos=macos, reference=reference)
            with pytest.raises(ValueError, match='missing'):
                policy.check_results({k: v for k, v in data.items() if k != job}, full=full,
                                     macos=macos, reference=reference)


def test_workflow_modes_preserve_nightly_release_and_security_boundaries():
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    smoke = workflow.split('  smoke:\n', 1)[1].split('  ci:\n', 1)[0]
    assert 'run: python3 scripts/smoke.py' in smoke
    assert "if: needs.scope.outputs.full != 'true'" in smoke
    assert 'ref: ${{ needs.scope.outputs.sha }}' in smoke
    assert '--ci-fast' not in workflow and 'PR_BASE:' not in workflow
    assert "FORCE_FULL: ${{ inputs.ref != '' || (github.event_name == 'workflow_dispatch'" in workflow
    for job in ('pytest', 'coverage', 'fzf-legacy', 'design-adherence'):
        body = re.split(r'\n  [a-z-]+:\n', workflow.split(f'  {job}:\n', 1)[1])[0]
        assert "if: needs.scope.outputs.full == 'true'" in body
    assert 'scripts/ci_shards.py combine shards' in workflow
    assert 'if percentage < 97:' in (ROOT / 'scripts/ci_shards.py').read_text()
    assert "if: github.event_name == 'workflow_dispatch' && inputs.run_macos" in workflow
    release = (ROOT / '.github/workflows/release.yml').read_text()
    validate = release.split('  validate:\n')[1].split('  archive:\n')[0]
    assert 'if:' not in validate  # Includes nights with no release tag.
    assert 'ref: ${{ needs.plan.outputs.sha }}' in validate
    assert 'needs.validate.outputs.sha == needs.plan.outputs.sha' in release
    assert 'name: release-validation' in release
    health = (ROOT / '.github/workflows/scheduled-health.yml').read_text()
    assert 'workflows: [Releases, Scorecard]' in health
    assert 'scripts/scheduled_health.py' in health


def test_only_successful_full_gate_writes_same_attempt_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['ci_policy.py', 'gate'])
    for name, value in dict(EXPECT_FULL='false', EXPECT_MACOS='false', EXPECT_REFERENCE='false',
                            TESTED_SHA='a' * 40, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2').items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('NEEDS_JSON', json.dumps(results(False)))
    policy.main()
    path = Path('validation/validation.json')
    assert not path.exists()
    monkeypatch.setenv('EXPECT_FULL', 'true')
    failed = results(True)
    failed['coverage']['result'] = 'failure'
    monkeypatch.setenv('NEEDS_JSON', json.dumps(failed))
    with pytest.raises(ValueError):
        policy.main()
    assert not path.exists()
    monkeypatch.setenv('NEEDS_JSON', json.dumps(results(True)))
    policy.main()
    assert json.loads(path.read_text()) == dict(sha='a'*40, full=True, run_id='123', run_attempt='2')


def test_macos_dispatch_is_scoped_and_independent_of_full_linux():
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    inputs = workflow.split('  workflow_dispatch:\n', 1)[1].split('  push:\n', 1)[0]
    def input_body(name):
        return re.split(r'\n      [a-z_]+:\n', inputs.split(f'      {name}:\n', 1)[1])[0]
    assert 'type: boolean' in input_body('run_macos')
    assert 'default: false' in input_body('run_macos')
    assert 'type: boolean' in input_body('run_full')
    assert 'default: false' in input_body('run_full')
    scope = input_body('macos_scope')
    assert 'type: choice' in scope and 'default: discovery' in scope
    assert re.findall(r'^\s+- (\w+)\s*$', scope, re.M) == ['discovery', 'terminal', 'native']
    force = next(line for line in workflow.splitlines() if 'FORCE_FULL:' in line)
    assert 'inputs.run_full' in force and 'inputs.verify_shards' in force
    assert "inputs.ref != ''" in force
    assert 'inputs.run_macos' not in force and 'inputs.macos_scope' not in force
    macos = re.split(r'\n  [a-z-]+:\n', workflow.split('  macos:\n', 1)[1])[0]
    assert 'runs-on: macos-latest' in macos
    assert "if: github.event_name == 'workflow_dispatch' && inputs.run_macos" in macos
    assert "fzf: '0.74.5'" in macos
    assert 'matrix' not in macos
    assert 'MACOS_SCOPE: ${{ inputs.macos_scope }}' in macos
    assert 'python3 scripts/smoke.py --macos "$MACOS_SCOPE"' in macos
    assert 'pytest' not in macos and '--cov' not in macos and '--run-macos-ui' not in macos
