"""nosleep config persists validated machine defaults without invoking power controls."""

import importlib.util
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import stat
import struct
import subprocess
import sys
import termios
import time

import pytest

from pty_helpers import run_terminal


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
                                          "battery_protection=0", "battery_below=20",
                                          "hotspot_attempts=3", "hotspot_retry_delay=300",
                                          "hotspot_retry_mode=duration", "hotspot_retry_minutes=60"]
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
    _pty_config(isolated, [(b"Nosleep", b"\r\r\r"),
                           (b"dim on close", b"qq"),
                           (b"Unsaved changes", b"d")])
    assert not path.exists()
    _pty_config(isolated, [(b"Nosleep", b"\r\r\r"),
                           (b"dim on close", b"s"),
                           (b"Review changes", b"y")])
    saved = json.loads(path.read_text())
    assert saved["dim"] is True and saved["lock"] is True
    assert run_config(isolated, "--values").stdout.startswith("dim=1\ndisplay_on=1\nlock=1\n")


def test_unsaved_confirmation_supports_arrow_selection(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Nosleep", b"\r\r\r"),
                           (b"dim on close", b"qq"),
                           (b"Unsaved changes", b"\x1b[B\x1b[B\r")])
    assert not path.exists()
    _pty_config(isolated, [(b"Nosleep", b"\r\r\r"),
                           (b"dim on close", b"qq"),
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


def test_short_narrow_tty_stacks_values_and_interrupt_restores_terminal(isolated, tmp_path):
    env, path = isolated
    output = _pty_config(isolated, [(b"Nosleep", b"q")], columns=42, lines=10)
    assert b"On close" in output
    assert b"Off" in output
    assert not path.exists()
    # A title can be visible before rendering has finished. Use the shared
    # terminal harness and signal at the input boundary; it also verifies the
    # actual terminal flags after exit, rather than inferring restoration.
    harness = tmp_path / "interrupt_config.py"
    harness.write_text('''import importlib.util, sys
spec = importlib.util.spec_from_file_location('config', sys.argv[1])
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
read_key = config.key
def ready_read_key():
    print('<test-read-ready>', end='', flush=True)
    return read_key()
config.key = ready_read_key
raise SystemExit(config.main([]))
''')
    code, output = run_terminal([sys.executable, str(harness), str(MODULE)], env,
                                tmp_path, (42, 10), [('<test-read-ready>', b'\x03')])
    assert code == 0, output
    assert 'Settings unchanged.' in output
    assert not path.exists()


def test_tty_menu_saves_display_on_without_dimming(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Nosleep", b"\r\r"),
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
    _pty_config(isolated, [(b"Nosleep", b"j\rj\rj\r"),
                           (b"off", b"s"), (b"Review changes", b"y")])
    saved = json.loads(path.read_text())
    assert saved["agent"] is False and saved["internet"] is False
    assert saved["forever"] is False
    _pty_config(isolated, [(b"Nosleep", b"j\r\r"),
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
    output = _pty_config(isolated, [(b"Nosleep", b"j\r\r"),
                                    (b"(inactive)", b"s"),
                                    (b"Review changes", b"y")], lines=40)
    for heading in (b"On close", b"Automatic checks", b"CHECK SETTINGS", b"FINISH"):
        assert heading in output
    review = output.split(b"Review changes", 1)[1]
    assert b"1 pending" in review
    assert b"Grace period" not in review and b"Agent activity checks" not in review
    saved = json.loads(path.read_text())
    assert saved["forever"] is True
    assert saved["grace"] == 900 and saved["agent"] is True


def test_settings_section_keyboard_navigation_edits_grace_without_toggling_checks(isolated):
    _, path = isolated
    _pty_config(isolated, [(b"Nosleep", b"j\rjjjj\r"),
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
                                  {"hotspot_timeout": 0}, {"hotspot_timeout": 301},
                                  {"hotspot_attempts": 0}, {"hotspot_attempts": 11},
                                  {"hotspot_attempts": True}, {"hotspot_attempts": 2.5},
                                  {"hotspot_retry_delay": 0}, {"hotspot_retry_delay": 301},
                                  {"hotspot_retry_delay": "30"}])
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
    assert "hotspot_attempts=3" in result.stdout and "hotspot_retry_delay=300" in result.stdout
    assert path.read_bytes() == before


def test_menu_enables_preconfigured_hotspot_and_marks_inactive(isolated):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hotspot_ssid": "Test Phone", "internet": False}))
    _pty_config(isolated, [(b"Nosleep", b"jjj\r\r"),
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
    output = _pty_config(isolated, [(b"Nosleep", b"j\rjjj" + b"\r" * presses + b"s"),
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
    _pty_config(isolated, [(b"Nosleep", b"j\rjjj\rqq"),
                           (b"Unsaved changes", b"d")], lines=40)
    assert not path.exists()
    config.write(path, None, config.DEFAULTS | {"requirement": "none"})
    defaults_index = 4
    _pty_config(isolated, [(b"Nosleep", b"j" * defaults_index + b"\rs"),
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
    output = _pty_config(isolated, [(b'Nosleep', b'k\r')], columns=columns, lines=lines)
    assert b'nothing written' in output
    assert b'On close' in output and b'Nosleep' in output


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
    steps = b'jj\r'
    output = _pty_config(isolated, [
        (b'Nosleep', steps + b'\rj\r'),
        (b'New value', b'25\n'),
        (b'25% on battery', b's'),
        (b'Review changes', b'y'),
    ], columns=columns, lines=lines)
    saved = json.loads(path.read_text())
    assert saved['battery_protection'] is True and saved['battery_below'] == 25
    assert b'Battery protection' in output and b'Exit below' in output
    before = path.read_bytes()
    _pty_config(isolated, [(b'Nosleep', steps + b'\rqq'),
                           (b'Unsaved changes', b'd')], columns=columns, lines=lines)
    assert path.read_bytes() == before


def test_battery_menu_invalid_cutoff_keeps_current_value(isolated):
    _, path = isolated
    steps = b'jj\rj'
    _pty_config(isolated, [(b'Nosleep', steps + b'\r'),
                           (b'New value', b'101\n'),
                           (b'battery_below must be an integer 1..100', b'qq')], lines=40)
    assert not path.exists()









def test_tty_remove_hotspot_saves_without_discovery_or_credentials(isolated):
    _, path = isolated
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config.DEFAULTS | {'hotspot': True, 'hotspot_ssid': 'Old phone'}))
    index = 5
    _pty_config(isolated, [(b'Nosleep', b'jjj\r' + b'j' * index + b'\r'),
                          (b'Hotspot network removed', b's'), (b'Review changes', b'y')])
    saved = json.loads(path.read_text())
    assert saved['hotspot_ssid'] == '' and saved['hotspot'] is False




@pytest.mark.parametrize('name,value,shown', [
    ('hotspot_attempts', 4, b'4 total'),
    ('hotspot_retry_delay', 12, b'12s'),
    ('hotspot_timeout', 20, b'20s'),
])
def test_tty_hotspot_retry_settings_review_and_save(isolated, name, value, shown):
    _, path = isolated
    values = config.DEFAULTS.copy()
    if name == 'hotspot_attempts':
        values['hotspot_retry_mode'] = 'attempts'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(values))
    index = config.page_names('retry', values).index(name)
    _pty_config(isolated, [(b'Nosleep', b'jjj\rjj\r' + b'j' * index + b'\r'),
                          (b'New value', str(value).encode() + b'\n'),
                          (shown, b's'), (b'Review changes', b'y')])
    assert json.loads(path.read_text())[name] == value
    assert ('total' if name == 'hotspot_attempts' else 'Window') in run_config(isolated, '--show').stdout










@pytest.mark.parametrize('bad', [{'hotspot_retry_mode': 'forever'}, {'hotspot_retry_mode': True},
                               {'hotspot_retry_minutes': 0}, {'hotspot_retry_minutes': 1441},
                               {'hotspot_retry_minutes': True}, {'hotspot_retry_minutes': 1.5}])
def test_retry_window_rejects_invalid_settings(bad):
    with pytest.raises(ValueError):
        config.validate(bad)


def test_retry_window_defaults_and_existing_attempt_limit_compatibility():
    assert config.validate({})['hotspot_retry_mode'] == 'duration'
    assert config.DEFAULTS['hotspot_retry_minutes'] == 60
    assert config.DEFAULTS['hotspot_retry_delay'] == 300
    saved = {'hotspot': True, 'hotspot_ssid': 'Phone', 'hotspot_attempts': 2, 'hotspot_retry_delay': 30}
    old = config.validate(saved)
    assert old['hotspot_retry_mode'] == 'attempts' and old['hotspot_attempts'] == 2
    assert old['hotspot_retry_delay'] == 30
    assert config.validate({**saved, 'hotspot_retry_mode': 'duration'})['hotspot_retry_mode'] == 'duration'
    assert config.validate({'hotspot': True, 'hotspot_ssid': 'Phone'})['hotspot_retry_mode'] == 'attempts'


@pytest.mark.parametrize('finish', ['save', 'discard'])
def test_tty_retry_window_mode_and_duration_stage_before_save(isolated, finish):
    _, path = isolated
    values = {**config.DEFAULTS, 'hotspot_retry_mode': 'attempts'}
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(values))
    before = path.read_bytes()
    index = 0
    steps = [(b'Nosleep', b'jjj\rjj\r' + b'j' * index + b'\r'),
             (b'Retry periodically for a set duration', b'\r'),
             (b'60m from first attempt', b'j\r'),
             (b'New value', b'90\n'),
             (b'90m from first attempt', b's' if finish == 'save' else b'qqq')]
    steps.append((b'Review changes', b'y') if finish == 'save' else (b'Unsaved changes', b'd'))
    _pty_config(isolated, steps)
    if finish == 'save':
        saved = json.loads(path.read_text())
        assert saved['hotspot_retry_mode'] == 'duration' and saved['hotspot_retry_minutes'] == 90
        assert saved['hotspot_attempts'] == 3  # Switching back preserves the inactive limit.
    else:
        assert path.read_bytes() == before


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.syspath_prepend(str(MODULE.parent))
    import nosleep_credentials
    import nosleep_wifi
    data = {'state': 'ok', 'current': 'Airport', 'networks': [
        {'ssid': 'Airport', 'source': 'system'}, {'ssid': 'Phone', 'source': 'nosleep'},
        {'ssid': 'Café 東京', 'source': 'system'}, {'ssid': 'qjk', 'source': 'system'}]}
    monkeypatch.setattr(nosleep_credentials, 'saved_networks', lambda: data)
    monkeypatch.setattr(nosleep_wifi, 'observe', lambda **kw: pytest.fail('Browsing saved names must not scan'))
    writes = []
    monkeypatch.setattr(nosleep_credentials, 'save_credentials', lambda ssid, **kw: writes.append((ssid, kw)))
    return data, writes


class FakeRail:
    def __init__(self, choices=(), ssids=()):
        from types import SimpleNamespace
        self.frames, self.events, self.commits = [], [], []
        self.choices, self.ssids = iter(choices), iter(ssids)
        self.ui = SimpleNamespace(pick=lambda *a, **kw: next(self.choices))
    def start(self): self.events.append('start')
    def stop(self): self.events.append('stop')
    def commit(self, lines): self.commits.extend(lines)
    def frame(self, title, body, focus, *args, **kwargs):
        self.frames.append((title, body, focus, kwargs))
    def prompt_ssid(self, current): return next(self.ssids)
    def prompt_int(self, name, current): return current + 1, ''


def drive(monkeypatch, keys):
    events = iter(keys)
    monkeypatch.setattr(config, 'key', lambda: next(events))


def test_saved_networks_are_first_and_listing_never_scans_or_reads_passwords(monkeypatch, credentials):
    ui = FakeRail()
    drive(monkeypatch, ['enter'])
    assert config.network_picker(ui, 'Phone') == {'ssid': 'Airport', 'source': 'system', 'password': None}
    title, rows, _, options = ui.frames[0]
    assert title == 'Choose fallback'
    assert [row[1] for row in rows[:4]] == ['Airport', 'Phone', 'Café 東京', 'qjk']
    assert 'Connected' in rows[0][2] and 'Selected fallback' in rows[1][2]
    assert not any(name in ('search', 'connected', 'connection_info') for name, *_ in rows)
    assert 'Connected: Airport' in options['flash'] and 'Saved fallback: Phone' in options['flash']
    assert credentials[1] == []


@pytest.mark.parametrize('query,expected', [('qjk', 'qjk'), ('É 東', 'Café 東京')])
def test_inline_unicode_filter_uses_letters_instead_of_navigation(monkeypatch, credentials, query, expected):
    ui = FakeRail()
    drive(monkeypatch, [*query, 'enter'])
    assert config.network_picker(ui, '')['ssid'] == expected
    assert f'Filter: {query}' in ui.frames[-1][3]['flash']
    assert all(frame[2] == 0 for frame in ui.frames)


def test_filter_backspace_escape_clear_and_optional_connected_hide(monkeypatch, credentials):
    ui = FakeRail()
    drive(monkeypatch, ['hide', 'P', 'x', 'backspace', 'esc', 'hide', 'esc'])
    assert config.network_picker(ui, 'Phone') is None
    assert ui.frames[0][1][0][1] == 'Airport'
    assert ui.frames[1][1][0][1] == 'Phone'
    assert 'Filter: P' in ui.frames[4][3]['flash']
    assert 'Filter:' not in ui.frames[5][3]['flash']
    assert ui.frames[6][1][0][1] == 'Airport'


@pytest.mark.parametrize('state', ['ok', 'unavailable', 'unsupported'])
def test_saved_empty_and_unavailable_states_keep_manual_nearby_clear_and_back(monkeypatch, credentials, state):
    data, _ = credentials
    data.update(state=state, networks=[])
    ui = FakeRail()
    drive(monkeypatch, ['esc'])
    assert config.network_picker(ui, '') is None
    rows = {row[0] for row in ui.frames[0][1]}
    assert {'nearby', 'manual', 'clear', 'back'} <= rows
    assert ('saved_refresh' in rows) == (state != 'ok')
    assert ('No saved networks' if state == 'ok' else 'unavailable') in ui.frames[0][3]['flash']


@pytest.mark.parametrize('saved_choice', [None, 'saved', 'new'])
def test_manual_ssid_credential_choice_finishes_before_candidate_is_returned(monkeypatch, credentials, saved_choice):
    data, writes = credentials
    data['networks'] = [{'ssid': 'Phone', 'source': 'system'}]
    ui = FakeRail(choices=[saved_choice], ssids=[('Phone', '')])
    calls = []
    def secret(prompt):
        assert ui.events[-1] == 'stop'
        calls.append(prompt)
        return 'private-password'
    monkeypatch.setattr(config.getpass, 'getpass', secret)
    # One saved name, Nearby, then Manual.
    drive(monkeypatch, ['down', 'down', 'enter'] + (['esc'] if saved_choice is None else []))
    result = config.network_picker(ui, 'Original')
    if saved_choice is None:
        assert result is None and calls == []
    elif saved_choice == 'saved':
        assert result == {'ssid': 'Phone', 'source': 'system', 'password': None}
        assert calls == []
    else:
        assert result == {'ssid': 'Phone', 'source': None, 'password': 'private-password'}
        assert ui.events == ['stop', 'start']
    assert writes == []
    assert 'private-password' not in str(ui.frames) + str(ui.commits)


@pytest.mark.parametrize('outcome', ['', EOFError, KeyboardInterrupt])
def test_private_password_cancellation_restores_tty_without_staging(monkeypatch, credentials, outcome):
    ui = FakeRail(ssids=[('New Phone', '')])
    def secret(_):
        assert ui.events[-1] == 'stop'
        if isinstance(outcome, type): raise outcome
        return outcome
    monkeypatch.setattr(config.getpass, 'getpass', secret)
    drive(monkeypatch, ['down'] * 5 + ['enter', 'esc'])
    assert config.network_picker(ui, 'Old phone') is None
    assert ui.events == ['stop', 'start']
    assert credentials[1] == []


def test_nearby_is_separate_preserves_permission_progress_and_saved_focus(monkeypatch, credentials):
    import nosleep_wifi
    observations = []
    def observe(authorize=False, *, progress):
        observations.append(authorize)
        if not authorize:
            progress('checking', 0)
            return {'state': 'permission', 'current': 'Airport', 'networks': []}
        for phase, elapsed in [('preparing', 0), ('permission', 2), ('scanning', 0), ('scanning', 16)]:
            progress(phase, elapsed)
        return {'state': 'ok', 'current': 'Airport', 'networks': [{'ssid': 'Nearby', 'rssi': -40}]}
    monkeypatch.setattr(nosleep_wifi, 'observe', observe)
    monkeypatch.setattr(config.getpass, 'getpass', lambda _: 'nearby-secret')
    ui = FakeRail()
    drive(monkeypatch, ['down'] * 4 + ['enter', 'enter', 'enter'])
    assert config.network_picker(ui, '') == {'ssid': 'Nearby', 'source': None, 'password': 'nearby-secret'}
    assert observations == [False, True]
    progress_frames = [frame for frame in ui.frames if frame[0] == 'Scanning nearby Wi-Fi…']
    assert progress_frames[-1][1] == [('progress', 'Location access granted', '16s · up to 30s')]
    assert all(frame[3]['hint'] == 'Ctrl-C cancel' and frame[3]['primary'] == '' for frame in progress_frames)
    assert credentials[1] == []


@pytest.mark.parametrize('state,reason,action', [('setup', '', 'authorize'), ('denied', '', 'refresh'),
    ('disabled', '', 'refresh'), ('unknown', 'access_timeout', 'authorize'), ('unknown', 'scan_timeout', 'refresh'),
    ('unsupported', '', None)])
def test_nearby_access_states_and_timeout_remain_actionable(monkeypatch, credentials, state, reason, action):
    import nosleep_wifi
    monkeypatch.setattr(nosleep_wifi, 'observe', lambda **kw: {
        'state': state, 'reason': reason, 'current': 'Airport', 'networks': []})
    ui = FakeRail()
    drive(monkeypatch, ['down'] * 4 + ['enter', 'esc', 'esc'])
    assert config.network_picker(ui, 'Old') is None
    frame = next(frame for frame in ui.frames if frame[0] == 'Nearby Wi-Fi networks')
    names = {name for name, *_ in frame[1]}
    assert {'manual', 'clear', 'back'} <= names
    assert (action in names) if action else not {'authorize', 'refresh'} & names
    assert 'Connected: Airport' in frame[3]['flash']
    if reason == 'scan_timeout': assert 'timed out after 30s' in frame[3]['flash']
    assert ui.frames[-1][2] == 4  # Back preserves saved-list focus.


def test_discovery_failure_and_invalid_manual_name_preserve_picker_draft(monkeypatch, credentials):
    import nosleep_wifi
    monkeypatch.setattr(nosleep_wifi, 'observe', lambda **kw: (_ for _ in ()).throw(ValueError('private failure')))
    ui = FakeRail(ssids=[('Old', 'Invalid SSID')])
    drive(monkeypatch, ['down'] * 4 + ['enter', 'down', 'enter', 'esc'])
    assert config.network_picker(ui, 'Old') is None
    assert 'unavailable' in ui.frames[-2][3]['flash'] or any('unavailable' in f[3]['flash'] for f in ui.frames)
    assert 'Invalid SSID' in ui.frames[-1][3]['flash']
    assert 'private failure' not in str(ui.frames)


@pytest.mark.parametrize('finish', ['save', 'cancel', 'failure'])
def test_new_hotspot_is_selected_and_password_saved_only_after_review(monkeypatch, credentials, finish):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    secret = {'ssid': 'Phone', 'password': 'private-secret', 'source': None}
    monkeypatch.setattr(config, 'network_picker', lambda *args: secret)
    import nosleep_credentials
    calls = []
    def save(ssid, **kwargs):
        assert ui.frames[-1][0] == 'Review changes'
        calls.append((ssid, kwargs))
        if finish == 'failure': raise ValueError('private-secret')
    monkeypatch.setattr(nosleep_credentials, 'save_credentials', save)
    keys = ['j'] * 3 + ['enter', 'enter']
    keys += ['q', 'q', 'd'] if finish == 'cancel' else ['s', 'y']
    if finish == 'failure': keys += ['q', 'q', 'd']
    drive(monkeypatch, keys)
    result = config.menu(config.DEFAULTS)
    if finish == 'save':
        assert result['hotspot'] and result['hotspot_ssid'] == 'Phone'
    else:
        assert result is None
    assert len(calls) == (0 if finish == 'cancel' else 1)
    assert 'private-secret' not in str(ui.frames) + str(ui.commits)


def test_failed_save_retains_entered_secret_and_retries_without_reprompt(monkeypatch, credentials):
    import nosleep_credentials
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    monkeypatch.setattr(config, 'network_picker', lambda *args: {'ssid': 'Phone', 'password': 'secret', 'source': None})
    calls = []
    def save(ssid, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1: raise ValueError('secret')
    monkeypatch.setattr(nosleep_credentials, 'save_credentials', save)
    drive(monkeypatch, ['j'] * 3 + ['enter', 'enter', 's', 'y', 's', 'y'])
    assert config.menu(config.DEFAULTS)['hotspot_ssid'] == 'Phone'
    assert calls == [{'password': 'secret', 'source': None}] * 2
    assert any('remain pending' in frame[3].get('flash', '') for frame in ui.frames)
    assert 'secret' not in str(ui.frames) + str(ui.commits)


@pytest.mark.parametrize('action', ['clear', 'remove', 'defaults'])
def test_clear_and_restore_defaults_drop_pending_credentials_without_keychain_mutation(monkeypatch, credentials, action):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    candidates = iter([{'ssid': 'New', 'password': 'secret', 'source': None},
                       {'ssid': '', 'password': None, 'source': None}])
    monkeypatch.setattr(config, 'network_picker', lambda *args: next(candidates))
    keys = ['j'] * 3 + ['enter', 'j', 'enter']
    if action == 'clear': keys += ['enter']
    elif action == 'remove': keys += ['j'] * 4 + ['enter']
    else: keys += ['q', 'j', 'enter']
    keys += ['s', 'y']
    drive(monkeypatch, keys)
    original = config.DEFAULTS | {'hotspot': True, 'hotspot_ssid': 'Old'}
    result = config.menu(original)
    assert result['hotspot_ssid'] == '' and result['hotspot'] is False
    assert credentials[1] == []
    assert original['hotspot_ssid'] == 'Old'
    assert all(name != 'password' for title, rows, *_ in ui.frames if title == 'Review changes' for name, *_ in rows)


def test_nested_back_preserves_parent_focus_and_common_review_includes_all_pages(monkeypatch, credentials):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    drive(monkeypatch, ['enter', 'enter', 'q', 'j', 'enter', 'j', 'enter', 'q', 'j', 'enter', 'enter', 'q', 's', 'y'])
    result = config.menu(config.DEFAULTS)
    assert result['display_on'] and not result['agent'] and result['battery_protection']
    hubs = [frame for frame in ui.frames if frame[0] == 'Nosleep']
    assert [frame[2] for frame in hubs] == [0, 0, 1, 1, 2, 2]
    review = next(rows for title, rows, *_ in ui.frames if title == 'Review changes')
    assert {name for name, *_ in review} == {'dim', 'agent', 'battery_protection'}


@pytest.mark.parametrize('answer', ['esc', 'q', 'cancel'])
def test_review_back_retains_draft_and_returns_to_same_page(monkeypatch, credentials, answer):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    drive(monkeypatch, ['enter', 'enter', 's', answer, 's', 'y'])
    assert config.menu(config.DEFAULTS)['display_on'] is True
    assert [title for title, *_ in ui.frames].count('On close') == 3


def test_change_password_cancellation_preserves_previous_pending_secret(monkeypatch, credentials):
    ui = FakeRail(choices=['new', 'new'])
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    passwords = iter(['first-secret', ''])
    monkeypatch.setattr(config.getpass, 'getpass', lambda _: next(passwords))
    # Change twice, second prompt cancels, then save the first entered password.
    drive(monkeypatch, ['j'] * 3 + ['enter'] + ['j'] * 4 + ['enter', 'enter', 's', 'y'])
    original = config.DEFAULTS | {'hotspot_ssid': 'Phone'}
    assert config.menu(original) == original
    assert credentials[1] == [('Phone', {'password': 'first-secret', 'source': None})]
    assert 'first-secret' not in str(ui.frames) + str(ui.commits)


@pytest.mark.parametrize('columns,lines', [(100, 24), (44, 20), (32, 12)])
def test_network_picker_renders_with_shared_terminal_profile(monkeypatch, credentials, t_mod, columns, lines):
    import io
    rail = object.__new__(t_mod._RailUI)
    rail.st, rail.out, rail.rail, rail.painted = t_mod.Style(), io.StringIO(), '  │', [0]
    rail.cols, rail.lines = lambda: columns, lambda: lines
    ui = object.__new__(config.Rail)
    ui.ui = rail
    drive(monkeypatch, ['down', 'esc'])
    assert config.network_picker(ui, 'Phone') is None
    output = rail.out.getvalue()
    assert 'Choose fallback' in output and 'Airport' in output
    assert rail.painted[0] < lines


def test_unicode_key_decoder_reads_whole_multibyte_character(monkeypatch):
    chunks = iter([b'\xe6', b'\x9d', b'\xb1'])
    monkeypatch.setattr(config.os, 'read', lambda *args: next(chunks))
    monkeypatch.setattr(config.sys, 'stdin', type('Input', (), {'fileno': lambda self: 0})())
    assert config.key() == '東'


@pytest.mark.parametrize('invalid', ['new\nline', 'nul\0byte', 'x' * 1025, '\udcff'])
def test_password_validation_happens_before_staging_and_keeps_private_value_hidden(monkeypatch, invalid):
    ui = FakeRail()
    passwords = iter([invalid, 'valid-test-password'])
    monkeypatch.setattr(config.getpass, 'getpass', lambda _: next(passwords))
    assert config.prompt_password(ui, 'Phone') == 'valid-test-password'
    assert 'at most 1024 bytes' in str(ui.commits)
    assert invalid not in str(ui.commits) + str(ui.frames)
    assert ui.events == ['stop', 'start']


def test_getpass_warning_aborts_instead_of_allowing_echo(monkeypatch):
    import warnings
    ui = FakeRail()
    def unavailable(_):
        warnings.warn('Password input may be echoed', config.getpass.GetPassWarning)
        pytest.fail('A fallback must never accept or read the password')
    monkeypatch.setattr(config.getpass, 'getpass', unavailable)
    assert config.prompt_password(ui, 'Phone') is None
    assert 'Private password entry unavailable' in str(ui.commits)
    assert ui.events == ['stop', 'start']


@pytest.mark.parametrize('credentials_pending', [True, False])
def test_snapshot_conflict_retains_draft_and_prevents_credential_write(monkeypatch, credentials, credentials_pending):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    monkeypatch.setattr(config, 'network_picker', lambda *args: {'ssid': 'Phone', 'password': 'secret', 'source': None})
    keys = (['j'] * 3 + ['enter', 'enter']) if credentials_pending else ['enter', 'enter']
    keys += ['s', 'y', 'q', 'q', 'd']
    drive(monkeypatch, keys)
    checked = []
    def conflict():
        checked.append(True)
        raise ValueError('changed while editing')
    assert config.menu(config.DEFAULTS, before_save=conflict) is None
    assert checked == [True] and credentials[1] == []
    assert any('Configuration changed' in frame[3].get('flash', '') for frame in ui.frames)


def test_credential_only_save_reports_saved_without_materializing_config(monkeypatch, isolated, capsys, credentials):
    env, path = isolated
    monkeypatch.setenv('XDG_CONFIG_HOME', env['XDG_CONFIG_HOME'])
    monkeypatch.setattr(config.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(config.sys.stdout, 'isatty', lambda: True)
    def menu(values, *, before_save, save_values, on_credentials_saved):
        before_save()
        on_credentials_saved()
        return values
    monkeypatch.setattr(config, 'menu', menu)
    assert config.main([]) == 0
    assert 'Saved nosleep defaults' in capsys.readouterr().out
    assert not path.exists()


def test_main_snapshot_guard_detects_external_edit_before_save(monkeypatch, isolated, credentials):
    env, path = isolated
    monkeypatch.setenv('XDG_CONFIG_HOME', env['XDG_CONFIG_HOME'])
    monkeypatch.setattr(config.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(config.sys.stdout, 'isatty', lambda: True)
    def menu(values, *, before_save, save_values, on_credentials_saved):
        path.parent.mkdir(parents=True)
        path.write_text('{"lock":false}')
        with pytest.raises(ValueError, match='changed while editing'):
            before_save()
        return None
    monkeypatch.setattr(config, 'menu', menu)
    assert config.main([]) == 0
    assert json.loads(path.read_text()) == {'lock': False}
    assert credentials[1] == []


@pytest.mark.parametrize('snapshot,ssid,expected', [
    ({}, '', 'No saved network'),
    ({'wifi_current': 'Phone'}, 'Phone', 'Connected now'),
    ({'wifi_observation': {'state': 'ok', 'networks': [{'ssid': 'Phone'}]}}, 'Phone', 'Available nearby'),
    ({'wifi_observation': {'state': 'ok', 'networks': []}}, 'Phone', 'Not seen in last scan'),
    ({'wifi_observation': {'state': 'disabled', 'networks': []}}, 'Phone', 'Unknown'),
    ({}, 'Phone', 'Unknown'),
])
def test_hotspot_availability_uses_only_last_observation(snapshot, ssid, expected):
    from types import SimpleNamespace
    assert config.availability(SimpleNamespace(**snapshot), ssid).startswith(expected)


def test_availability_is_nonselectable_and_visible_when_fallback_off(monkeypatch, credentials):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    drive(monkeypatch, ['j'] * 3 + ['enter', 'q', 'q'])
    assert config.menu(config.DEFAULTS | {'hotspot_ssid': 'Phone'}) is None
    frame = next(frame for frame in ui.frames if frame[0] == 'Hotspot fallback')
    assert 'availability' not in [name for name, *_ in frame[1]]
    assert frame[3]['details']['hotspot_ssid'] == [('Availability', 'Unknown · open Nearby networks to check')]


def test_readonly_details_use_shared_row_without_selection_index():
    from types import SimpleNamespace
    frames = []
    rail = object.__new__(config.Rail)
    rail.ui = SimpleNamespace(rail='  │', section=lambda label: label,
                              row=lambda label, value, **kw: [label],
                              frame=lambda *args, **kw: frames.append(args))
    rail.frame('Hotspot', [('ssid', 'Saved hotspot', 'Phone'), ('back', 'Back', None)], 1,
               details={'ssid': [('Availability', 'Unknown')]}, primary='')
    assert frames[0][1] == [('Saved hotspot', 0), ('Availability', None), ('Back', 1)]


def test_config_write_failure_keeps_draft_and_credential_choice_for_retry(monkeypatch, credentials):
    ui = FakeRail()
    monkeypatch.setattr(config, 'Rail', lambda: ui)
    monkeypatch.setattr(config, 'network_picker', lambda *args: {'ssid': 'Phone', 'password': 'private', 'source': None})
    calls = []
    def persist(values):
        calls.append(values.copy())
        if len(calls) == 1: raise OSError('private filesystem error')
    drive(monkeypatch, ['j'] * 3 + ['enter', 'enter', 's', 'y', 's', 'y'])
    result = config.menu(config.DEFAULTS, save_values=persist)
    assert result['hotspot_ssid'] == 'Phone'
    assert len(calls) == 2 and calls[0] == calls[1] == result
    assert credentials[1] == [('Phone', {'password': 'private', 'source': None})] * 2
    assert any('Settings were not saved; changes remain pending' in frame[3].get('flash', '') for frame in ui.frames)
    assert 'private filesystem error' not in str(ui.frames)


def test_tty_manual_network_prompts_privately_then_saves_without_reprompt(isolated, tmp_path):
    env, path = isolated
    harness = tmp_path / 'private_prompt.py'
    harness.write_text('''import sys
sys.path.insert(0, sys.argv[1])
import nosleep_config as config
import nosleep_credentials as credentials
credentials.saved_networks = lambda: {'state':'ok', 'networks':[], 'current':''}
def save(ssid, **kwargs):
    assert ssid == 'My test phone'
    assert kwargs == {'password':'private-test-password', 'source':None}
credentials.save_credentials = save
raise SystemExit(config.main([]))
''')
    code, output = run_terminal([sys.executable, str(harness), str(MODULE.parent)], env,
        tmp_path, (80, 24), [('Nosleep', b'jjj\r\r'), ('Choose fallback', b'\x1b[B\r'),
        ('Wi-Fi name (blank cancels)', b'My test phone\n'),
        ('Password (blank cancels)', b'private-test-password\n'),
        ('Pending save', b's'), ('Review changes', b'y')])
    assert code == 0, output
    assert output.count('Password (blank cancels)') == 1
    assert 'private-test-password' not in output
    saved = json.loads(path.read_text())
    assert saved['hotspot'] and saved['hotspot_ssid'] == 'My test phone'
    assert set(saved) == set(config.DEFAULTS)
    assert 'private-test-password' not in path.read_text()


def test_saved_list_reuses_observed_connection_when_metadata_is_redacted(monkeypatch, credentials):
    import nosleep_wifi
    data, _ = credentials
    data['current'] = ''
    monkeypatch.setattr(nosleep_wifi, 'observe', lambda **kwargs: {
        'state': 'ok', 'current': 'Airport', 'networks': []})
    ui = FakeRail()
    drive(monkeypatch, ['down'] * 4 + ['enter', 'esc', 'esc'])
    assert config.network_picker(ui, 'Phone') is None
    assert ui.frames[-1][1][0][2] == 'Connected · Saved password'
    assert config.availability(ui, 'Airport') == 'Connected now'
