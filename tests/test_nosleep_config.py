"""nosleep config persists validated machine defaults without invoking power controls."""

import importlib.util
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import stat
import struct
import subprocess
import sys
import termios
import time

import pytest


MODULE = Path(__file__).resolve().parents[1] / "libexec" / "nosleep_config.py"
spec = importlib.util.spec_from_file_location("nosleep_config", MODULE)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


@pytest.fixture
def isolated(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    Path(env["HOME"]).mkdir()
    return env, Path(env["XDG_CONFIG_HOME"]) / "nosleep" / "config.json"


def run_config(isolated, *args):
    env, _ = isolated
    return subprocess.run([sys.executable, str(MODULE), *args], env=env,
                          text=True, capture_output=True)


def test_defaults_and_values_do_not_write(isolated):
    _, path = isolated
    result = run_config(isolated, "--values")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["dim=0", "display_on=0", "lock=1", "forever=0", "agent=1", "internet=1", "requirement=all", "grace=900",
                                          "every=30", "retries=3", "backoff=30", "hotspot=0",
                                          "hotspot_ssid=", "hotspot_after=300", "hotspot_timeout=30",
                                          "battery_protection=0", "battery_below=20"]
    shown = run_config(isolated, "--show")
    assert shown.returncode == 0
    assert "Lock" in shown.stdout and "Display" in shown.stdout
    assert not path.exists()


@pytest.mark.parametrize("bad", [
    {"dim": 1}, {"display_on": 1}, {"lock": "no"}, {"every": 0}, {"retries": 11},
    {"agent": 1}, {"internet": "off"}, {"backoff": 301}, {"grace": -1}, {"unknown": True}, [],
])
def test_invalid_config_blocks_all_modes(isolated, bad):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bad))
    for option in ("--values", "--show"):
        result = run_config(isolated, option)
        assert result.returncode == 1
        assert not result.stdout


def test_atomic_write_preserves_symlink_and_rejects_stale_snapshot(tmp_path):
    target = tmp_path / "actual.json"
    target.write_text('{"dim": false}\n')
    target.chmod(0o640)
    link = tmp_path / "config.json"
    link.symlink_to(target)
    before = target.read_bytes()
    values = config.DEFAULTS | {"dim": True, "lock": True}
    config.write(link, before, values)
    assert link.is_symlink()
    assert json.loads(target.read_text())["dim"] is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    with pytest.raises(ValueError, match="changed while editing"):
        config.write(link, before, config.DEFAULTS)
    assert json.loads(target.read_text())["dim"] is True


def test_new_config_private(isolated):
    _, path = isolated
    config.write(path, None, config.DEFAULTS | {"dim": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _pty_config(isolated, inputs, *args, expected_rc=0, columns=100, lines=24):
    env, _ = isolated
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
    process = subprocess.Popen([sys.executable, str(MODULE), *args], stdin=slave, stdout=slave,
                               stderr=slave, env=env, close_fds=True)
    os.close(slave)
    output = bytearray()
    try:
        for expected, keys in inputs:
            deadline = time.monotonic() + 5
            while expected not in output:
                remaining = deadline - time.monotonic()
                assert remaining > 0, output.decode(errors="replace")
                ready, _, _ = select.select([master], [], [], remaining)
                assert ready, output.decode(errors="replace")
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            os.write(master, keys)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # Linux reports EIO when the slave closes, before waitpid may reap it.
                if not chunk:
                    break
                output.extend(chunk)
            elif process.poll() is not None:
                break
        rc = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        assert rc == expected_rc, output.decode(errors="replace")
        os.set_blocking(master, False)
        while select.select([master], [], [], 0)[0]:
            try:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                output.extend(chunk)
            except (OSError, BlockingIOError):
                break
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def test_tty_menu_cancel_then_save_independent_dim_and_lock(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Local settings", b"\r\r"),
                           (b"dim on close", b"q"),
                           (b"Unsaved changes", b"d")])
    assert not path.exists()
    _pty_config(isolated, [(b"Local settings", b"\r\r"),
                           (b"dim on close", b"s"),
                           (b"Review changes", b"y")])
    saved = json.loads(path.read_text())
    assert saved["dim"] is True and saved["lock"] is True
    assert run_config(isolated, "--values").stdout.startswith("dim=1\ndisplay_on=1\nlock=1\n")


def test_unsaved_confirmation_supports_arrow_selection(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Local settings", b"\r\r"),
                           (b"dim on close", b"q"),
                           (b"Unsaved changes", b"\x1b[B\x1b[B\r")])
    assert not path.exists()
    _pty_config(isolated, [(b"Local settings", b"\r\r"),
                           (b"dim on close", b"q"),
                           (b"Unsaved changes", b"\x1b[B\r"),
                           (b"Review changes", b"y")])
    assert json.loads(path.read_text())["dim"] is True


def test_edit_validates_draft_before_replacing_config(isolated, tmp_path):
    env, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text('{"dim": false}\n')
    original = path.read_bytes()
    editor = tmp_path / "editor.py"
    editor.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('{\"lock\": 1}')\n")
    env["EDITOR"] = f"{sys.executable} {editor}"
    output = _pty_config(isolated, [], "--edit", expected_rc=1)
    assert b"invalid edited config" in output
    assert path.read_bytes() == original
    editor.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('{\"dim\": true, \"lock\": true}')\n")
    output = _pty_config(isolated, [], "--edit")
    assert b"Saved nosleep defaults" in output
    assert json.loads(path.read_text())["dim"] is True
    path.write_text("{ broken")
    output = _pty_config(isolated, [], "--edit")
    assert b"Saved nosleep defaults" in output
    assert json.loads(path.read_text())["dim"] is True


def test_short_narrow_tty_stacks_values_and_interrupt_restores_terminal(isolated):
    env, path = isolated
    output = _pty_config(isolated, [(b"Local settings", b"q")], columns=42, lines=10)
    assert b"Display when lid closes" in output
    assert b"off on close" in output
    assert not path.exists()
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(MODULE)], stdin=slave, stdout=slave,
                               stderr=slave, env=env, close_fds=True)
    os.close(slave)
    try:
        output = bytearray()
        deadline = time.monotonic() + 5
        while b"Local settings" not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 4096))
        assert b"Local settings" in output
        process.send_signal(signal.SIGINT)
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
        # Linux may report PTY closure before waitpid observes process exit.
        assert process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0, output.decode(errors="replace")
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def test_tty_menu_saves_display_on_without_dimming(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Local settings", b"\r"),
                           (b"keep on when closed", b"s"),
                           (b"Review changes", b"y")])
    saved = json.loads(path.read_text())
    assert saved["display_on"] is True and saved["dim"] is False
    assert "Display   On" in run_config(isolated, "--show").stdout


@pytest.mark.parametrize("saved,mode", [
    ({"dim": True}, "Dim"),
    ({"dim": True, "display_on": False}, "Dim"),
    ({"display_on": True}, "On"),
])
def test_saved_display_modes_and_legacy_dim(isolated, saved, mode):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(saved))
    assert f"Display   {mode}" in run_config(isolated, "--show").stdout
    assert "display_on=1" in run_config(isolated, "--values").stdout


@pytest.mark.parametrize("agent,internet,mode", [
    (True, True, "Activity and internet checks"),
    (True, False, "Agent activity checks only"),
    (False, True, "Internet checks only"),
    (False, False, "Disabled (both checks off)"),
])
def test_saved_independent_check_modes(isolated, agent, internet, mode):
    _, path = isolated
    config.write(path, None, config.DEFAULTS | {"agent": agent, "internet": internet})
    result = run_config(isolated, "--show")
    assert mode in result.stdout
    assert f"Agents    {'On' if agent else 'Off'}" in result.stdout
    assert f"Internet  {'On' if internet else 'Off'}" in result.stdout
    assert ("timing settings inactive" in result.stdout) == (not agent and not internet)


def test_tty_menu_saves_individual_checks_and_forever_retains_preferences(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Local settings", b"jjj\rj\r"),
                           (b"off", b"s"), (b"Review changes", b"y")])
    saved = json.loads(path.read_text())
    assert saved["agent"] is False and saved["internet"] is False
    assert saved["forever"] is False
    _pty_config(isolated, [(b"Local settings", b"jj\r"),
                           (b"run until Ctrl-C", b"s"), (b"Review changes", b"y")])
    saved = json.loads(path.read_text())
    assert saved["forever"] is True
    assert saved["agent"] is False and saved["internet"] is False
    assert "Inactive (forever; saved off)" in run_config(isolated, "--show").stdout


def test_config_show_separates_check_switches_from_settings(isolated):
    output = run_config(isolated, "--show").stdout
    checks, settings = output.split("\nChecks\n", 1)[1].split("\nCheck settings\n", 1)
    assert "Agents    On" in checks and "Internet  On" in checks
    assert "Grace" not in checks and "Interval" not in checks
    assert "Grace" in settings and "Interval" in settings and "Retries" in settings


def test_menu_sections_and_only_actual_toggle_edits_are_reviewed(isolated):
    _, path = isolated
    output = _pty_config(isolated, [(b"Local settings", b"jj\r"),
                                    (b"(inactive)", b"s"),
                                    (b"Review changes", b"y")], lines=40)
    for heading in (b"ON CLOSE", b"CHECKS", b"CHECK SETTINGS", b"FINISH"):
        assert heading in output
    review = output.split(b"Review changes", 1)[1]
    assert b"1 pending" in review
    assert b"Grace period" not in review and b"Agent activity checks" not in review
    saved = json.loads(path.read_text())
    assert saved["forever"] is True
    assert saved["grace"] == 900 and saved["agent"] is True


def test_settings_section_keyboard_navigation_edits_grace_without_toggling_checks(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Local settings", b"jjjjjj\r"),
                           (b"New value", b"42\n"),
                           (b"42s", b"s"),
                           (b"Review changes", b"y")], columns=42, lines=10)
    saved = json.loads(path.read_text())
    assert saved["grace"] == 42
    assert saved["agent"] is True and saved["internet"] is True
    assert saved["forever"] is False


@pytest.mark.parametrize("bad", [{"hotspot": 1}, {"hotspot": True}, {"hotspot_ssid": 1},
                                  {"hotspot_ssid": "x\x85y"}, {"hotspot_ssid": "📱" * 9},
                                  {"hotspot_after": -1}, {"hotspot_after": 2147483648},
                                  {"hotspot_timeout": 0}, {"hotspot_timeout": 301}])
def test_hotspot_configuration_validation(isolated, bad):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bad))
    assert run_config(isolated, "--values").returncode == 1


def test_old_config_migrates_hotspot_defaults_without_writing(isolated):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text('{"every": 12}\n')
    before = path.read_bytes()
    result = run_config(isolated, "--values")
    assert "hotspot=0" in result.stdout and "hotspot_after=300" in result.stdout
    assert path.read_bytes() == before


def test_menu_enables_preconfigured_hotspot_and_marks_inactive(isolated):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hotspot_ssid": "Test Phone", "internet": False}))
    _pty_config(isolated, [(b"Local settings", b"j" * list(config.LABELS).index("hotspot") + b"\r"),
                           (b"on (inactive)", b"s"), (b"Review changes", b"y")], lines=40)
    assert json.loads(path.read_text())["hotspot"] is True
    assert "Hotspot   on (inactive)" in run_config(isolated, "--show").stdout


@pytest.mark.parametrize("requirement", ["all", "none", 1, 2, 3, 18446744073709551617])
def test_saved_requirement_read_show_values_and_write(isolated, requirement):
    _, path = isolated
    config.write(path, None, config.DEFAULTS | {"requirement": requirement, "internet": False})
    before = path.read_bytes()
    values, _ = config.read(path)
    assert values["requirement"] == requirement
    assert f"requirement={requirement}\n" in run_config(isolated, "--values").stdout
    shown = run_config(isolated, "--show").stdout
    assert f"Require   {requirement}" in shown
    assert ("timing settings inactive" in shown) == (requirement == "none")
    assert path.read_bytes() == before


@pytest.mark.parametrize("requirement", [True, False, 0, -1, 1.0, "1", "one", "ALL", "", None, []])
def test_invalid_requirement_config_blocks_modes(isolated, requirement):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"requirement": requirement}))
    for mode in ("--values", "--show"):
        result = run_config(isolated, mode)
        assert result.returncode == 1
        assert "requirement must be" in result.stderr
        assert not result.stdout


@pytest.mark.parametrize("presses,expected", [(1, "none"), (2, 1), (3, 2), (4, "all")])
def test_tty_requirement_cycle_review_and_save(isolated, presses, expected):
    _, path = isolated
    output = _pty_config(isolated, [(b"Local settings", b"jjjjj" + b"\r" * presses + b"s"),
                                    (b"Review changes" if presses != 4 else b"settings unchanged",
                                     b"y" if presses != 4 else b"")], lines=40)
    if presses == 4:
        assert not path.exists()
    else:
        assert json.loads(path.read_text())["requirement"] == expected
        review = output.split(b"Review changes", 1)[1]
        assert b"Passing checks required" in review and b"1 pending" in review


def test_tty_requirement_cancel_and_restore_defaults(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Local settings", b"jjjjj\rq"),
                           (b"Unsaved changes", b"d")], lines=40)
    assert not path.exists()
    config.write(path, None, config.DEFAULTS | {"requirement": "none"})
    defaults_index = len(config.LABELS) + 2
    _pty_config(isolated, [(b"Local settings", b"j" * defaults_index + b"\rs"),
                           (b"Review changes", b"y")], lines=40)
    assert json.loads(path.read_text())["requirement"] == "all"


def test_editor_saves_and_rejects_requirement(isolated, tmp_path):
    env, path = isolated
    editor = tmp_path / "requirement_editor.py"
    env["EDITOR"] = f"{sys.executable} {editor}"
    editor.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('{\"requirement\": 2}')\n")
    _pty_config(isolated, [], "--edit")
    assert json.loads(path.read_text())["requirement"] == 2
    before = path.read_bytes()
    editor.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('{\"requirement\": true}')\n")
    output = _pty_config(isolated, [], "--edit", expected_rc=1)
    assert b"invalid edited config" in output
    assert path.read_bytes() == before


def test_none_requirement_marks_check_timing_and_hotspot_inactive(isolated):
    _, path = isolated
    values = config.DEFAULTS | {"requirement": "none", "hotspot": True, "hotspot_ssid": "test-hotspot"}
    config.write(path, None, values)
    shown = run_config(isolated, "--show").stdout
    assert "Inactive (require none; saved on)" in shown
    assert "timing settings inactive" in shown and "Hotspot   on (inactive)" in shown
    assert "inactive" in config._choice("hotspot_after", values)


@pytest.mark.parametrize('columns,lines', [(80, 24), (44, 20), (32, 12)])
def test_section_spacing_and_finish_remain_navigable(isolated, columns, lines):
    output = _pty_config(isolated, [(b'Local settings', b'k\r')], columns=columns, lines=lines)
    assert b'nothing written' in output
    assert b'ON CLOSE' in output and b'\xe2\x94\x82' in output


def test_frame_inserts_nonselectable_breaks_between_sections():
    from types import SimpleNamespace
    frames = []
    rail = object.__new__(config.Rail)
    rail.ui = SimpleNamespace(rail='  │', section=lambda label: label,
                              row=lambda label, value, **kw: [label],
                              frame=lambda *args, **kw: frames.append(args))
    rail.frame('Settings', [('a', 'First', ''), ('b', 'Second', '')], 1,
               sections={'a': 'ONE', 'b': 'TWO'})
    assert frames[0][1] == [('ONE', None), ('First', 0), ('  │', None), ('TWO', None), ('Second', 1)]


@pytest.mark.parametrize('finish', ['save', 'cancel', 'failure'])
def test_new_hotspot_is_selected_and_password_saved_only_after_review(monkeypatch, finish):
    class Rail:
        def start(self): pass
        def stop(self): pass
        def commit(self, lines): pass
        def frame(self, *args, **kwargs): pass
    monkeypatch.setattr(config, 'Rail', Rail)
    monkeypatch.setattr(config, 'network_picker', lambda ui, current: 'Phone')
    saved = []
    def password(ui, ssid):
        saved.append(ssid)
        return 'failure' if finish == 'failure' else ''
    monkeypatch.setattr(config, 'store_password', password)
    keys = ['j'] * list(config.LABELS).index('hotspot') + ['enter']
    keys += ['q', 'd'] if finish == 'cancel' else ['s', 'y']
    if finish == 'failure': keys += ['q', 'd']
    events = iter(keys)
    monkeypatch.setattr(config, 'key', lambda: next(events))
    result = config.menu(config.DEFAULTS)
    if finish == 'save':
        assert result['hotspot'] and result['hotspot_ssid'] == 'Phone'
    else:
        assert result is None
    assert saved == ([] if finish == 'cancel' else ['Phone'])


def test_network_picker_refresh_manual_and_permission_setup(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(MODULE.parent))
    import nosleep_wifi
    observations = []
    def observe(authorize=False):
        observations.append(authorize)
        return {'state': 'ok', 'current': 'Elsewhere', 'networks': [{'ssid': 'Nearby', 'rssi': -40}]}
    monkeypatch.setattr(nosleep_wifi, 'observe', observe)
    picks = iter(['refresh', 'authorize', 'manual'])
    seen = []
    def pick(title, rows, **kwargs):
        seen.append(rows)
        return next(picks)
    ui = SimpleNamespace(ui=SimpleNamespace(pick=pick), start=lambda: None, stop=lambda: None,
                         commit=lambda lines: None, prompt_ssid=lambda value: ('Manual', ''))
    assert config.network_picker(ui, 'Saved') == 'Manual'
    assert observations == [False, False, True]
    assert seen[0][0] == ('current', 'Saved')
    assert ('0', 'Nearby') in seen[0]


@pytest.mark.parametrize('selection,expected', [('0', 'Nearby'), ('current', 'Saved'), ('back', 'Saved'), (None, 'Saved')])
def test_network_picker_select_and_cancel(monkeypatch, selection, expected):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(MODULE.parent))
    import nosleep_wifi
    monkeypatch.setattr(nosleep_wifi, 'observe', lambda: {'state': 'ok', 'current': 'Nearby',
                        'networks': [{'ssid': 'Nearby', 'rssi': -30}]})
    ui = SimpleNamespace(ui=SimpleNamespace(pick=lambda *a, **kw: selection), commit=lambda lines: None)
    assert config.network_picker(ui, 'Saved') == expected


def test_network_picker_handles_invalid_name_and_setup_failure(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(MODULE.parent))
    import nosleep_wifi
    def observe(authorize=False):
        if authorize: raise ValueError('Install command-line tools')
        return {'state': 'setup', 'current': '', 'networks': []}
    monkeypatch.setattr(nosleep_wifi, 'observe', observe)
    keys = iter(['manual', 'authorize', 'back'])
    messages = []
    ui = SimpleNamespace(ui=SimpleNamespace(pick=lambda *a, **kw: next(keys)),
                         commit=lambda lines: messages.extend(lines), start=lambda: None, stop=lambda: None,
                         prompt_ssid=lambda value: (value, 'Invalid SSID'))
    assert config.network_picker(ui, '') == ''
    assert any('Invalid SSID' in line for line in messages)
    assert any('command-line tools' in line for line in messages)


@pytest.mark.parametrize('failure', [False, True])
def test_password_prompt_restores_terminal_and_reports_failure(monkeypatch, failure):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(MODULE.parent))
    import nosleep_hotspot
    events = []
    def save(ssid):
        assert events[-1] == 'stop'
        if failure: raise nosleep_hotspot.HotspotError('private data')
    monkeypatch.setattr(nosleep_hotspot, 'save_password', save)
    ui = SimpleNamespace(commit=lambda lines: None, stop=lambda: events.append('stop'),
                         start=lambda: events.append('start'))
    result = config.store_password(ui, 'Phone')
    assert bool(result) == failure
    assert events == ['stop', 'start']
    assert 'private data' not in result


@pytest.mark.parametrize('action', ['hotspot_ssid', 'password', 'discovery'])
def test_config_hotspot_actions_are_reachable_and_staged(monkeypatch, action):
    monkeypatch.syspath_prepend(str(MODULE.parent))
    import nosleep_wifi
    class Rail:
        def start(self): pass
        def stop(self): pass
        def commit(self, lines): pass
        def frame(self, *args, **kwargs): pass
    monkeypatch.setattr(config, 'Rail', Rail)
    monkeypatch.setattr(config, 'network_picker', lambda ui, current: 'New phone')
    def observe(authorize=False):
        assert authorize
        raise ValueError('Permission setup failed')
    monkeypatch.setattr(nosleep_wifi, 'observe', observe)
    names = list(config.LABELS) + ['password', 'discovery']
    keys = ['j'] * names.index(action) + ['enter', 'q']
    if action != 'discovery': keys.append('d')
    events = iter(keys)
    monkeypatch.setattr(config, 'key', lambda: next(events))
    values = config.DEFAULTS | {'hotspot_ssid': 'Saved'}
    assert config.menu(values) is None
    assert values['hotspot_ssid'] == 'Saved'


@pytest.mark.parametrize('bad', [
    {'battery_protection': 1}, {'battery_protection': 'on'},
    {'battery_below': True}, {'battery_below': '20'}, {'battery_below': 20.5},
    {'battery_below': 0}, {'battery_below': -1}, {'battery_below': 101},
])
def test_battery_config_rejects_invalid_types_and_bounds(isolated, bad):
    test_invalid_config_blocks_all_modes(isolated, bad)


def test_battery_config_defaults_bounds_and_independence(isolated):
    assert config.validate({})['battery_protection'] is False
    assert config.validate({})['battery_below'] == 20
    for cutoff in (1, 20, 100):
        assert config.validate({'battery_below': cutoff})['battery_below'] == cutoff
    shown = run_config(isolated, '--show').stdout
    assert 'Battery protection\n  Enabled   off\n  Below     20% (inactive)' in shown
    values = config.validate({'battery_protection': True, 'battery_below': 25,
                              'forever': True, 'requirement': 'none'})
    assert config._choice('battery_protection', values) == 'on'
    assert config._choice('battery_below', values) == '25% on battery'
    assert 'Below     25% on battery' in '\n'.join(config.describe(values))


@pytest.mark.parametrize('columns,lines', [(80, 24), (44, 20), (32, 12)])
def test_battery_menu_edit_review_save_and_discard(isolated, columns, lines):
    _, path = isolated
    steps = b'j' * list(config.LABELS).index('battery_protection')
    output = _pty_config(isolated, [
        (b'Local settings', steps + b'\rj\r'),
        (b'New value', b'25\n'),
        (b'25% on battery', b's'),
        (b'Review changes', b'y'),
    ], columns=columns, lines=lines)
    saved = json.loads(path.read_text())
    assert saved['battery_protection'] is True and saved['battery_below'] == 25
    assert b'BATTERY PROTECTION' in output and b'Exit below' in output
    before = path.read_bytes()
    _pty_config(isolated, [(b'Local settings', steps + b'\rq'),
                           (b'Unsaved changes', b'd')], columns=columns, lines=lines)
    assert path.read_bytes() == before


def test_battery_menu_invalid_cutoff_keeps_current_value(isolated):
    _, path = isolated
    steps = b'j' * list(config.LABELS).index('battery_below')
    _pty_config(isolated, [(b'Local settings', steps + b'\r'),
                           (b'New value', b'101\n'),
                           (b'battery_below must be an integer 1..100', b'q')], lines=40)
    assert not path.exists()
