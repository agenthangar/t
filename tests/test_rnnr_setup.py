import json
from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_rnnr_install_and_status_routes(t_mod):
    for args in (["system", "apps", "install", "rnnr"], ["install", "rnnr"]):
        parsed = t_mod.build_parser().parse_args(args + ["--dry-run"])
        assert parsed.app == "rnnr" and parsed.dry_run
        assert t_mod._command_readonly(args + ["--dry-run"])
        assert not t_mod._command_readonly(args + ["--yes"])
    assert t_mod._command_readonly(["system", "apps", "status", "rnnr", "--json"])


@pytest.mark.parametrize("entry,expected", [
    ({"enabled": False}, "disabled"),
    ({"command": "/custom", "args": ["mcp"]}, "custom"),
    ({"command": "/usr/bin/false", "args": []}, "custom"),
])
def test_setup_preserves_external_registrations(entry, expected, monkeypatch):
    setup = runpy.run_path(str(ROOT / "apps/rnnr/integration.py"))
    monkeypatch.setattr(setup["shutil"], "which", lambda name: "/test/client")
    monkeypatch.setitem(setup["state"].__globals__, "registration", lambda *args: entry)
    monkeypatch.setitem(setup["state"].__globals__, "run", lambda *args: pytest.fail("unexpected command"))
    assert setup["state"]("codex", "connect") == expected


def test_rnnr_dry_run_and_status_do_not_install(t_mod, tmp_path, monkeypatch, capsys):
    module = t_mod._rnnr_module()
    monkeypatch.setitem(module["command"].__globals__, "APP", tmp_path / "rnnr.app")
    monkeypatch.setitem(module["command"].__globals__, "install", lambda: pytest.fail("unexpected install"))
    for words in (["install", "rnnr", "--dry-run"], ["system", "apps", "status", "rnnr", "--json"]):
        assert module["command"](t_mod.build_parser().parse_args(words), None) == 0
        output = capsys.readouterr().out
        if "--json" in words:
            assert json.loads(output) == {"installed": False, "version": None}
        else:
            assert "brew install --cask agenthangar/tap/rnnr" in output
