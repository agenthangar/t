"""Session defaults: safe persistence, picker choices, and save/cancel behavior."""

import io
import stat
from types import SimpleNamespace

import pytest


def defaults(agent, models):
    return {"DEV_AGENT_DEFAULT": agent, **{f"DEV_MODEL[{a}]": models.get(a, "") for a in ("claude", "codex")}}


def test_config_bridge_reads_model_defaults(t_mod, tmp_path, monkeypatch):
    path = tmp_path / "config.sh"
    path.write_text("DEV_AGENT_DEFAULT=codex\nDEV_AGENT[api]=claude\n"
                    "DEV_MODEL[claude]=sonnet\\[1m\\]\nDEV_MODEL[codex]=local/model\n")
    monkeypatch.setattr(t_mod, "CONFIG", str(path))
    cfg = t_mod.Config()
    assert cfg.models == {"claude": "sonnet[1m]", "codex": "local/model"}
    assert cfg.agent_for("web") == "codex"
    assert cfg.agent_for("api") == "claude"


def test_config_managed_block_preserves_shell_and_is_idempotent(t_mod):
    original = '# my config\nDEV_AGENT_DEFAULT=claude\nsource "$HOME/private.zsh"'
    models = {"claude": "sonnet[1m]", "codex": "local/model"}
    updated = t_mod._config_text(original, defaults("codex", models))
    assert updated.startswith(original + "\n\n")
    assert "DEV_MODEL[claude]='sonnet[1m]'" in updated
    assert t_mod._config_text(updated, defaults("codex", models)) == updated
    # Hand-written content appended later is preserved; our latest choice goes last.
    updated += "DEV_AGENT_DEFAULT=claude\n"
    cleared = t_mod._config_text(updated, defaults("claude", {}))
    assert cleared.count(t_mod._CONFIG_BEGIN) == 1
    assert cleared.count("DEV_AGENT_DEFAULT=claude") == 3
    assert "DEV_MODEL[claude]=''\nDEV_MODEL[codex]=''" in cleared
    assert "local/model" not in cleared


@pytest.mark.parametrize("text", [
    "# >>> t config defaults >>>\n", "# <<< t config defaults <<<\n",
    "# <<< t config defaults <<<\n# >>> t config defaults >>>\n",
    "# >>> t config defaults >>>\n# >>> t config defaults >>>\n# <<< t config defaults <<<\n",
])
def test_config_refuses_broken_markers(t_mod, text):
    with pytest.raises(ValueError, match="malformed"):
        t_mod._config_text(text, defaults("claude", {}))


@pytest.mark.parametrize("model", ["a b", "$(touch /tmp/no)", "x\ny", "--help", "a;exit", "\x1b[31m"])
def test_config_rejects_non_model_input(t_mod, model):
    with pytest.raises(ValueError, match="model IDs|control characters"):
        t_mod._config_text("", defaults("claude", {"claude": model}))


def test_config_refuses_unknown_tool(t_mod):
    with pytest.raises(ValueError, match="choose claude or codex"):
        t_mod._config_text("", defaults("cursor", {}))


def test_config_atomic_write_preserves_symlink_mode_and_concurrent_edits(t_mod, tmp_path, monkeypatch):
    target = tmp_path / "private.zsh"
    target.write_text("original\n")
    target.chmod(0o640)
    link = tmp_path / "local"
    link.symlink_to(target)
    t_mod._config_write(str(link), "original\n", "saved\n")
    assert link.is_symlink() and target.read_text() == "saved\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    with pytest.raises(ValueError, match="changed while"):
        t_mod._config_write(str(link), "original\n", "overwrite\n")
    assert target.read_text() == "saved\n"
    # A failed replacement leaves the old file intact and cleans the temp file.
    def fail(*args):
        raise OSError("read-only")
    monkeypatch.setattr(t_mod.os, "replace", fail)
    with pytest.raises(OSError):
        t_mod._config_write(str(link), "saved\n", "overwrite\n")
    assert target.read_text() == "saved\n" and not list(tmp_path.glob(".t-config-*"))


def test_config_creates_missing_local_file(t_mod, tmp_path):
    target = tmp_path / "local"
    assert t_mod._config_read(target) == ""
    t_mod._config_write(str(target), "", "new\n")
    assert target.read_text() == "new\n" and stat.S_IMODE(target.stat().st_mode) == 0o600


class Menu:
    def __init__(self, picks, inputs=(), pages=("y",)):
        self.picks, self.inputs, self.pages = iter(picks), iter(inputs), iter(pages)
        self.out = io.StringIO()
        self.restored = False

    def intro(self, *args):
        pass

    def raw(self):
        pass

    def restore(self):
        self.restored = True

    def pick(self, label, rows, default=0, **kwargs):
        pick = next(self.picks)
        assert pick is None or pick in dict(rows), (label, pick, rows)
        assert 0 <= default < len(rows)
        return pick

    def cooked_input(self, prompt, **kwargs):
        return next(self.inputs)

    def page(self, *args, **kwargs):
        return next(self.pages)

    def done(self, *args):
        pass

    def commit(self, lines):
        self.out.write("\n".join(lines))


@pytest.mark.parametrize('before,after,expected', [
    ({'old': 'alice@mini'}, {'renamed': 'alice@mini'}, []),
    ({'old': 'mini'}, {'old': 'mini'}, []),
    ({'old': 'mini'}, {}, []),
    ({'old': 'mini'}, {'old': 'mini.tail', 'other': 'mini.tail'}, ['mini.tail']),
    ({'old': 'mini'}, {'new': 'alice@mini', 'ip': '100.64.0.2'}, ['alice@mini', '100.64.0.2']),
])
def test_host_verification_uses_new_actual_targets(t_mod, before, after, expected):
    assert t_mod._host_new_targets(before, after) == expected


@pytest.mark.parametrize('tty,term,no_color', [(False, 'xterm', False), (True, 'xterm', False),
                                             (True, 'dumb', False), (True, 'xterm', True)])
def test_native_host_verification_inherits_streams_and_continues_after_failure(
        t_mod, monkeypatch, capsys, tty, term, no_color):
    monkeypatch.setenv('TERM', term)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    calls = []
    def run(argv, **kwargs):
        assert kwargs == {}, 'native SSH must inherit terminal input/output'
        calls.append(argv)
        if argv[-2] == 'offline':
            raise OSError('network unavailable')
        return SimpleNamespace(returncode=255 if argv[-2] == 'refused' else 0)
    monkeypatch.setattr(t_mod.subprocess, 'run', run)
    monkeypatch.setattr(t_mod.sys.stderr, 'isatty', lambda: tty)
    t_mod._host_verify_targets(['refused', 'offline', 'user@good'])
    assert calls == [['ssh', '-T', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=no',
                      '-o', 'StrictHostKeyChecking=ask', '--', target, 'true']
                     for target in ['refused', 'offline', 'user@good']]
    err = capsys.readouterr().err
    assert 'SSH exited 255' in err and 'network unavailable' in err
    assert 'Registration is saved' in err and 'user@good' not in err
    colored = tty and term != 'dumb' and not no_color
    assert ('\x1b[' in err) == colored
    if colored:
        assert '\x1b[33m' in err


@pytest.mark.parametrize('status', [130, -2, None])
def test_native_host_verification_interrupt_stops_batch(t_mod, monkeypatch, status):
    calls = []
    def run(argv):
        calls.append(argv[-2])
        if status is None:
            raise KeyboardInterrupt
        return SimpleNamespace(returncode=status)
    monkeypatch.setattr(t_mod.subprocess, 'run', run)
    with pytest.raises(KeyboardInterrupt):
        t_mod._host_verify_targets(['first', 'second'])
    assert calls == ['first']


@pytest.mark.parametrize('answer', ['verify', 'later', None])
def test_post_save_offer_restores_ui_before_native_ssh(t_mod, monkeypatch, answer):
    ui = Menu([answer])
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    calls = []
    def verify(targets):
        assert ui.restored
        calls.append(targets)
    monkeypatch.setattr(t_mod, '_host_verify_targets', verify)
    if answer is None:
        with pytest.raises(KeyboardInterrupt):
            t_mod._host_verify_offer(['new'])
    else:
        t_mod._host_verify_offer(['new'])
    assert ui.restored
    assert calls == ([['new']] if answer == 'verify' else [])


@pytest.mark.parametrize('answer,expected', [('y', True), ('', False), ('q', None)])
def test_verification_offer_uses_plain_prompt_if_raw_unavailable(t_mod, monkeypatch, answer, expected):
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    ui = Menu([])
    def fail():
        raise OSError('raw unavailable')
    ui.raw = fail
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    monkeypatch.setattr('builtins.input', lambda _: answer)
    calls = []
    monkeypatch.setattr(t_mod, '_host_verify_targets', calls.append)
    if expected is None:
        with pytest.raises(KeyboardInterrupt):
            t_mod._host_verify_offer(['new'])
    else:
        t_mod._host_verify_offer(['new'])
    assert calls == ([['new']] if expected else [])
    assert ui.restored


def test_host_verification_offer_skips_empty_or_nonterminal(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    def forbidden():
        pytest.fail('machine mode opened a picker')
    monkeypatch.setattr(t_mod, '_RailUI', forbidden)
    t_mod._host_verify_offer(['new'])
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    t_mod._host_verify_offer([])


@pytest.fixture
def config_cli(t_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(t_mod, "CONFIG", str(tmp_path / "config.sh"))
    local = tmp_path / "local"
    local.write_text("# keep me\n")
    monkeypatch.setattr(t_mod, "ZSHRC_LOCAL", str(local))
    monkeypatch.setenv("T_LOCAL_RC", str(local))
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(t_mod, "_config_live_models", lambda agent: [
        {"value": "opus", "resolvedModel": "claude-test-opus", "supportsEffort": True,
         "supportedEffortLevels": ["low", "high", "max"], "supportsFastMode": True},
        {"value": "haiku", "resolvedModel": "claude-test-haiku"},
    ] if agent == "claude" else [
        {"model": "test-model", "serviceTiers": [{"id": "priority"}], "supportedReasoningEfforts": [{"reasoningEffort": "high"},
                                                               {"reasoningEffort": "ultra"}]}])
    monkeypatch.setattr(t_mod, "zsh_capture", lambda snippet: "")
    monkeypatch.setattr(t_mod, "_host_discovery", lambda: ([], []))
    monkeypatch.setattr(t_mod, "_host_verify_targets", lambda targets: pytest.fail('unexpected native SSH'))
    return t_mod.Config(), local


@pytest.mark.parametrize('change,expected', [
    ('rename', []), ('default', []), ('target', ['mini.tail.test']),
    ('duplicate', ['mini.tail.test']),
])
def test_config_verification_delta_is_offered_after_save_and_sync(t_mod, config_cli, monkeypatch, change, expected):
    import copy
    cfg, local = config_cli
    cfg.hosts = {'mini': 'mini'}
    edited = copy.deepcopy(cfg)
    if change == 'rename':
        edited.hosts = {'renamed': 'mini'}
    elif change == 'default':
        edited.beam_host = 'mini'
    else:
        edited.hosts = {'mini': 'mini.tail.test'}
        if change == 'duplicate':
            edited.hosts['another'] = 'mini.tail.test'
    monkeypatch.setattr(t_mod, '_config_wizard', lambda cfg: edited)
    synced = []
    monkeypatch.setattr(t_mod, 'zsh_capture', synced.append)
    offers = []
    def offer(targets):
        assert synced == ['_t_sync_config']
        assert local.read_text() != '# keep me\n'
        offers.append(targets)
    monkeypatch.setattr(t_mod, '_host_verify_offer', offer)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert offers == [expected]


def test_local_config_resolves_env_cache_then_fallback(t_mod, tmp_path, monkeypatch):
    bridge = tmp_path / "config.sh"
    fallback = tmp_path / "legacy.local"
    cached = tmp_path / "custom.local"
    explicit = tmp_path / "override.local"
    monkeypatch.setattr(t_mod, "CONFIG", str(bridge))
    monkeypatch.setattr(t_mod, "ZSHRC_LOCAL", str(fallback))
    monkeypatch.delenv("T_LOCAL_RC", raising=False)
    assert t_mod.Config().local_rc == str(fallback)
    bridge.write_text(f"T_LOCAL_RC={cached}\n")
    assert t_mod.Config().local_rc == str(cached)
    assert t_mod._current_local_rc() == str(cached)
    monkeypatch.setenv("T_LOCAL_RC", str(explicit))
    assert t_mod.Config().local_rc == str(explicit)
    assert t_mod._current_local_rc() == str(explicit)


def test_config_menu_save_then_cancel(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(["tool", "codex", "codex", "model", "__custom__", "back", "claude", "model", "opus", "back", "save"], ["local/model"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert ui.restored
    text = local.read_text()
    assert text.startswith("# keep me\n") and "DEV_AGENT_DEFAULT=codex" in text
    assert "DEV_MODEL[codex]=local/model" in text and "DEV_MODEL[claude]=opus" in text
    ui = Menu(["tool", "claude", "cancel"])
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert local.read_text() == text and ui.restored


def test_config_menu_custom_validation_back_reset_and_no_change(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    cfg.models = {"codex": "old"}
    ui = Menu(["tool", None, "codex", "model", "__custom__", "__custom__", "model", "", "back", "save"],
              ["bad model", ""])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert "DEV_MODEL[codex]=''" in local.read_text()
    before = local.stat()
    cfg.models = {}
    ui = Menu(["save"])
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert local.stat() == before and ui.restored


def test_config_show_and_non_tty_never_write(t_mod, config_cli, monkeypatch, capsys):
    cfg, local = config_cli
    cfg.agents = {"api": "codex"}
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: False)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=True)) == 0
    assert "api=codex" in capsys.readouterr().out
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 1
    assert "terminal" in capsys.readouterr().err
    assert local.read_text() == "# keep me\n"


def test_config_main_menu_shows_repo_aliases(t_mod, config_cli, monkeypatch):
    cfg, _ = config_cli
    cfg.repos = {"t": "/code/t", "api": "/code/my-api"}
    ui = Menu(["cancel"])
    def pick(label, rows, **kwargs):
        assert label == "Local settings"
        assert kwargs["values"]["repos"] == "api, t"
        assert [key for key, _ in rows] == [
            "tool", "mode", "trust", *t_mod.AGENTS, "local", "workflow",
            "repos", "worktrees", "hosts", "beam", "edit", "save", "cancel",
        ]
        assert kwargs["sections"] == {
            "tool": "Session defaults", t_mod.AGENTS[0]: "Agents", "workflow": "Workflow",
            "repos": "Repositories & worktrees", "hosts": "Remote hosts", "edit": "Finish",
        }
        collections = {*t_mod.AGENTS, "local", "workflow", "repos", "worktrees", "hosts"}
        assert {key for key, text in rows if text.endswith(" ›")} == collections
        return "cancel"
    ui.pick = pick
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130


def test_config_group_navigation_retains_focus(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(["workflow", "repos", "worktrees", "hosts", "cancel"])
    pick = ui.pick
    previous = "tool"
    visited = []
    def choose(label, rows, **kwargs):
        nonlocal previous
        assert rows[kwargs["default"]][0] == previous
        previous = pick(label, rows, **kwargs)
        return previous
    ui.pick = choose
    for key in ("workflow", "repos", "worktrees", "hosts"):
        monkeypatch.setattr(t_mod, "_config_" + key,
                            lambda ui, cfg, key=key: visited.append(key))
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert visited == ["workflow", "repos", "worktrees", "hosts"]
    assert local.read_text() == "# keep me\n" and ui.restored


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_config_grouped_menu_narrow_navigation(t_mod, config_cli, monkeypatch, width, height):
    from test_tui import make_ui
    cfg, local = config_cli
    cfg.repos = {"long-repository-alias-" + str(i): "/code/repo-" + str(i) for i in range(8)}
    cfg.models = {agent: "long-model-name-with-context-window" for agent in t_mod.AGENTS}
    before = t_mod._config_state(cfg)
    # Visit each row in both directions without entering settings or saving.
    moves = len(t_mod.AGENTS) + 12
    ui = make_ui(t_mod, ["j"] * moves + ["k"] * moves + ["q"], width, height)
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod._config_wizard(cfg) is None
    assert ui.restored and t_mod._config_state(cfg) == before
    assert local.read_text() == "# keep me\n"
    # Exclude the separate cancellation summary from the navigation frames.
    frames = [frame for frame in ui.frames if any("Local settings" in line for line in frame)]
    assert len(frames) == 2 * moves + 1
    for frame in frames:
        assert sum(t_mod._term_rows(line, width) for line in frame) <= height
        assert all(t_mod._vis_len(line) <= width for line in frame)
        assert any("▸" in line for line in frame)
        assert any("SAVE CHANGES" in line for line in frame)


def test_config_menu_interrupt_restores_terminal(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu([])
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    ui.pick = interrupt
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert ui.restored and local.read_text() == "# keep me\n"


def test_config_removals_survive_other_edits_and_setup_can_reregister(t_mod):
    original = "REMOTE_HOSTS[retired]=old.example\nDEV_REPOS[api]=/code/api\nexport TBEAM_HOST=old.example\n"
    updated = t_mod._config_text(original, {"REMOTE_HOSTS[retired]": None,
                                "DEV_REPOS[api]": None, "TBEAM_HOST": ""})
    assert t_mod._local_entries(updated) == ({}, {}, False)
    again = t_mod._config_text(updated, {"DEV_MODEL[claude]": "opus"})
    assert t_mod._local_entries(again) == ({}, {}, False)
    # t setup appends a replacement after the overlay. A later unrelated save
    # reconciles the old tombstone with that effective registration.
    again += "REMOTE_HOSTS[retired]=new.example\n"
    final = t_mod._config_text(again, {"DEV_AGENT_DEFAULT": "codex"},
                              {"REMOTE_HOSTS[retired]": "new.example", "DEV_MODEL[claude]": "opus"})
    assert t_mod._local_entries(final) == ({}, {"retired": "new.example"}, False)
    assert "DEV_MODEL[claude]=opus" in final


def test_config_preserves_existing_defaults_blocks_and_rejects_custom_shell(t_mod):
    text = t_mod._config_text("", defaults("codex", {"codex": "local/model"}))
    text = t_mod._config_text(text, {"REMOTE_HOSTS[mini]": "user@mini.example"})
    assert "DEV_MODEL[codex]=local/model" in text
    assert "REMOTE_HOSTS[mini]=user@mini.example" in text
    with pytest.raises(ValueError, match="custom shell"):
        t_mod._config_text(text.replace("DEV_AGENT_DEFAULT=codex", "source private.zsh"), {})


@pytest.mark.parametrize("key,value", [("PATH", "/tmp"), ("REMOTE_HOSTS[$(id)]", "host"),
                                       ("DEV_REPOS[api]", "/x\ny"), ("DEV_BRANCH", 3)])
def test_config_rejects_unsafe_settings(t_mod, key, value):
    with pytest.raises(ValueError):
        t_mod._config_text("", {key: value})


def test_config_host_removal_clears_related_defaults(t_mod, config_cli):
    cfg, _ = config_cli
    cfg.hosts = {"retired": "old.example", "mini": "mini.example"}
    cfg.beam_host, cfg.mini_host = "retired", "old.example"
    t_mod._config_remove_host(cfg, "retired")
    assert cfg.hosts == {"mini": "mini.example"} and cfg.beam_host == cfg.mini_host == ""
    cfg.beam_host, cfg.mini_host = "stale.example", "legacy.example"
    t_mod._config_remove_host(cfg, "mini")
    assert not cfg.hosts and cfg.beam_host == cfg.mini_host == ""


def test_config_hosts_remove_save_and_cancel(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    cfg.hosts = {"retired": "old.example", "mini": "mini.example"}
    cfg.beam_host = "old.example"
    cfg.mini_host = "old.example"
    local.write_text("REMOTE_HOSTS[retired]=old.example\nREMOTE_HOSTS[mini]=mini.example\n"
                     "export TBEAM_HOST=old.example\nMINI_HOST=old.example\n")
    before = local.read_text()
    ui = Menu(["hosts", "retired", "remove", "__back__", "cancel", "discard"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert local.read_text() == before and "retired" in cfg.hosts
    ui = Menu(["hosts", "retired", "remove", "__back__", "save"])
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert t_mod._local_entries(local.read_text()) == ({}, {"mini": "mini.example"}, False)
    assert "unset MINI_HOST" in local.read_text()


def test_config_hosts_add_edit_and_default(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(["hosts", "__add__", "__manual__", "lab", "beam", "lab", "target", "__back__", "save", "later"],
              ["old.example", "lab", "new.example"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    text = local.read_text()
    assert "REMOTE_HOSTS[lab]=new.example" in text and "export TBEAM_HOST=new.example" in text


def test_host_discovery_loads_current_peers_with_alternative_endpoints(t_mod, monkeypatch):
    results = iter([([], []), ([{'alias': 'new', 'target': 'new.tail.test',
                               'addresses': ['100.64.0.3']}], [])])
    calls = []
    def discover(*, include_addresses):
        assert include_addresses is True
        return next(results)
    monkeypatch.setattr(t_mod.runpy, 'run_path',
                        lambda path: calls.append(path) or {'tailscale_hosts': discover})
    assert t_mod._host_discovery() == ([], [])
    assert t_mod._host_discovery()[0][0]['addresses'] == ['100.64.0.3']
    assert len(calls) == 2 and all(path.endswith('/libexec/t_discovery.py') for path in calls)


@pytest.mark.parametrize("target", [
    "mini.tail.test", "user@MINI.TAIL.TEST.", "mini", "dev@100.64.0.3",
    "user@[FD7A:115C:A1E0::1]",
])
def test_host_candidates_exclude_registered_peer_endpoints(t_mod, target):
    peers = [{"alias": "mini", "target": "mini.tail.test",
              "addresses": ["mini.tail.test", "mini", "100.64.0.3", "fd7a:115c:a1e0::1"]},
             {"alias": "new", "target": "new.tail.test", "addresses": ["100.64.0.4"]}]
    registered = {"existing": target}
    assert t_mod._host_candidates(peers, registered) == [{"alias": "new", "target": "new.tail.test"}]
    assert registered == {"existing": target}


def test_host_candidates_disambiguate_aliases_without_overwriting(t_mod):
    peers = [{"alias": "mini", "target": "first.tail.test",
              "addresses": ["mini", "100.64.0.3"]},
             {"alias": "mini", "target": "second.tail.test",
              "addresses": ["mini", "100.64.0.4"]},
             {"alias": "__add__", "target": "third.tail.test"},
             {"alias": "__back__", "target": "fourth.tail.test"}]
    registered = {"mini": "old.tail.test", "mini-2": "older.tail.test"}
    candidates = t_mod._host_candidates(peers, registered)
    assert [host['alias'] for host in candidates[:2]] == ['mini-3', 'mini-4']
    assert len({host['alias'] for host in candidates}) == len(candidates)
    assert all(host['alias'] not in {*registered, '__add__', '__back__'} for host in candidates)
    assert [host['target'] for host in candidates] == [host['target'] for host in peers]
    assert registered == {"mini": "old.tail.test", "mini-2": "older.tail.test"}


def test_config_add_host_discovers_new_peers_each_visit(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    first = {"alias": "first", "target": "first.tail.test"}
    second = {"alias": "second", "target": "second.tail.test"}
    results = iter([([first], []), ([first, second], [])])
    calls, displayed = [], []
    monkeypatch.setattr(t_mod, "_host_discovery", lambda: calls.append('discover') or next(results))
    ui = Menu(['__add__', 'tailscale:first.tail.test', '__add__',
               'tailscale:second.tail.test', '__back__'])
    pick = ui.pick
    def record(label, rows, **kwargs):
        if label == 'Add host':
            displayed.append(dict(rows))
        return pick(label, rows, **kwargs)
    ui.pick = record
    t_mod._config_hosts(ui, cfg)
    assert calls == ['discover', 'discover']
    assert 'tailscale:first.tail.test' in displayed[0]
    assert 'tailscale:first.tail.test' not in displayed[1]
    assert 'tailscale:second.tail.test' in displayed[1]
    assert cfg.hosts == {'first': 'first.tail.test', 'second': 'second.tail.test'}
    assert local.read_text() == '# keep me\n'


def test_config_add_host_refresh_finds_peer_that_appears_while_open(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    results = iter([([], []), ([{'alias': 'new', 'target': 'new.tail.test'}], [])])
    calls = []
    monkeypatch.setattr(t_mod, '_host_discovery', lambda: calls.append('discover') or next(results))
    ui = Menu(['__add__', '__refresh__', 'tailscale:new.tail.test', '__back__'])
    t_mod._config_hosts(ui, cfg)
    assert calls == ['discover', 'discover']
    assert cfg.hosts == {'new': 'new.tail.test'}
    assert local.read_text() == '# keep me\n'


@pytest.mark.parametrize('back', [None, '__back__'])
def test_config_add_host_back_returns_to_hosts_without_changes(t_mod, config_cli, monkeypatch, back):
    cfg, local = config_cli
    monkeypatch.setattr(t_mod, '_host_discovery',
                        lambda: ([{'alias': 'new', 'target': 'new.tail.test'}], []))
    ui = Menu(['__add__', back, '__back__'])
    t_mod._config_hosts(ui, cfg)
    assert cfg.hosts == {} and local.read_text() == '# keep me\n'


@pytest.mark.parametrize('save', [False, True])
def test_config_discovered_host_requires_review_and_can_be_discarded(t_mod, config_cli, monkeypatch, save):
    cfg, local = config_cli
    before = local.read_bytes()
    monkeypatch.setattr(t_mod, '_host_discovery',
                        lambda: ([{'alias': 'new', 'target': 'new.tail.test'}], []))
    tail = ['save', 'later'] if save else ['cancel', 'discard']
    ui = Menu(['hosts', '__add__', 'tailscale:new.tail.test', '__back__', *tail])
    reviews = []
    def review(label, lines, *args, **kwargs):
        assert local.read_bytes() == before
        assert cfg.hosts == {}, 'staged host leaked into original config'
        reviews.append(label)
        assert any('REMOTE_HOSTS[new]' in line and 'new.tail.test' in line for line in lines)
        assert any('Verify SSH now / Later' in line for line in lines)
        return 'enter'
    ui.page = review
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == (0 if save else 130)
    assert ui.restored
    if save:
        assert reviews == ['Review local settings']
        assert t_mod._local_entries(local.read_text())[1] == {'new': 'new.tail.test'}
    else:
        assert reviews == [] and local.read_bytes() == before
    assert cfg.hosts == {}


def test_config_host_review_back_then_discard_never_writes(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    before = local.read_bytes()
    monkeypatch.setattr(t_mod, '_host_discovery',
                        lambda: ([{'alias': 'new', 'target': 'new.tail.test'}], []))
    ui = Menu(['hosts', '__add__', 'tailscale:new.tail.test', '__back__',
               'save', 'cancel', 'discard'], pages=['esc'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert local.read_bytes() == before and cfg.hosts == {} and ui.restored


def test_config_add_host_unavailable_discovery_keeps_manual_entry(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    message = 'Tailscale is not connected; hosts can be added later.'
    monkeypatch.setattr(t_mod, '_host_discovery', lambda: ([], [message]))
    ui = Menu(['__add__', '__manual__', '__back__'], ['user@lab.local', 'lab'])
    notices = []
    ui.done = lambda label, value: notices.append(value)
    t_mod._config_hosts(ui, cfg)
    assert message in notices
    assert cfg.hosts == {'lab': 'user@lab.local'}
    assert local.read_text() == '# keep me\n'


def test_config_add_host_discovery_error_keeps_manual_entry(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    def unavailable():
        raise OSError('Tailscale stopped while refreshing')
    monkeypatch.setattr(t_mod, '_host_discovery', unavailable)
    ui = Menu(['__add__', '__manual__', '__back__'], ['user@lab.local', 'lab'])
    t_mod._config_hosts(ui, cfg)
    assert cfg.hosts == {'lab': 'user@lab.local'}
    assert local.read_text() == '# keep me\n'


@pytest.mark.parametrize('inputs', [('',), ('lab.local', ''), ('lab.local', 'taken'), ('lab.local', '__back__')])
def test_config_manual_host_cancel_or_conflict_keeps_existing_hosts(t_mod, config_cli, inputs):
    cfg, local = config_cli
    cfg.hosts = {'taken': 'old.tail.test'}
    ui = Menu(['__add__', '__manual__', '__back__'], inputs)
    t_mod._config_hosts(ui, cfg)
    assert cfg.hosts == {'taken': 'old.tail.test'}
    assert local.read_text() == '# keep me\n'


def test_config_host_discovery_interrupt_restores_terminal_without_writes(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    def interrupt():
        raise KeyboardInterrupt
    monkeypatch.setattr(t_mod, '_host_discovery', interrupt)
    ui = Menu(['hosts', '__add__'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert ui.restored and cfg.hosts == {} and local.read_text() == '# keep me\n'


def test_explicit_host_commands_and_config_show_never_discover(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    monkeypatch.setattr(t_mod, '_host_discovery', lambda: pytest.fail('explicit command discovered peers'))
    monkeypatch.setattr(t_mod, '_host_verify_offer', lambda targets: pytest.fail('explicit command prompted for SSH'))
    monkeypatch.setattr(t_mod, '_host_refresh_config', lambda: None)
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    assert t_mod.cmd_hosts(cfg, SimpleNamespace(action='add', alias='lab', target='lab.local')) == 0
    before = local.read_bytes()
    assert t_mod.cmd_hosts(cfg, SimpleNamespace(action='list', json=True)) == 0
    assert t_mod.cmd_hosts(cfg, SimpleNamespace(action='show', alias='lab')) == 0
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=True)) == 0
    assert local.read_bytes() == before


def test_config_repos_edit_overrides_unregister_and_add(t_mod, config_cli, monkeypatch, tmp_path):
    cfg, local = config_cli
    import subprocess
    web = tmp_path / "web"
    subprocess.run(["git", "init", "-q", str(web)], check=True)
    monkeypatch.setattr(t_mod, "_new_sh_whence", lambda alias: None)
    cfg.repos = {"api": "/old/api", "retired": "/old/retired"}
    cfg.agents = {"retired": "codex"}
    cfg.branches = {"retired": "main"}
    cfg.worktree = {"retired": "0"}
    ui = Menu(["repos", "api", "tool", "codex", "worktree", "0",
               "branch", "custom", "path", "back", "retired", "remove",
               "__add__", "__back__", "save"], ["dev/custom", str(tmp_path / "new api"), str(web), "web"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    text = local.read_text()
    assert "DEV_AGENT[api]=codex" in text and "DEV_WORKTREE[api]=0" in text
    assert "DEV_BRANCHES[api]=dev/custom" in text and f"DEV_REPOS[web]={web}" in text
    for key in ("DEV_REPOS", "DEV_AGENT", "DEV_BRANCHES", "DEV_WORKTREE"):
        assert f"unset '{key}[retired]'" in text


def test_config_worktree_defaults_and_beam_clear(t_mod, config_cli, monkeypatch, tmp_path):
    cfg, local = config_cli
    cfg.beam_host = "old.example"
    ui = Menu(["worktrees", "enabled", "0", "root", "branch", "back", "beam", "", "save"],
              [str(tmp_path / "trees"), "dev/shared"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    text = local.read_text()
    assert "DEV_WORKTREE_DEFAULT=0" in text and "DEV_BRANCH=dev/shared" in text
    assert "unset TBEAM_HOST" in text


def test_config_register_suggests_path_and_alias_and_can_cancel(t_mod, config_cli, monkeypatch, tmp_path):
    import subprocess
    cfg, local = config_cli
    repo = tmp_path / "config" / "scripts"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(t_mod, "HOME", str(tmp_path))
    monkeypatch.setattr(t_mod, "_new_sh_whence", lambda alias: None)
    ui = Menu(["__add__", "__back__"], [str(repo), "scripts"])
    seen = []
    original = ui.cooked_input
    def read(prompt, **kwargs):
        seen.append(kwargs["prefill"])
        return original(prompt, **kwargs)
    ui.cooked_input = read
    t_mod._config_repos(ui, cfg)
    assert seen == [str(tmp_path / "config"), "scripts"]
    assert cfg.repos == {"scripts": str(repo)}
    assert local.read_text() == "# keep me\n"
    ui = Menu(["__add__", "__add__", "__back__"], ["", str(repo), ""])
    t_mod._config_repos(ui, cfg)
    assert cfg.repos == {"scripts": str(repo)}


def test_config_repo_short_name_saves_overrides_and_reloads(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    cfg.repos = {"long-name": "/code/site"}
    cfg.agents = {"long-name": "codex"}
    cfg.branches = {"long-name": "main"}
    cfg.worktree = {"long-name": "0"}
    cfg.open_mode = {"long-name": "app"}
    before = t_mod._config_state(cfg)
    ui = Menu(["repos", "long-name", "alias", "mode", "app", "back", "__back__", "save"], ["site"])
    original_pick = ui.pick
    labels = []
    def pick(label, rows, **kwargs):
        labels.append(label)
        if label == "Repository · long-name":
            assert dict(rows)["alias"] == "Short name"
            assert kwargs["values"]["alias"] == "long-name"
        if label == "Repository · site":
            assert kwargs["values"]["alias"] == "site"
        return original_pick(label, rows, **kwargs)
    ui.pick = pick
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert labels[labels.index("Repository · long-name") + 1] == "Repository · site"
    # Saving is an overlay: the old registration must stay removed on reload.
    saved = local.read_text()
    assert "DEV_OPEN_MODE[site]=app" in saved
    assert "unset 'DEV_OPEN_MODE[long-name]'" in saved
    local.write_text(t_mod._config_text("", before) + saved)
    output = t_mod.subprocess.check_output([
        "zsh", "-f", "-c",
        'typeset -A DEV_REPOS DEV_AGENT DEV_BRANCHES DEV_WORKTREE DEV_OPEN_MODE DEV_MODEL DEV_EFFORT DEV_FAST; '
        'source "$1"; print -r -- "${(kv)DEV_REPOS}"; print -r -- "${(kv)DEV_AGENT}"; '
        'print -r -- "${(kv)DEV_BRANCHES}"; print -r -- "${(kv)DEV_WORKTREE}"',
        "test-config", str(local)], text=True, env={"HOME": str(local.parent), "XDG_CONFIG_HOME": str(local.parent)})
    assert output.splitlines() == ["site /code/site", "site codex", "site main", "site 0"]


@pytest.mark.parametrize("new_alias", ["taken", "__add__", "__back__", "bad name", "$(bad)"])
def test_config_repo_rename_rejects_invalid_names_without_changes(t_mod, config_cli, new_alias):
    cfg, _ = config_cli
    cfg.repos = {"site": "/code/site", "taken": "/code/other"}
    before = t_mod._config_state(cfg)
    with pytest.raises(ValueError):
        t_mod._config_rename_repo(cfg, "site", new_alias)
    assert t_mod._config_state(cfg) == before


@pytest.mark.parametrize("new_alias", ["", "site", "taken", "__add__", "bad name", "web"])
def test_config_repo_short_name_cancel_discards_changes(t_mod, config_cli, monkeypatch, new_alias):
    cfg, local = config_cli
    cfg.repos = {"site": "/code/site", "taken": "/code/other"}
    before = t_mod._config_state(cfg)
    ui = Menu(["repos", "site", "alias", "back", "__back__", "cancel", "discard"], [new_alias])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert t_mod._config_state(cfg) == before
    assert local.read_text() == "# keep me\n"


def test_config_review_can_back_out_or_cancel(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(["tool", "codex", "save", "save", "cancel", "discard"], pages=["n", "q"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert local.read_text() == "# keep me\n"


def test_config_editor_uses_argument_list_and_checks_syntax(t_mod, monkeypatch):
    monkeypatch.setenv("VISUAL", "my-editor --wait")
    calls = []
    monkeypatch.setattr(t_mod.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert t_mod._config_editor() == 0
    assert calls == [["my-editor", "--wait", t_mod.ZSHRC_LOCAL], ["zsh", "-n", t_mod.ZSHRC_LOCAL]]
    monkeypatch.setattr(t_mod.subprocess, "call", lambda argv: 7)
    assert t_mod._config_editor() == 7


def test_config_editor_requires_pending_changes_to_be_saved(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(["tool", "codex", "edit", "cancel", "discard"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    monkeypatch.setattr(t_mod, "_config_editor", lambda: pytest.fail("must not open editor"))
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130
    assert local.read_text() == "# keep me\n"


def test_config_selects_and_persists_model_specific_effort(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(["claude", "model", "opus", "effort", "max", "back",
               "codex", "model", "test-model", "effort", "ultra", "back", "save"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    text = local.read_text()
    assert "DEV_EFFORT[claude]=max" in text and "DEV_EFFORT[codex]=ultra" in text
    assert "DEV_MODEL[claude]=opus" in text and "DEV_MODEL[codex]=test-model" in text


def test_config_switching_to_non_effort_model_clears_effort(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    cfg.models, cfg.efforts = {"claude": "opus"}, {"claude": "max"}
    ui = Menu(["claude", "model", "haiku", "effort", "", "back", "save"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert "DEV_EFFORT[claude]=''" in local.read_text()


def test_config_model_lookup_is_lazy_and_refreshable(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    calls = []
    def discover(agent):
        calls.append(agent)
        if len(calls) == 1:
            raise ValueError("offline")
        return [{"model": "new-model", "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}]}]
    monkeypatch.setattr(t_mod, "_config_live_models", discover)
    ui = Menu(["cancel"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 130 and not calls
    ui = Menu(["codex", "model", "__refresh__", "new-model", "effort", "ultra", "back", "save"])
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert calls == ["codex", "codex"]  # one retry, then reuse within this menu
    assert "DEV_MODEL[codex]=new-model" in local.read_text()


def test_config_unavailable_metadata_keeps_custom_and_native_choices(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    def unavailable(agent):
        raise FileNotFoundError(agent)
    monkeypatch.setattr(t_mod, "_config_live_models", unavailable)
    ui = Menu(["codex", "model", "__custom__", "effort", "", "back", "save"], ["private/model"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert "DEV_MODEL[codex]=private/model" in local.read_text()


@pytest.mark.parametrize('quit_choice', ['cancel', None])
def test_dirty_exit_keeps_edits_unless_discard_is_explicit(t_mod, config_cli, monkeypatch, quit_choice):
    cfg, local = config_cli
    ui = Menu(['tool', 'codex', quit_choice, None, quit_choice, 'keep', quit_choice, 'save'])
    original_pick = ui.pick
    def pick(label, rows, default=0, **kwargs):
        if 'unsaved' in label:
            assert rows[default][0] == 'keep'
        return original_pick(label, rows, default, **kwargs)
    ui.pick = pick
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert 'DEV_AGENT_DEFAULT=codex' in local.read_text()


def test_fast_mode_persists_per_tool_and_can_be_disabled_or_inherited(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(['claude', 'model', 'opus', 'fast', '1', 'back',
               'codex', 'model', 'test-model', 'fast', 'fast', 'back', 'save'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert 'DEV_FAST[claude]=1' in local.read_text() and 'DEV_FAST[codex]=fast' in local.read_text()
    cfg.fast = {'claude': '1', 'codex': '1'}
    ui = Menu(['claude', 'fast', '0', 'back', 'codex', 'fast', '', 'back', 'save'])
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert 'DEV_FAST[claude]=0' in local.read_text() and "DEV_FAST[codex]=''" in local.read_text()


def test_unsupported_model_does_not_offer_fast_and_switching_disables_it(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    cfg.models, cfg.fast = {'claude': 'opus'}, {'claude': '1'}
    ui = Menu(['claude', 'model', 'haiku', 'fast', '0', 'back', 'save'])
    original_pick = ui.pick
    def pick(label, rows, default=0, **kwargs):
        if 'Fast mode' in label:
            assert '1' not in dict(rows)
        return original_pick(label, rows, default, **kwargs)
    ui.pick = pick
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert 'DEV_FAST[claude]=0' in local.read_text()


def test_ultrafast_menu_save_reload_and_show(t_mod, config_cli, monkeypatch, capsys):
    cfg, local = config_cli
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    cfg.models = {'codex': 'both'}
    monkeypatch.setattr(t_mod, '_config_live_models', lambda agent: [
        {'model': 'both', 'serviceTiers': [{'id': 'priority'}, {'id': 'ultrafast'}]}])
    ui = Menu(['codex', 'fast', 'ultrafast', 'back', 'save'])
    original_pick = ui.pick
    def pick(label, rows, default=0, **kwargs):
        if 'Speed' in label:
            assert dict(rows)['fast'].startswith('Fast')
            assert dict(rows)['ultrafast'].startswith('Ultrafast')
        return original_pick(label, rows, default, **kwargs)
    ui.pick = pick
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert 'DEV_FAST[codex]=ultrafast' in local.read_text()
    monkeypatch.setattr(t_mod, 'CONFIG', str(local))
    reloaded = t_mod.Config()
    assert reloaded.fast['codex'] == 'ultrafast'
    assert t_mod.cmd_config(reloaded, SimpleNamespace(show=True)) == 0
    assert 'codex speed: Ultrafast' in capsys.readouterr().out


@pytest.mark.parametrize('saved,expected', [('1', 'fast'), ('fast', 'fast'), ('ultrafast', 'ultrafast')])
@pytest.mark.parametrize('available', [True, False])
def test_saved_speed_stays_selected_when_metadata_changes(t_mod, config_cli, monkeypatch, saved, expected, available):
    cfg, _ = config_cli
    cfg.models, cfg.fast = {'codex': 'chosen'}, {'codex': saved}
    def models(agent):
        if not available:
            raise OSError('offline')
        return [{'model': 'chosen', 'serviceTiers': [{'id': 'priority'}]}]
    monkeypatch.setattr(t_mod, '_config_live_models', models)
    ui = Menu([None])
    original_pick = ui.pick
    def pick(label, rows, default=0, **kwargs):
        assert rows[default][0] == expected
        if not available or saved == 'ultrafast':
            assert 'saved; support not verified' in rows[default][1]
        return original_pick(label, rows, default, **kwargs)
    ui.pick = pick
    t_mod._config_model_menu(ui, cfg, 'codex', {}, setting='fast')
    assert cfg.fast['codex'] == saved


def test_config_opening_mode_global_and_repo_defaults(t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    cfg.repos = {"site": "/code/site"}
    ui = Menu(["mode", "app", "repos", "site", "mode", "cli", "back", "__back__", "save"])
    monkeypatch.setattr(t_mod, "_RailUI", lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    monkeypatch.setattr(t_mod, "CONFIG", str(local))
    reloaded = t_mod.Config()
    assert reloaded.open_mode_default == "app"
    assert reloaded.open_mode == {"site": "cli"}
    t_mod._config_remove_repo(reloaded, "site")
    assert reloaded.open_mode == {}
    with pytest.raises(ValueError, match="choose cli or app"):
        t_mod._config_assignment("DEV_OPEN_MODE_DEFAULT", "other")
    with pytest.raises(ValueError, match="choose cli or app"):
        t_mod._config_assignment("DEV_OPEN_MODE[site]", "other")


def test_config_tui_enables_and_displays_automatic_trust(t_mod, tmp_path, monkeypatch, config_cli):
    cfg, local = config_cli
    cfg.auto_trust = '0'
    seen = []
    class TrustMenu(Menu):
        def pick(self, label, rows, default=0, **kwargs):
            if 'trust' in (kwargs.get('values') or {}):
                seen.append(kwargs['values']['trust'])
            return super().pick(label, rows, default, **kwargs)
    menu = TrustMenu(['trust', '1', 'save'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: menu)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == 0
    assert 'export T_AUTO_TRUST=1' in local.read_text()
    assert [v.split()[0] for v in seen] == ['disabled', 'enabled']
    assert t_mod._config_assignment('T_AUTO_TRUST', '0') == 'export T_AUTO_TRUST=0'
    with pytest.raises(ValueError, match='automatic trust'):
        t_mod._config_assignment('T_AUTO_TRUST', 'yes')
    bridge = tmp_path / 'bridge'
    bridge.write_text('T_AUTO_TRUST=1\n')
    monkeypatch.setattr(t_mod, 'CONFIG', str(bridge))
    assert t_mod.Config().auto_trust == '1'


@pytest.mark.parametrize('target,taken,expected', [
    ('alice@mini.example', {}, 'mini'),
    ('mini.example', {'mini', 'mini-2'}, 'mini-3'),
    ('__add__', {}, '__add__-2'),
    ('::', {}, 'host'),
    ('fd7a::1', {}, 'fd7a--1'),
])
def test_host_short_name_suggestions(t_mod, target, taken, expected):
    assert t_mod._host_short_name(target, taken) == expected


def test_config_host_first_add_prefills_short_name(t_mod, config_cli):
    cfg, _ = config_cli
    ui = Menu(['__add__', '__manual__', '__back__'])
    prompts = []
    def answer(prompt, prefill=''):
        prompts.append((prompt, prefill))
        return 'alice@mini.example' if len(prompts) == 1 else prefill
    ui.cooked_input = answer
    t_mod._config_hosts(ui, cfg)
    assert prompts[1][1] == 'mini'
    assert cfg.hosts == {'mini': 'alice@mini.example'}


@pytest.mark.parametrize('save', [True, False])
def test_config_host_rename_review_save_or_discard(t_mod, config_cli, monkeypatch, save):
    cfg, local = config_cli
    cfg.hosts = {'old': 'alice@mini.example'}
    cfg.beam_host = 'old'
    cfg.mini_host = 'alice@mini.example'
    before = local.read_text()
    ui = Menu(['hosts', 'old', 'alias', '__back__'] + (['save'] if save else ['cancel', 'discard']), ['mini'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == (0 if save else 130)
    assert cfg.hosts == {'old': 'alice@mini.example'}
    if save:
        text = local.read_text()
        assert "unset 'REMOTE_HOSTS[old]'" in text
        assert 'REMOTE_HOSTS[mini]=alice@mini.example' in text
        assert 'export TBEAM_HOST=mini' in text
        assert 'MINI_HOST' not in text
    else:
        assert local.read_text() == before


@pytest.mark.parametrize('name', ['bad name', '__add__', '__back__', 'taken'])
def test_config_host_rename_rejects_invalid_or_taken(t_mod, config_cli, name):
    cfg, _ = config_cli
    cfg.hosts = {'old': 'old.example', 'taken': 'other.example'}
    ui = Menu(['old', 'alias', '__back__'], [name])
    t_mod._config_hosts(ui, cfg)
    assert cfg.hosts == {'old': 'old.example', 'taken': 'other.example'}


def test_config_host_rename_preserves_target_defaults_and_same_name(t_mod, config_cli):
    cfg, _ = config_cli
    cfg.hosts = {'old': 'old.example'}
    cfg.beam_host, cfg.mini_host = 'old.example', 'old'
    t_mod._config_rename_host(cfg, 'old', 'old')
    t_mod._config_rename_host(cfg, 'old', 'new')
    assert cfg.hosts == {'new': 'old.example'}
    assert (cfg.beam_host, cfg.mini_host) == ('old.example', 'new')
    with pytest.raises(ValueError, match='Use letters'):
        t_mod._config_rename_host(cfg, 'new', 'bad name')


def test_handoff_defaults_persistence_and_validation(t_mod, tmp_path, monkeypatch):
    path = tmp_path / 'config.sh'
    monkeypatch.setattr(t_mod, 'CONFIG', str(path))
    cfg = t_mod.Config()
    assert cfg.handoff_for('claude') == 't'
    assert cfg.handoff_for('codex') == 'native'
    assert 'manual Hand off' in cfg.handoff_label('codex')
    assert 'Git checkpoint' in cfg.handoff_label('claude')
    changes = {'DEV_HANDOFF[codex]': 't', 'DEV_HANDOFF[claude]': 't'}
    path.write_text(t_mod._config_text('', changes))
    assert t_mod.Config().handoff_for('codex') == 't'
    assert t_mod._config_state(t_mod.Config())['DEV_HANDOFF[codex]'] == 't'
    for key, value in [('DEV_HANDOFF[claude]', 'native'), ('DEV_HANDOFF[codex]', 'unknown'), ('DEV_HANDOFF[other]', 't')]:
        with pytest.raises(ValueError, match='cross-host handoff'):
            t_mod._config_assignment(key, value)
    cfg.handoff['codex'] = 'typo'
    with pytest.raises(ValueError, match='invalid cross-host'):
        cfg.handoff_for('codex')


def test_handoff_menu_stages_selection_and_cancel(t_mod):
    cfg = t_mod.Config()
    choices = iter(['handoff', 't', 'back'])
    ui = SimpleNamespace(pick=lambda *a, **kw: next(choices))
    t_mod._config_tool_menu(ui, cfg, 'codex', {})
    assert cfg.handoff['codex'] == 't'
    choices = iter(['handoff', None, 'back'])
    t_mod._config_tool_menu(ui, cfg, 'codex', {})
    assert cfg.handoff['codex'] == 't'
