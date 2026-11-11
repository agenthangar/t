"""Fast selection cannot accidentally omit new tests or accept skipped gates."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_policy as policy, ci_shards as shards


@pytest.mark.parametrize('path,full', [
    ('README.md', False), ('docs/releases.md', False),
    ('libexec/t_usage.py', False), ('libexec/t_lifecycle.py', True),
    ('libexec/t_codex_ownership.py', True), ('bin/t', True),
    ('ui/fzf.sh', True), ('tests/new_helper.py', True),
    ('tests/test_usage.py', False), ('tests/test_future.py', False),
    ('tests/conftest.py', True), ('tests/test_onboarding_pty.py', True),
    ('tests/test_history_picker.py', True), ('tests/test_ci_shards.py', True),
    ('.github/workflows/ci.yml', True), ('scripts/release.py', True),
])
def test_risky_changes_expand_validation(path, full):
    assert policy.requires_full([path]) is full


def test_fast_selection_keeps_unknown_tests_and_real_safety_cases():
    assert policy.fast_node('tests/test_future.py::test_future')
    assert policy.fast_node('tests/test_session_lifecycle.py::test_concurrent_alias_opens_keep_one_worktree_owner')
    assert policy.fast_node('tests/test_session_lifecycle.py::test_public_remote_open_uses_login_shell_and_reattaches[codex]')
    assert not policy.fast_node('tests/test_lifecycle_transfer_e2e.py::test_move')
    assert not policy.fast_node('tests/test_session_lifecycle.py::test_public_resume_revives_saved_thread_once[codex]')


def test_fast_collection_records_omissions_and_is_exclusive():
    options = {'--ci-shard': None, '--ci-shards': 4, '--ci-legacy': False, '--ci-fast': True}
    omitted = []
    config = SimpleNamespace(_ci_audit={}, getoption=options.__getitem__,
                             hook=SimpleNamespace(pytest_deselected=lambda **kw: omitted.extend(kw['items'])))
    nodes = ['tests/test_future.py::test_it', 'tests/test_lifecycle_transfer_e2e.py::test_move']
    items = [SimpleNamespace(nodeid=node, get_closest_marker=lambda name: None) for node in nodes]
    shards.pytest_collection_modifyitems(config, items)
    assert [item.nodeid for item in items] == nodes[:1]
    assert [item.nodeid for item in omitted] == nodes[1:]
    assert config._ci_audit['full'] == sorted(nodes)
    options['--ci-legacy'] = True
    with pytest.raises(ValueError, match='exclusive'):
        shards.pytest_collection_modifyitems(config, items)


def test_fast_shards_partition_all_eligible_cases_once(monkeypatch):
    monkeypatch.setattr(shards, 'read_json', lambda path: {'tests/test_new.py': 2, 'tests/test_other.py': 1})
    nodes = ['tests/test_new.py::test_one', 'tests/test_other.py::test_two',
             'tests/test_lifecycle_transfer_e2e.py::test_deferred', 'tests/test_picker_ui.py::test_real']
    selections = []
    for index in range(2):
        options = {'--ci-shard': index, '--ci-shards': 2, '--ci-legacy': False, '--ci-fast': True}
        config = SimpleNamespace(_ci_audit={}, getoption=options.__getitem__,
                                 hook=SimpleNamespace(pytest_deselected=lambda **kw: None))
        items = [SimpleNamespace(nodeid=node, get_closest_marker=lambda name, n=node: n.endswith('test_real')) for node in nodes]
        shards.pytest_collection_modifyitems(config, items)
        selections.extend(item.nodeid for item in items)
    assert sorted(selections) == sorted(nodes[:2])


def results(full=True, macos=False, reference=False):
    data = {key: {'result': 'success'} for key in (
        'scope', 'cli-contract', 'shell', 'pytest', 'smoke', 'fzf-legacy', 'coverage',
        'reference', 'verify-shards', 'macos', 'playground', 'gitleaks', 'pip-audit', 'codeql')}
    skipped = {'smoke'} if full else {'pytest', 'fzf-legacy', 'coverage'}
    if not macos:
        skipped.add('macos')
    if not reference:
        skipped.update(('reference', 'verify-shards'))
    for key in skipped:
        data[key]['result'] = 'skipped'
    return data


@pytest.mark.parametrize('full,macos,reference', [(False, False, False), (True, False, False), (True, True, True)])
def test_required_gate_rejects_failures_cancellation_missing_and_unexpected_skips(full, macos, reference):
    data = results(full, macos, reference)
    policy.check_results(data, full=full, macos=macos, reference=reference)
    for outcome in ('failure', 'cancelled', 'skipped'):
        data['playground']['result'] = outcome
        with pytest.raises(ValueError):
            policy.check_results(data, full=full, macos=macos, reference=reference)
    del data['playground']
    with pytest.raises(ValueError, match='missing'):
        policy.check_results(data, full=full, macos=macos, reference=reference)


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
    monkeypatch.setenv('NEEDS_JSON', json.dumps(results(True)))
    policy.main()
    assert json.loads(path.read_text()) == dict(sha='a'*40, full=True, run_id='123', run_attempt='2')
