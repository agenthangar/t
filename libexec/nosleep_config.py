#!/usr/bin/env python3
"""Machine defaults for nosleep. The file is data, never shell code."""

import getpass
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
import warnings



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
    "hotspot_attempts": 3,
    "hotspot_retry_delay": 300,
    "hotspot_retry_mode": "duration",
    "hotspot_retry_minutes": 60,
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
    "hotspot_ssid": "Hotspot network",
    "hotspot_after": "Hotspot outage delay",
    "hotspot_retry_mode": "Retry limit",
    "hotspot_retry_minutes": "Retry for (minutes)",
    "hotspot_attempts": "Hotspot attempts",
    "hotspot_retry_delay": "Hotspot retry delay",
    "hotspot_timeout": "Hotspot attempt timeout",
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
    # Do not silently extend the hold for an existing configured fallback.
    if 'hotspot_retry_mode' not in data and (data.get('hotspot') or 'hotspot_attempts' in data):
        result['hotspot_retry_mode'] = 'attempts'
    if result['hotspot_retry_mode'] not in ('attempts', 'duration'):
        raise ValueError('hotspot_retry_mode must be attempts or duration')
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
              "battery_below": (1, 100),
              "hotspot_attempts": (1, 10), "hotspot_retry_delay": (1, 300),
              "hotspot_retry_minutes": (1, 1440)}
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
        f"  Wi-Fi     {values['hotspot_ssid'] or 'Not configured; choose Hotspot network in t config → Nosleep'}",
        f"  Delay     {values['hotspot_after']}s of continuous outage",
        f"  Limit     {_choice('hotspot_retry_mode', values)}",
        (f"  Window    {values['hotspot_retry_minutes']}m from first attempt" if values['hotspot_retry_mode'] == 'duration'
         else f"  Attempts  {values['hotspot_attempts']} total (including first try)"),
        f"  Retry     {values['hotspot_retry_delay']}s between attempts",
        f"  Timeout   {values['hotspot_timeout']}s per attempt",
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
    if char and char[0] >= 0xc0:
        size = 2 if char[0] < 0xe0 else 3 if char[0] < 0xf0 else 4
        for _ in range(size - 1):
            char += os.read(sys.stdin.fileno(), 1)
    return {b"\r": "enter", b"\n": "enter", b"\x03": "cancel",
            b"\x08": "hide", b"\x7f": "backspace", b"": "cancel"}.get(char, char.decode("utf-8", "ignore"))


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

    def frame(self, title, body, focus, pending=0, hint="↑↓/jk move · enter change · q back", flash="", primary="s  SAVE CHANGES", sections=None, details=None):
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
            for detail_label, detail_value in (details or {}).get(name, []):
                display.extend((line, None) for line in self.ui.row(
                    detail_label, detail_value, selected=False, label_width=label_width))
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
        self.commit(["  ◇  Hotspot Wi-Fi name · password next", "  │"])
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original)
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        try:
            entered = input("      Wi-Fi name (blank cancels): ")
            return validate_ssid(entered) if entered else None, ""
        except (EOFError, KeyboardInterrupt):
            return None, ""
        except ValueError as exc:
            return value, str(exc)
        finally:
            tty.setcbreak(self.fd)
            sys.stdout.write("\x1b[?25l")
            sys.stdout.flush()


def prompt_password(ui, ssid):
    """Collect a secret privately now; only Review → Save may persist it."""
    ui.commit(['  ◇  Hotspot password · ' + ssid, ''])
    ui.stop()
    try:
        while True:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('      Password (blank cancels): ')
            if not password:
                return None
            try:
                encoded = password.encode('utf-8')
                valid = len(encoded) <= 1024 and not any(c in encoded for c in (b'\0', b'\n', b'\r'))
            except UnicodeEncodeError:
                valid = False
            if valid:
                return password
            ui.commit(['  ◇  Enter a password of at most 1024 bytes without line breaks or NUL.', ''])
    except (EOFError, KeyboardInterrupt):
        return None
    except (getpass.GetPassWarning, OSError):
        ui.commit(['  ◇  Private password entry unavailable; try again in a terminal.', ''])
        return None
    finally:
        ui.start()


def credential_choice(ui, ssid, saved, *, choose=False):
    source = next((row['source'] for row in saved if row['ssid'] == ssid), None)
    if source and choose:
        picked = ui.ui.pick('Hotspot password', [('saved', 'Use saved password'),
                            ('new', 'Enter a new password'), ('back', 'Back')],
                            values={'saved': 'Keychain', 'new': 'Store in Keychain on save'})
        if picked in (None, 'back'):
            return None
        if picked == 'new':
            source = None
    if source:
        return {'ssid': ssid, 'source': source, 'password': None}
    password = prompt_password(ui, ssid)
    return {'ssid': ssid, 'source': None, 'password': password} if password is not None else None


def network_picker(ui, current):
    """Browse saved names first. Nearby discovery is an explicit separate page."""
    from nosleep_credentials import saved_networks
    from nosleep_wifi import observe, message

    def progress(phase, elapsed):
        title, detail, limit = {
            'preparing': ('Preparing Wi-Fi discovery…', 'Setting up access', 120),
            'checking': ('Checking Wi-Fi access…', 'Waiting for macOS', None),
            'permission': ('Allow Location access to find nearby Wi-Fi', 'Approve the macOS prompt', 120),
            'scanning': ('Scanning nearby Wi-Fi…', 'Location access granted', 30),
        }[phase]
        duration = f'{elapsed}s · up to {limit}s' if limit else f'{elapsed}s'
        ui.frame(title, [('progress', detail, duration)], 0, hint='Ctrl-C cancel', primary='')

    saved = saved_networks()
    ui.wifi_current = saved.get('current') or getattr(ui, 'wifi_current', '')
    page = 'saved'
    data = None
    queries = {'saved': '', 'nearby': ''}
    focuses = {'saved': 0, 'nearby': 0}
    hidden_connected = {'saved': False, 'nearby': False}
    flash = ''
    while True:
        snapshot = saved if page == 'saved' else data
        query = queries[page]
        connected = snapshot.get('current') or getattr(ui, 'wifi_current', '')
        networks = snapshot.get('networks', [])
        shown = [(str(i), row) for i, row in enumerate(networks)
                 if query.casefold() in row['ssid'].casefold()
                 and not (hidden_connected[page] and row['ssid'] == connected)]
        rows = [(i, row['ssid'], ' · '.join(filter(None, [
                    'Selected fallback' if row['ssid'] == current else '',
                    'Connected' if row['ssid'] == connected else '',
                    'Saved password' if page == 'saved' else f"{row['rssi']} dBm"])))
                for i, row in shown]
        sections = {rows[0][0]: 'SAVED WI-FI NETWORKS' if page == 'saved' else 'NEARBY NETWORKS'} if rows else {}
        status = []
        if page == 'saved':
            if saved['state'] != 'ok':
                status.append('Saved network names unavailable · check Keychain access')
            elif not networks:
                status.append('No saved networks found')
            rows += [('saved_refresh', 'Try saved networks again', None)] if saved['state'] != 'ok' else []
            rows += [('nearby', 'Nearby networks…', 'Scan live Wi-Fi')]
            sections['nearby'] = 'OTHER OPTIONS'
        else:
            needs_access = data['state'] in ('setup', 'permission') or data.get('reason') == 'access_timeout'
            status.append(message(data))
            if data['state'] != 'unsupported':
                action = 'authorize' if needs_access else 'refresh'
                label = ('Allow Wi-Fi discovery' if needs_access else
                         'Refresh networks' if data['state'] == 'ok' else 'Check Wi-Fi access again')
                rows += [(action, label, 'macOS Location permission' if needs_access else None)]
                sections[action] = 'WI-FI ACCESS'
        if not shown and networks:
            status.append('No matching networks · Esc clears the filter')
        status += [f"Connected: {connected or 'unknown'} · Saved fallback: {current or 'none'}",
                   f"{len(shown)} shown · {len(networks) - len(shown)} hidden" +
                   (f' · Filter: {query}' if query else '')]
        rows += [('manual', 'Enter Wi-Fi name manually', None),
                 ('clear', 'Clear fallback', 'Disable fallback · keep saved Wi-Fi'),
                 ('back', 'Back to hotspot settings' if page == 'saved' else 'Back to saved networks', None)]
        sections.setdefault('manual', 'OTHER OPTIONS')
        focuses[page] = min(focuses[page], len(rows) - 1)
        ui.frame('Choose fallback' if page == 'saved' else 'Nearby Wi-Fi networks', rows,
                 focuses[page], hint='↑↓ move · enter select · type to filter · Esc back · Ctrl-H hide connected',
                 flash=flash or ' · '.join(status), primary='', sections=sections)
        flash = ''
        pressed = key()
        if pressed in ('up', 'down'):
            focuses[page] = (focuses[page] + (1 if pressed == 'down' else -1)) % len(rows)
            continue
        if pressed == 'hide':
            hidden_connected[page] = not hidden_connected[page]
            focuses[page] = 0
            continue
        if pressed == 'backspace':
            queries[page] = query[:-1]
            focuses[page] = 0
            continue
        if pressed == 'esc' and query:
            queries[page] = ''
            focuses[page] = 0
            continue
        if pressed == 'cancel':
            return None
        if pressed == 'esc':
            picked = 'back'
        elif pressed == 'enter':
            picked = rows[focuses[page]][0]
        elif len(pressed) == 1 and unicodedata.category(pressed)[0] != 'C':
            queries[page] += pressed
            focuses[page] = 0
            continue
        else:
            continue
        if picked == 'back':
            if page == 'saved':
                return None
            page = 'saved'
        elif picked == 'clear':
            return {'ssid': '', 'source': None, 'password': None}
        elif picked == 'manual':
            ssid, error = ui.prompt_ssid(current)
            if error:
                flash = error
            elif ssid is not None:
                candidate = credential_choice(ui, ssid, saved['networks'], choose=True)
                if candidate is not None:
                    return candidate
        elif picked == 'saved_refresh':
            saved = saved_networks()
            ui.wifi_current = saved.get('current') or getattr(ui, 'wifi_current', '')
        elif picked in ('nearby', 'refresh', 'authorize'):
            try:
                data = observe(authorize=picked == 'authorize', progress=progress)
                ui.wifi_observation = data
                ui.wifi_current = data.get('current', '')
                page = 'nearby'
            except ValueError:
                flash = 'Wi-Fi discovery unavailable; retry or enter a name manually'
            except KeyboardInterrupt:
                return None
        else:
            row = networks[int(picked)]
            candidate = credential_choice(ui, row['ssid'], saved['networks'])
            if candidate is not None:
                return candidate


def store_password(ui, ssid, *, password=None, source=None):
    from nosleep_credentials import save_credentials
    try:
        save_credentials(ssid, password=password, source=source)
        return ''
    except (ValueError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        return 'Password was not saved; changes remain pending. Retry or cancel.'


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
        return value or "none · choose or enter a Wi-Fi name"
    if name.startswith("hotspot_"):
        if name == 'hotspot_retry_mode':
            text = 'Elapsed time' if value == 'duration' else 'Number of attempts'
        elif name == 'hotspot_retry_minutes':
            text = f'{value}m from first attempt'
        else:
            text = f"{value} total · includes first try" if name == 'hotspot_attempts' else f"{value}s"
        return text + (" (inactive)" if not values["hotspot"] or not monitoring(values) or not values["internet"] else "")
    inactive = not monitoring(values)
    return f"{value}{'s' if name != 'retries' else ''}" + (" (inactive)" if inactive else "")


def changed(name, before, after):
    # A toggle can change inactive annotations on other rows without changing
    # their settings. Count and review only actual edits.
    if name == "dim":
        return display_mode(before) != display_mode(after)
    return before[name] != after[name]


def visible_names(values):
    hidden = 'hotspot_attempts' if values['hotspot_retry_mode'] == 'duration' else 'hotspot_retry_minutes'
    return [name for name in LABELS if name != hidden]


def page_names(page, values):
    if page == 'on_close':
        return ['dim', 'lock']
    if page == 'checks':
        return ['forever', 'agent', 'internet', 'requirement', 'grace', 'every', 'retries', 'backoff']
    if page == 'battery':
        return ['battery_protection', 'battery_below']
    if page == 'hotspot':
        return ['hotspot', 'hotspot_ssid', 'retry', 'hotspot_after', 'password', 'remove_hotspot', 'save']
    if page == 'retry':
        return ['hotspot_retry_mode', 'hotspot_retry_minutes' if values['hotspot_retry_mode'] == 'duration'
                else 'hotspot_attempts', 'hotspot_retry_delay', 'hotspot_timeout']
    return ['on_close', 'checks', 'battery', 'hotspot', 'defaults', 'save']


def availability(ui, ssid):
    if not ssid:
        return 'No saved network'
    if getattr(ui, 'wifi_current', '') == ssid:
        return 'Connected now'
    observation = getattr(ui, 'wifi_observation', None)
    if observation and observation['state'] == 'ok':
        return ('Available nearby' if any(row['ssid'] == ssid for row in observation['networks'])
                else 'Not seen in last scan')
    return 'Unknown · open Nearby networks to check'


def menu(values, *, before_save=None, save_values=None, on_credentials_saved=None):
    edited = values.copy()
    credentials = None
    page, stack = 'hub', []
    focuses = {}
    ui = Rail()
    sys.stdout.write("\n  nosleep config\n      Settings for this Mac · changes stay pending until saved.\n\n")
    ui.start()
    try:
        flash = ''
        while True:
            pending = sum(changed(name, values, edited) for name in LABELS) + int(credentials is not None)
            names = page_names(page, edited) + ['back']
            focus = min(focuses.get(page, 0), len(names) - 1)
            focuses[page] = focus
            summaries = {
                'on_close': ('On close ›', display_mode(edited) + ' · ' + ('lock' if edited['lock'] else 'no lock')),
                'checks': ('Automatic checks ›', monitoring_mode(edited)),
                'battery': ('Battery protection ›', _choice('battery_protection', edited)),
                'hotspot': ('Hotspot fallback ›', edited['hotspot_ssid'] if edited['hotspot'] else 'Off'),
                'retry': ('Retries ›', f"Every {edited['hotspot_retry_delay']}s · " +
                          (f"up to {edited['hotspot_retry_minutes']}m" if edited['hotspot_retry_mode'] == 'duration'
                           else f"{edited['hotspot_attempts']} total attempts")),
                'password': ('Set/change hotspot password', 'Pending save' if credentials else 'Keychain'),
                'remove_hotspot': ('Remove hotspot network', 'Clear network and disable fallback'),
                'defaults': ('Restore defaults', None),
                'save': ('Review and save', f'{pending} pending'),
                'back': ('Back' if stack else 'Back to t config', None),
            }
            body = [(name, *(summaries[name] if name in summaries and not (name == 'hotspot' and page == 'hotspot')
                             else (LABELS[name], _choice(name, edited)))) for name in names]
            titles = {'hub': 'Nosleep', 'on_close': 'On close', 'checks': 'Automatic checks',
                      'battery': 'Battery protection', 'hotspot': 'Hotspot fallback', 'retry': 'Hotspot retries'}
            sections = {'defaults': 'MANAGE', 'save': 'FINISH', 'back': 'NAVIGATION'}
            if page == 'checks':
                sections['grace'] = 'CHECK SETTINGS' + (' (inactive)' if not monitoring(edited) else '')
            elif page == 'hotspot':
                sections.update(retry='RETRY POLICY', password='MANAGE')
            details = {'hotspot_ssid': [('Availability', availability(ui, edited['hotspot_ssid']))]} if page == 'hotspot' else {}
            ui.frame(titles[page], body, focus, pending, flash=flash, sections=sections, details=details,
                     hint='↑↓/jk move · enter change · s review/save · q back', primary='s REVIEW AND SAVE')
            flash = ''
            pressed = key()
            if pressed in ('up', 'k', 'down', 'j'):
                focuses[page] = (focus + (1 if pressed in ('down', 'j') else -1)) % len(names)
                continue
            name = ('save' if pressed == 's' else names[focus] if pressed == 'enter' else
                    'exit' if pressed == 'cancel' else 'back' if pressed in ('q', 'esc') else None)
            if name is None:
                continue
            if name == 'back' and stack:
                page = stack.pop()
                continue
            if name in ('back', 'exit'):
                if not pending:
                    ui.commit(['  ◇  cancelled', '      nothing written', ''])
                    return None
                options = [('keep', 'Keep editing', None), ('save', 'Review and save', None),
                           ('discard', 'Discard changes and exit', None)]
                confirm_focus = 0
                while True:
                    ui.frame('Unsaved changes', options, confirm_focus,
                             hint='↑↓/jk move · enter select · q/esc keep', primary='s SAVE · d DISCARD')
                    answer = key()
                    if answer in ('up', 'k', 'down', 'j'):
                        confirm_focus = (confirm_focus + (1 if answer in ('down', 'j') else -1)) % len(options)
                    elif answer in ('q', 'esc', 'cancel'):
                        name = 'keep'
                        break
                    elif answer in ('s', 'd', 'enter'):
                        name = 'save' if answer == 's' else 'discard' if answer == 'd' else options[confirm_focus][0]
                        break
                if name == 'discard':
                    ui.commit(['  ◇  cancelled', '      nothing written', ''])
                    return None
                if name != 'save':
                    continue
            if name == 'save':
                if not pending:
                    ui.commit(['  ◇  settings unchanged', ''])
                    return edited
                changes = [(name, LABELS[name], f'{_choice(name, values)} → {_choice(name, edited)}')
                           for name in LABELS if changed(name, values, edited)]
                if credentials is not None:
                    changes.append(('password', 'Hotspot password', 'Store in Keychain on save'))
                review_focus = 0
                while True:
                    ui.frame('Review changes', changes, review_focus, pending,
                             hint='↑↓/jk scroll · y / enter save · n / esc back', primary='y SAVE CHANGES')
                    answer = key()
                    if answer in ('up', 'k', 'down', 'j'):
                        review_focus = max(0, min(len(changes) - 1, review_focus + (1 if answer in ('down', 'j') else -1)))
                    elif answer in ('y', 'enter'):
                        if before_save is not None:
                            try:
                                before_save()
                            except (OSError, ValueError):
                                flash = 'Configuration changed or could not be checked; changes remain pending. Reopen settings to retry.'
                                break
                        if credentials is not None:
                            flash = store_password(ui, edited['hotspot_ssid'], password=credentials['password'], source=credentials['source'])
                            if flash:
                                break
                        if save_values is not None:
                            try:
                                save_values(edited)
                            except (OSError, ValueError):
                                flash = 'Settings were not saved; changes remain pending. Check configuration access and retry.'
                                break
                        if credentials is not None and on_credentials_saved is not None:
                            on_credentials_saved()
                        ui.commit(['  ◇  saving settings', ''])
                        return edited
                    elif answer in ('n', 'esc', 'q', 'cancel'):
                        break
                continue
            if name in ('on_close', 'checks', 'battery', 'retry') or (name == 'hotspot' and page == 'hub'):
                stack.append(page)
                page = name
            elif name == 'dim':
                mode = display_mode(edited)
                edited['display_on'], edited['dim'] = mode != 'Dim', mode == 'On'
            elif name in ('lock', 'forever', 'agent', 'internet', 'battery_protection'):
                edited[name] = not edited[name]
            elif name == 'requirement':
                choices = ('all', 'none', 1, 2)
                edited[name] = choices[(choices.index(edited[name]) + 1) % len(choices)] if edited[name] in choices else 'all'
            elif name == 'hotspot_retry_mode':
                picked = ui.ui.pick('Retry limit', [('duration', 'Elapsed time'), ('attempts', 'Number of attempts')],
                                    values={'duration': 'Retry periodically for a set duration', 'attempts': 'Stop after a fixed number of tries'})
                if picked is not None:
                    edited[name] = picked
            elif name == 'hotspot' and (edited['hotspot'] or edited['hotspot_ssid']):
                edited[name] = not edited[name]
            elif name in ('hotspot', 'hotspot_ssid'):
                candidate = network_picker(ui, edited['hotspot_ssid'])
                if candidate is not None:
                    edited['hotspot_ssid'] = candidate['ssid']
                    if not candidate['ssid']:
                        edited['hotspot'], credentials = False, None
                    else:
                        credentials = candidate
                        if name == 'hotspot':
                            edited['hotspot'] = True
            elif name == 'remove_hotspot':
                edited['hotspot_ssid'], edited['hotspot'], credentials = '', False, None
                flash = 'Hotspot network removed · save to apply'
            elif name == 'password':
                if edited['hotspot_ssid']:
                    from nosleep_credentials import saved_networks
                    candidate = credential_choice(ui, edited['hotspot_ssid'], saved_networks()['networks'], choose=True)
                    if candidate is not None:
                        credentials = candidate
                else:
                    flash = 'Select a hotspot Wi-Fi network first'
            elif name == 'defaults':
                edited, credentials = DEFAULTS.copy(), None
            elif name in DEFAULTS:
                edited[name], flash = ui.prompt_int(name, edited[name])
    except KeyboardInterrupt:
        # The normal Ctrl-C key path offers the unsaved confirmation.
        ui.commit(['  ◇  cancelled', '      nothing written', ''])
        return None
    finally:
        credentials = None
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
            credentials_saved = []
            def check_snapshot():
                try:
                    current = path.read_bytes()
                except FileNotFoundError:
                    current = None
                if current != before:
                    raise ValueError('config changed while editing')
            updated = menu(values, before_save=check_snapshot,
                           save_values=lambda updated: write(path, before, updated) if updated != values else None,
                           on_credentials_saved=lambda: credentials_saved.append(True))
            changed = updated is not None and updated != values
        saved = changed or (mode != '--edit' and bool(credentials_saved))
        print("Saved nosleep defaults. They apply to the next run." if saved else "Settings unchanged.")
        return 0
    except (OSError, ValueError) as exc:
        print(f"nosleep config: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
