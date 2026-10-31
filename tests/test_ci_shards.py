"""Audit the CI scheduler and fail-closed artifact handling without running suites."""
import json

import pytest

from scripts import ci_shards as ci


def manifest(shard=0, selected=None):
    selected = selected or [f'tests/test_{shard}.py::test_ok']
    return dict(shard=shard, count=2, legacy=False, commit='abc',
                full=['tests/test_0.py::test_ok', 'tests/test_1.py::test_ok'],
                selected=selected, outcomes={node: 'passed' for node in selected},
                exitstatus=0, elapsed=1, durations={node: 1 for node in selected})


def artifacts(tmp_path):
    from coverage import CoverageData
    for index in range(2):
        folder = tmp_path / f'shard-{index}'
        ci.write_json(folder / 'manifest.json', manifest(index))
        data = CoverageData(basename=str(folder / f'.coverage.{index}'))
        data.add_lines({str(ci.ROOT / 'bin/t'): {1, 2}})
        data.write()
    return tmp_path


def test_balancing_is_deterministic_and_whole_file():
    weights = {'a': 8, 'b': 7, 'c': 3, 'd': 2}
    assert ci.assignments(reversed(list(weights)), weights, 2) == [['a', 'd'], ['b', 'c']]
    assert ci.assignments(['b', 'a', 'new'], {'a': 2, 'b': 2}, 2) == [['a', 'new'], ['b']]
    with pytest.raises(ValueError, match='positive'):
        ci.assignments(['a'], {'a': -1}, 2)


def test_exact_once_reconciliation():
    assert len(ci.reconcile([manifest(0), manifest(1)], 2)) == 2
    for values in ([manifest(0)], [manifest(0), manifest(0)]):
        with pytest.raises(ValueError, match='missing or duplicate'):
            ci.reconcile(values, 2)
    duplicate = manifest(1, manifest(0)['selected'])
    with pytest.raises(ValueError, match='duplicate selected'):
        ci.reconcile([manifest(0), duplicate], 2)
    incomplete = manifest(1)
    incomplete['full'] += ['tests/test_new.py::test_new']
    first = manifest(0)
    first['full'] = incomplete['full']
    with pytest.raises(ValueError, match='incomplete'):
        ci.reconcile([first, incomplete], 2)


@pytest.mark.parametrize('mutation', ['outcomes', 'exitstatus', 'selected', 'commit'])
def test_invalid_manifests(tmp_path, mutation):
    data = manifest()
    if mutation == 'outcomes':
        data['outcomes'] = {}
    elif mutation == 'exitstatus':
        data['exitstatus'] = 1
    elif mutation == 'selected':
        data['selected'] *= 2
    else:
        second = manifest(1)
        second['commit'] = 'different'
        with pytest.raises(ValueError, match='differs'):
            ci.reconcile([data, second], 2)
        return
    ci.write_json(tmp_path / 'manifest.json', data)
    with pytest.raises(ValueError):
        ci.checked_manifest(tmp_path / 'manifest.json')


def test_missing_and_corrupt_coverage(tmp_path):
    directory = artifacts(tmp_path)
    path = directory / 'shard-0/.coverage.0'
    path.unlink()
    with pytest.raises(ValueError, match='missing or duplicate coverage'):
        ci.combine(directory, 2, tmp_path / 'output')
    path.write_text('corrupt sqlite data')
    with pytest.raises(Exception, match="[Dd]ata|database"):
        ci.combine(directory, 2, tmp_path / 'output')


def test_combined_threshold_and_reference(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.delenv('COVERAGE_FILE', raising=False)
    import coverage
    directory = artifacts(tmp_path / 'shards')
    monkeypatch.setattr(coverage.Coverage, 'report', lambda *args, **kwargs: 96.9)
    monkeypatch.setattr(coverage.Coverage, 'xml_report', lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match='below 97'):
        ci.combine(directory, 2, tmp_path / 'output')
    monkeypatch.setattr(coverage.Coverage, 'report', lambda *args, **kwargs: 97.0)
    ci.combine(directory, 2, tmp_path / 'output')
    audit_path = tmp_path / 'output/audit.json'
    ref_dir = tmp_path / 'reference'
    ref = manifest()
    ref.update(shard=None, selected=ref['full'], outcomes={node: 'passed' for node in ref['full']})
    ci.write_json(ref_dir / 'manifest.json', ref)
    from coverage import CoverageData
    data = CoverageData(basename=str(ref_dir / '.coverage.reference'))
    data.add_lines({str(ci.ROOT / 'bin/t'): {1, 2}})
    data.write()
    ci.compare(audit_path, ref_dir)
    audit = ci.read_json(audit_path)
    audit['measured']['bin/t'] = [1]
    ci.write_json(audit_path, audit)
    with pytest.raises(ValueError, match='executed lines differ'):
        ci.compare(audit_path, ref_dir)
    audit['outcomes'][ref['full'][0]] = 'skipped'
    ci.write_json(audit_path, audit)
    with pytest.raises(ValueError, match='outcomes'):
        ci.compare(audit_path, ref_dir)


def test_refresh_uses_all_phases_per_file(tmp_path):
    data = manifest()
    data['durations'] = {data['selected'][0]: 2.5}
    ci.write_json(tmp_path / 'run/shard-0/manifest.json', data)
    output = tmp_path / 'weights.json'
    ci.refresh(tmp_path, output)
    assert json.loads(output.read_text()) == {'tests/test_0.py': 2.5}


def test_opt_in_collection_and_report_accounting(monkeypatch):
    from types import SimpleNamespace
    options = {'--ci-shard': 0, '--ci-shards': 2, '--ci-legacy': False}
    deselections = []
    config = SimpleNamespace(
        _ci_audit={'outcomes': {}, 'durations': {}},
        getoption=options.__getitem__,
        hook=SimpleNamespace(pytest_deselected=lambda **kwargs: deselections.extend(kwargs['items'])))
    items = [SimpleNamespace(nodeid=node) for node in
             ['a.py::test_one', 'a.py::test_two', 'b.py::test_one']]
    monkeypatch.setattr(ci, 'read_json', lambda path: {'a.py': 2, 'b.py': 1})
    ci.pytest_collection_modifyitems(config, items)
    assert [item.nodeid for item in items] == ['a.py::test_one', 'a.py::test_two']
    assert len(config._ci_audit['full']) == 3
    assert [item.nodeid for item in deselections] == ['b.py::test_one']
    monkeypatch.setattr(ci, '_state', config._ci_audit, raising=False)
    for phase in ('setup', 'call', 'teardown'):
        ci.pytest_runtest_logreport(SimpleNamespace(
            nodeid=items[0].nodeid, duration=0.5, when=phase,
            failed=False, skipped=False, passed=True))
    assert config._ci_audit['durations'][items[0].nodeid] == 1.5
    assert config._ci_audit['outcomes'][items[0].nodeid] == 'passed'


@pytest.mark.parametrize(('shard', 'count', 'legacy'), [(-1, 4, False), (4, 4, False), (0, 0, False), (0, 4, True)])
def test_invalid_collection_configuration(shard, count, legacy):
    from types import SimpleNamespace
    options = {'--ci-shard': shard, '--ci-shards': count, '--ci-legacy': legacy}
    config = SimpleNamespace(_ci_audit={}, getoption=options.__getitem__)
    with pytest.raises(ValueError):
        ci.pytest_collection_modifyitems(config, [SimpleNamespace(nodeid='a.py::test_one')])


def test_legacy_collection_keeps_entire_files():
    from types import SimpleNamespace
    options = {'--ci-shard': None, '--ci-shards': 4, '--ci-legacy': True}
    config = SimpleNamespace(_ci_audit={}, getoption=options.__getitem__,
                             hook=SimpleNamespace(pytest_deselected=lambda **kwargs: None))
    nodes = [name + suffix for name in ci.LEGACY for suffix in ('::test_a', '::test_b')]
    items = [SimpleNamespace(nodeid=node) for node in nodes + ['other.py::test_other']]
    ci.pytest_collection_modifyitems(config, items)
    assert sorted(item.nodeid for item in items) == sorted(nodes)
    assert len(config._ci_audit['full']) == len(nodes) + 1
