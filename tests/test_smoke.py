"""Essential selection stays exact and cannot pass with skipped dependencies."""
from types import SimpleNamespace

import pytest

from scripts import smoke


def manifest():
    return dict(exitstatus=0, full=list(smoke.NODES), selected=list(smoke.NODES),
                outcomes=dict.fromkeys(smoke.NODES, 'passed'))


def test_smoke_selection_is_bounded_and_fail_closed():
    assert 15 <= len(smoke.NODES) <= 30
    assert len(set(smoke.NODES)) == len(smoke.NODES)
    smoke.check_manifest(manifest())
    for change in ('missing', 'new', 'skipped', 'failed', 'exit', 'collection'):
        data = manifest()
        if change == 'missing':
            data['selected'].pop()
        elif change == 'new':
            data['full'].append('tests/test_future.py::test_new')
        elif change in ('skipped', 'failed'):
            data['outcomes'][smoke.NODES[0]] = change
        elif change == 'exit':
            data['exitstatus'] = 1
        else:
            data['full'].pop()
        with pytest.raises(ValueError, match='every exact'):
            smoke.check_manifest(data)


def test_smoke_command_uses_only_explicit_nodes_and_clears_old_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, 'ROOT', tmp_path)
    destination = tmp_path / 'artifacts/smoke/manifest.json'
    destination.parent.mkdir(parents=True)
    destination.write_text('old success')
    def run(argv, **kwargs):
        assert argv[-len(smoke.NODES):] == list(smoke.NODES)
        assert '--cov' not in argv and '--run-macos-ui' not in argv
        assert not destination.exists()
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    assert smoke.main() == 2
