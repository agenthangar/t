"""Essential selection stays exact and cannot pass with skipped dependencies."""
from types import SimpleNamespace

import pytest

from scripts import smoke


def manifest():
    return dict(exitstatus=0, full=list(smoke.NODES), selected=list(smoke.NODES),
                outcomes=dict.fromkeys(smoke.NODES, 'passed'))


def test_smoke_selection_is_bounded_and_fail_closed():
    assert 15 <= len(smoke.NODES) <= 31
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


@pytest.mark.parametrize('scope,outcome', [
    ('discovery', 'passed'), ('terminal', 'passed'), ('native', 'passed'),
    ('discovery', 'skipped'), ('terminal', 'failed'), ('native', 'exit'),
])
def test_macos_runner_uses_exact_scope_and_fails_closed(tmp_path, monkeypatch, scope, outcome):
    import json
    monkeypatch.setattr(smoke, 'ROOT', tmp_path)
    nodes = smoke.MACOS_NODES[scope]
    assert nodes

    def run(argv, **kwargs):
        assert kwargs == {'cwd': tmp_path}
        assert argv[:6] == [smoke.sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.ci_shards']
        assert argv[-len(nodes):] == list(nodes)
        assert all(not arg.startswith('--cov') for arg in argv)
        assert ('--run-macos-ui' in argv) is (scope == 'native')
        manifest_arg = next(arg for arg in argv if arg.startswith('--ci-manifest='))
        destination = tmp_path / manifest_arg.split('=', 1)[1]
        assert not destination.exists()
        data = dict(exitstatus=0, full=list(nodes), selected=list(nodes),
                    outcomes=dict.fromkeys(nodes, 'passed'))
        if outcome in ('skipped', 'failed'):
            data['outcomes'][nodes[0]] = outcome
        destination.write_text(json.dumps(data))
        return SimpleNamespace(returncode=2 if outcome == 'exit' else 0)
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    if outcome in ('skipped', 'failed'):
        with pytest.raises(ValueError):
            smoke.main(macos_scope=scope)
    else:
        assert smoke.main(macos_scope=scope) == (2 if outcome == 'exit' else 0)


def test_macos_cli_accepts_only_named_scopes():
    result = smoke.subprocess.run(
        [smoke.sys.executable, str(smoke.ROOT / 'scripts/smoke.py'), '--macos', 'all'],
        capture_output=True, text=True)
    assert result.returncode == 2
    assert 'invalid choice' in result.stderr
    assert all(scope in result.stderr for scope in ('discovery', 'terminal', 'native'))
