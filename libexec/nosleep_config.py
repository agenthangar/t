#!/usr/bin/env python3
"""Machine defaults for nosleep. The file is data, never shell code."""

import json
import os
from pathlib import Path
import select
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
import tty
import unicodedata



DEFAULTS = {
    "dim": False,
    "display_on": False,
    "lock": True,
    "forever": False,
    "agent": True,
    "internet": True,
    "requirement": "all",
    "grace": 900,
    "every": 30,
    "retries": 3,
    "backoff": 30,
    "hotspot": False,
    "hotspot_ssid": "",
    "hotspot_after": 300,
    "hotspot_timeout": 30,
    "battery_protection": False,
    "battery_below": 20,
}
LABELS = {
    "dim": "Display when lid closes",
    "lock": "Lock when lid closes",
    "forever": "Automatic checks",
    "agent": "Agent activity checks",
    "internet": "Internet checks",
    "requirement": "Passing checks required",
    "grace": "Grace period",
    "every": "Check interval",
    "retries": "Retries",
    "backoff": "Retry delay",
    "battery_protection": "Battery protection",
    "battery_below": "Exit below",
    "hotspot": "Hotspot fallback",
    "hotspot_ssid": "Hotspot Wi-Fi name",
    "hotspot_after": "Hotspot outage delay",
    "hotspot_timeout": "Hotspot attempt limit",
}


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "nosleep" / "config.json"


def validate_ssid(ssid):
    if (not isinstance(ssid, str) or not ssid or len(ssid.encode("utf-8")) > 32
            or any(unicodedata.category(char) == "Cc" for char in ssid)):
        raise ValueError("hotspot_ssid must contain 1–32 UTF-8 bytes without control characters")
    return ssid


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("configuration must be a JSON object")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValueError("unknown setting: " + ", ".join(sorted(unknown)))
    result = DEFAULTS.copy()
    result.update(data)
    for key in ("dim", "display_on", "lock", "forever", "agent", "internet", "hotspot", "battery_protection"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be true or false")
    requirement = result["requirement"]
    if not ((type(requirement) is str and requirement in ("all", "none"))
            or (type(requirement) is int and requirement > 0)):
        raise ValueError("requirement must be all, none, or a positive integer")
    limits = {"grace": (0, None), "every": (1, None),
              "retries": (0, 10), "backoff": (1, 300),
              "hotspot_after": (0, 2147483647), "hotspot_timeout": (1, 300),
              "battery_below": (1, 100)}
    for key, (minimum, maximum) in limits.items():
        value = result[key]
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            bound = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
            raise ValueError(f"{key} must be an integer {bound}")
    # Existing dim-only configurations retain their display hold.
    result["display_on"] = result["display_on"] or result["dim"]
    if not isinstance(result["hotspot_ssid"], str):
        raise ValueError("hotspot_ssid must be text")
    if result["hotspot"] or result["hotspot_ssid"]:
        validate_ssid(result["hotspot_ssid"])
    return result


def read(path=None):
    path = path or config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return DEFAULTS.copy(), None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    return validate(data), raw


def write(path, before, values):
    """Replace only the target of a symlink and only if it has not changed."""
    path = Path(path)
    target = Path(os.path.realpath(path))
    try:
        current = target.read_bytes()
    except FileNotFoundError:
        current = None
    if current != before:
        raise ValueError("config changed while editing; run nosleep config again")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if current is not None else 0o600
    payload = (json.dumps(validate(values), indent=2) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".nosleep-config-", delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), mode)
            file.write(payload)
        if (target.read_bytes() if target.exists() else None) != before or Path(os.path.realpath(path)) != target:
            raise ValueError("config changed while editing; run nosleep config again")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def monitoring(values):
    return (not values["forever"] and values["requirement"] != "none"
            and (values["agent"] or values["internet"]))


def monitoring_mode(values):
    if values["forever"]:
        return "Disabled (run until Ctrl-C)"
    if values["requirement"] == "none":
        return "Disabled (require none; run until Ctrl-C)"
    if values["agent"] and values["internet"]:
        return "Activity and internet checks"
    if values["agent"]:
        return "Agent activity checks only"
    if values["internet"]:
        return "Internet checks only"
    return "Disabled (both checks off)"


def describe(values):
    inactive = not monitoring(values)

    def check_state(name):
        if values["forever"]:
            return f"Inactive (forever; saved {'on' if values[name] else 'off'})"
        if values["requirement"] == "none":
            return f"Inactive (require none; saved {'on' if values[name] else 'off'})"
        return "On" if values[name] else "Off"

    return [
        "", "On close",
        f"  Display   {display_mode(values)}",
        f"  Locking   {'Lock screen' if values['lock'] else 'No lock requested'}",
        "", "Checks",
        f"  Mode      {monitoring_mode(values)}",
        f"  Agents    {check_state('agent')}",
        f"  Internet  {check_state('internet')}",
        f"  Require   {_choice('requirement', values)}",
        "", "Check settings" + (" (timing settings inactive)" if inactive else ""),
        f"  Interval  {values['every']}s",
        f"  Grace     {values['grace']}s before retrying",
        f"  Retries   {values['retries']}",
        f"  Backoff   {values['backoff']}s, doubles to 300s",
        "", "Battery protection",
        f"  Enabled   {_choice('battery_protection', values)}",
        f"  Below     {_choice('battery_below', values)}",
        "", "Hotspot fallback",
        f"  Hotspot   {_choice('hotspot', values)}",
        f"  Wi-Fi     {values['hotspot_ssid'] or 'Not configured; run nosleep hotspot setup'}",
        f"  Delay     {values['hotspot_after']}s of continuous outage",
        f"  Limit     {values['hotspot_timeout']}s per attempt",
    ]


def key():
    try:
        char = os.read(sys.stdin.fileno(), 1)
    except KeyboardInterrupt:
        return "cancel"
    if char == b"\x1b":
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if ready and os.read(sys.stdin.fileno(), 1) == b"[":
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if ready:
                return {b"A": "up", b"B": "down"}.get(os.read(sys.stdin.fileno(), 1), "esc")
        return "esc"
    return {b"\r": "enter", b"\n": "enter", b"\x03": "cancel"}.get(char, char.decode("utf-8", "ignore"))


class Rail:
    """Small inline picker: redraw only its own lines and keep finished steps in scrollback."""

    def __init__(self):
        # Lazy load: read-only defaults never load the CLI or touch terminal state.
        shared = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/t"))
        self.ui = shared["_RailUI"]()
        self.fd = self.ui.fd
        self.original = self.ui.old_attrs

    def start(self):
        self.ui.raw()

    def stop(self):
        self.ui.restore()

    def style(self, text, code):
        return f"\x1b[{code}m{text}\x1b[0m" if self.ui.st.c else text

    def commit(self, lines):
        self.ui.commit(lines)

    def frame(self, title, body, focus, pending=0, hint="↑↓/jk move · enter change · q back", flash="", primary="s  SAVE CHANGES", sections=None):
        display = []
        label_width = max((len(label) for _, label, _ in body), default=0)
        for index, (name, label, value) in enumerate(body):
            section = (sections or {}).get(name)
            if section:
                if display:
                    display.append((self.ui.rail, None))
                display.append((self.ui.section(section), None))
            display.extend((line, index) for line in self.ui.row(
                label, value, selected=index == focus, label_width=label_width))
        action = None
        if primary:
            parts = primary.split(maxsplit=1)
            action = (parts[0], parts[1] if len(parts) > 1 else "", f"{pending} pending" if pending else "")
        self.ui.frame(title, display, focus, hint, primary=action, flash=flash)

    def prompt_int(self, name, value):
        self.commit([f"  {self.style('◇', '36')}  {LABELS[name]}: {value}", "  │"])
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        try:
            entered = input("      New value (blank keeps current): ").strip()
        except (EOFError, KeyboardInterrupt):
            entered = ""
        finally:
            tty.setcbreak(self.fd)
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()
        if not entered:
            return value, ""
        try:
            return validate({name: int(entered)})[name], ""
        except (ValueError, OverflowError) as exc:
            return value, str(exc)

    def prompt_ssid(self, value):
        self.commit(["  ◇  Hotspot Wi-Fi name (password will be requested when saving)", "  │"])
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        try:
            entered = input("      Wi-Fi name (blank keeps current): ")
            return validate_ssid(entered) if entered else value, ""
        except (EOFError, KeyboardInterrupt):
            return value, ""
        except ValueError as exc:
            return value, str(exc)
        finally:
            tty.setcbreak(self.fd)
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()


def network_picker(ui, current):
    from nosleep_wifi import observe, MESSAGES
    ui.commit(['  ◇  Scanning nearby networks…'])
    data = observe()
    while True:
        rows = [(str(i), row['ssid']) for i, row in enumerate(data['networks'])]
        values = {str(i): ('connected' if row['ssid'] == data['current'] else f"{row['rssi']} dBm")
                  for i, row in enumerate(data['networks'])}
        if current and not any(row['ssid'] == current for row in data['networks']):
            rows.insert(0, ('current', current))
            values['current'] = 'configured · not observed'
        rows += [('refresh', 'Refresh networks'), ('authorize', 'Enable network discovery'),
                 ('manual', 'Enter Wi-Fi name manually'), ('back', 'Back')]
        picked = ui.ui.pick(MESSAGES[data['state']], rows, values=values)
        if picked in (None, 'back'):
            return current
        if picked == 'current':
            return current
        if picked == 'manual':
            selected, error = ui.prompt_ssid(current)
            if not error:
                return selected
            ui.commit(['  ◇  ' + error])
            continue
        if picked in ('refresh', 'authorize'):
            ui.commit(['  ◇  ' + ('Setting up network discovery…' if picked == 'authorize' else 'Scanning nearby networks…')])
            ui.stop()
            try:
                data = observe(authorize=picked == 'authorize')
            except ValueError as exc:
                ui.commit(['  ◇  ' + str(exc)])
            finally:
                ui.start()
        else:
            return data['networks'][int(picked)]['ssid']


def store_password(ui, ssid):
    from nosleep_hotspot import save_password, HotspotError
    ui.commit(['  ◇  Save hotspot password in Keychain', ''])
    ui.stop()
    try:
        save_password(ssid)
        ui.commit(['  ◇  Hotspot password saved in Keychain', ''])
        return ''
    except (HotspotError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        return 'Password was not saved; configuration unchanged. Retry or cancel.'
    finally:
        ui.start()


def display_mode(values):
    return "Dim" if values["dim"] else "On" if values["display_on"] else "Off"


def _choice(name, values):
    value = values[name]
    if name == "dim":
        return {"Off": "off on close", "On": "keep on when closed", "Dim": "dim on close"}[display_mode(values)]
    if name == "lock": return "lock on close" if value else "skip locking"
    if name == "forever": return "disabled (run until Ctrl-C)" if value else "use selected checks"
    if name == "requirement":
        return str(value) + (" (run until Ctrl-C)" if value == "none" else "")
    if name in ("agent", "internet"):
        return ("on" if value else "off") + (" (inactive while monitoring disabled)" if not monitoring(values) else "")
    if name == "battery_protection":
        return "on" if value else "off"
    if name == "battery_below":
        return f"{value}%" + (" on battery" if values["battery_protection"] else " (inactive)")
    if name == "hotspot":
        return ("on" if value else "off") + (" (inactive)" if not monitoring(values) or not values["internet"] else "")
    if name == "hotspot_ssid":
        return value or "select a nearby network"
    if name.startswith("hotspot_"):
        return f"{value}s" + (" (inactive)" if not values["hotspot"] or not monitoring(values) or not values["internet"] else "")
    inactive = not monitoring(values)
    return f"{value}{'s' if name != 'retries' else ''}" + (" (inactive)" if inactive else "")


def changed(name, before, after):
    # A toggle can change inactive annotations on other rows without changing
    # their settings. Count and review only actual edits.
    if name == "dim":
        return display_mode(before) != display_mode(after)
    return before[name] != after[name]


def menu(values):
    edited = values.copy()
    names = list(LABELS) + ["password", "discovery", "defaults", "save", "cancel"]
    password_pending = False
    selected = 0
    ui = Rail()
    sys.stdout.write("\n  nosleep config\n      Settings for this Mac · changes stay pending until saved.\n\n")
    ui.start()
    try:
        flash = ""
        while True:
            pending = sum(changed(name, values, edited) for name in LABELS) + int(password_pending)
            body = [(name, LABELS[name], _choice(name, edited)) for name in LABELS]
            body += [("password", "Set/change hotspot password", "on save" if password_pending else "Keychain"),
                     ("discovery", "Enable network discovery", "macOS Location permission"),
                     ("defaults", "Restore defaults", None),
                     ("save", "Save changes", f"{pending} pending"),
                     ("cancel", "Cancel", None)]
            inactive = not monitoring(edited)
            sections = {"dim": "ON CLOSE", "forever": "CHECKS",
                        "grace": "CHECK SETTINGS" + (" (inactive)" if inactive else ""),
                        "battery_protection": "BATTERY PROTECTION",
                        "hotspot": "HOTSPOT FALLBACK",
                        "defaults": "FINISH"}
            ui.frame("Local settings", body, selected, pending, flash=flash, sections=sections)
            flash = ""
            pressed = key()
            if pressed in ("up", "k"):
                selected = (selected - 1) % len(names)
                continue
            if pressed in ("down", "j"):
                selected = (selected + 1) % len(names)
                continue
            name = "save" if pressed == "s" else names[selected] if pressed == "enter" else "cancel" if pressed in ("q", "esc", "cancel") else None
            if name is None:
                flash = "No binding for that key"
            elif name == "dim":
                mode = display_mode(edited)
                edited["display_on"] = mode != "Dim"
                edited["dim"] = mode == "On"
            elif name in ("lock", "forever", "agent", "internet", "battery_protection"):
                edited[name] = not edited[name]
            elif name == "requirement":
                choices = ("all", "none", 1, 2)
                edited[name] = (choices[(choices.index(edited[name]) + 1) % len(choices)]
                                if edited[name] in choices else "all")
            elif name == "hotspot":
                if not edited["hotspot"] and not edited["hotspot_ssid"]:
                    edited['hotspot_ssid'] = network_picker(ui, '')
                    if edited['hotspot_ssid']:
                        edited['hotspot'] = True
                        password_pending = True
                else:
                    edited[name] = not edited[name]
            elif name == "hotspot_ssid":
                previous = edited[name]
                edited[name] = network_picker(ui, previous)
                if edited[name] != previous:
                    password_pending = True
            elif name == "password":
                if edited['hotspot_ssid']:
                    password_pending = not password_pending
                else:
                    flash = 'Select a hotspot Wi-Fi network first'
            elif name == "discovery":
                from nosleep_wifi import observe, MESSAGES
                ui.commit(['  ◇  Setting up network discovery…'])
                ui.stop()
                try:
                    flash = MESSAGES[observe(authorize=True)['state']]
                except ValueError as exc:
                    flash = str(exc)
                finally:
                    ui.start()
            elif name in DEFAULTS:
                edited[name], flash = ui.prompt_int(name, edited[name])
            elif name == "defaults":
                edited = DEFAULTS.copy()
                password_pending = False
            elif name == "cancel":
                if not pending:
                    ui.commit(["  ◇  cancelled", "      nothing written", ""])
                    return None
                options = [("keep", "Keep editing", None),
                           ("save", "Review and save", None),
                           ("discard", "Discard changes and exit", None)]
                confirm_focus = 0
                while True:
                    ui.frame("Unsaved changes", options, confirm_focus,
                             hint="↑↓/jk move · enter select · q/esc keep",
                             primary="s SAVE · d DISCARD")
                    answer = key()
                    if answer in ("up", "k"):
                        confirm_focus = (confirm_focus - 1) % len(options)
                    elif answer in ("down", "j"):
                        confirm_focus = (confirm_focus + 1) % len(options)
                    elif answer in ("q", "esc", "cancel"):
                        break
                    elif answer == "s" or (answer == "enter" and confirm_focus == 1):
                        name = "save"
                        break
                    elif answer == "d" or (answer == "enter" and confirm_focus == 2):
                        name = "discard"
                        break
                    elif answer == "enter":
                        break
                if name == "discard":
                    ui.commit(["  ◇  cancelled", "      nothing written", ""])
                    return None
                if name != "save":
                    continue
            if name == "save":
                if not pending:
                    ui.commit(["  ◇  settings unchanged", ""])
                    return edited
                changes = [(name, LABELS[name], f"{_choice(name, values)} → {_choice(name, edited)}")
                           for name in LABELS if changed(name, values, edited)]
                if password_pending:
                    changes.append(('password', 'Hotspot password', 'Store in Keychain (private prompt follows)'))
                review_focus = 0
                while True:
                    ui.frame("Review changes", changes, review_focus, pending,
                             hint="↑↓/jk scroll · y / enter save · n / esc back · q cancel",
                             primary="y  SAVE CHANGES")
                    answer = key()
                    if answer in ("up", "k"):
                        review_focus = max(0, review_focus - 1)
                    elif answer in ("down", "j"):
                        review_focus = min(len(changes) - 1, review_focus + 1)
                    elif answer in ("y", "enter"):
                        if password_pending:
                            flash = store_password(ui, edited['hotspot_ssid'])
                            if flash:
                                break
                        ui.commit(["  ◇  saving settings", ""])
                        return edited
                    elif answer in ("q", "cancel"):
                        ui.commit(["  ◇  cancelled", "      nothing written", ""])
                        return None
                    elif answer in ("n", "esc"):
                        break
    except KeyboardInterrupt:
        ui.commit(["  ◇  cancelled", "      nothing written", ""])
        return None
    finally:
        ui.stop()


def edit(path, before, values):
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    if not editor:
        raise ValueError("VISUAL or EDITOR must name an editor")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nosleep-edit-") as directory:
        draft = Path(directory) / "config.json"
        draft.write_bytes(before if before is not None else (json.dumps(DEFAULTS, indent=2) + "\n").encode())
        if subprocess.call([*editor, str(draft)]) != 0:
            raise ValueError("editor failed; configuration unchanged")
        try:
            updated = validate(json.loads(draft.read_text()))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid edited config: {exc}") from exc
        if values is None or updated != values:
            write(path, before, updated)
            return True
    return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["config"]:
        argv.pop(0)
    if argv in (["--help"], ["-h"]):
        print("usage: nosleep config [--show|--edit]\n"
              "\nOpen an interactive menu to set defaults for the next nosleep run.\n"
              "  --show  Print current defaults\n"
              "  --edit  Edit the JSON configuration in VISUAL/EDITOR")
        return 0
    if len(argv) > 1 or (argv and argv[0] not in ("--show", "--edit", "--values")):
        print("usage: nosleep config [--show|--edit]", file=sys.stderr)
        return 2
    mode = argv[0] if argv else ""
    path = config_path()
    try:
        if mode == "--edit":
            try:
                before = path.read_bytes()
            except FileNotFoundError:
                before = None
            try:
                values = validate(json.loads(before)) if before is not None else DEFAULTS.copy()
            except (ValueError, UnicodeDecodeError):
                values = None  # the draft preserves malformed bytes so the editor can repair them
        else:
            values, before = read(path)
        if mode == "--values":
            for name in DEFAULTS:
                print(f"{name}={int(values[name]) if isinstance(values[name], bool) else values[name]}")
            return 0
        if mode == "--show":
            print("nosleep defaults" + (f" · {path}" if before is not None else " · built in"))
            print("\n".join(describe(values)))
            print("\nRun `nosleep config` to change defaults. Flags override them for one run.")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("open this menu in a terminal; use `nosleep config --show` to view defaults")
        if mode == "--edit":
            changed = edit(path, before, values)
        else:
            updated = menu(values)
            changed = updated is not None and updated != values
            if changed:
                write(path, before, updated)
        print("Saved nosleep defaults. They apply to the next run." if changed else "Settings unchanged.")
        return 0
    except (OSError, ValueError) as exc:
        print(f"nosleep config: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
