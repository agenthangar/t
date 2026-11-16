"""Native trust reads never create or broaden decisions."""
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def trust(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    (tmp_path / '.codex').mkdir()
    return runpy.run_path(str(ROOT / 'libexec/t_trust_state.py'))


def test_linked_worktrees_inherit_only_canonical_decisions(trust, tmp_path):
    canonical = tmp_path / 'repo'
    common = canonical / '.git'
    private = common / 'worktrees/slot'
    private.mkdir(parents=True)
    (common / 'config').write_text('[core]\n')
    (private / 'commondir').write_text('../..\n')
    workspace = tmp_path / 'slots/slot'
    workspace.mkdir(parents=True)
    (workspace / '.git').write_text('gitdir: ' + str(private) + '\n')
    config = tmp_path / '.codex/config.toml'
    config.write_text('[projects.' + json.dumps(str(canonical)) + ']\ntrust_level="trusted"\n')
    assert trust['trusted']('codex', workspace)
    assert trust['covered']('codex', workspace)
    config.write_text(config.read_text() + '[projects.' + json.dumps(str(workspace)) + ']\ntrust_level="untrusted"\n')
    assert not trust['trusted']('codex', workspace)
    assert trust['covered']('codex', workspace)
    config.write_text('[projects.' + json.dumps(str(tmp_path)) + ']\ntrust_level="trusted"\n')
    assert not trust['covered']('codex', workspace)
    (tmp_path / '.claude.json').write_text(json.dumps({'projects': {str(canonical): {'hasTrustDialogAccepted': True}}}))
    assert trust['trusted']('claude', workspace)
    assert trust['roots'](canonical) == (canonical, canonical)
    (private / 'commondir').unlink()
    assert trust['roots'](workspace) == (workspace, workspace)
    (workspace / '.git').write_text('invalid')
    assert trust['roots'](workspace) == (workspace, workspace)


def test_missing_foreign_and_plain_directory_decisions(trust, tmp_path):
    workspace = tmp_path / 'plain'
    workspace.mkdir()
    config = tmp_path / '.codex/config.toml'
    assert trust['roots'](workspace) == (workspace, workspace)
    assert not trust['covered']('codex', workspace)
    assert not trust['covered']('claude', workspace)
    assert not trust['covered']('unknown', workspace)
    for text in ('invalid toml', 'projects = 1'):
        config.write_text(text)
        assert not trust['covered']('codex', workspace)
    config.write_text('[projects.' + json.dumps(str(workspace)) + ']\ntrust_level="trusted"\n')
    assert trust['trusted']('codex', workspace)
    (tmp_path / '.claude.json').write_text('{}')
    assert not trust['covered']('claude', workspace)


def test_claude_uses_selected_config_store(trust, tmp_path, monkeypatch):
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    trusted = json.dumps({'projects': {str(workspace): {'hasTrustDialogAccepted': True}}})
    (tmp_path / '.claude.json').write_text(trusted)
    custom = tmp_path / 'custom'
    custom.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(custom))
    assert not trust['covered']('claude', workspace)
    (custom / '.claude.json').write_text(trusted)
    assert trust['covered']('claude', workspace)
