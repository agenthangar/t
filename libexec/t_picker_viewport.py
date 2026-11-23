"""Display viewport and terminal relay for the resume table.

Canonical rows stay in the private snapshot. Only the final presentation field
is sliced; selections are translated back before leaving the shared picker.
"""
import errno
import fcntl
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import threading

from t_session_details import display_width

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def plain(text):
    return ANSI.sub('', text)


def window(text, offset, width):
    """Slice terminal cells, preserving SGR and padding split wide glyphs."""
    if width <= 0:
        return ''
    result, position, colored, visible = [], 0, False, False
    for match in re.finditer(ANSI.pattern + r'|[\s\S]', text):
        char = match[0]
        if ANSI.fullmatch(char):
            # Carry styling through the skipped prefix, but never relay cursor
            # movement from a display field. Reset before the overflow marker.
            if char.endswith('m'):
                result.append(char)
                colored = True
            continue
        cells = display_width(char)
        end = position + cells
        if cells == 0:
            if visible and offset < position <= offset + width:
                result.append(char)
        elif end > offset and position < offset + width:
            result.append(char if position >= offset and end <= offset + width
                          else ' ' * (min(end, offset + width) - max(position, offset)))
            visible = True
        position = end
        if position >= offset + width:
            break
    if colored:
        result.append('\x1b[0m')
    value = ''.join(result)
    return value + ' ' * max(0, width - display_width(plain(value)))


def fixed_prefix(text, width):
    """Separate a fixed cell prefix while retaining its status colors."""
    position = cells = 0
    while position < len(text) and cells < width:
        match = ANSI.match(text, position)
        if match:
            position = match.end()
        else:
            cells += display_width(text[position])
            position += 1
    return text[:position], text[position:]


def project(directory, rows, columns, width, fixed=0):
    directory = Path(directory)
    # Status stays fixed; its trailing gap hosts the left overflow indicator.
    # A right overflow cell remains outside the scrolling table text.
    fixed = min(fixed, max(0, width - 3))
    size = max(1, width - fixed - (1 if fixed else 2))
    canonical = (directory / 'all').exists()
    snapshot = (directory / 'all').read_text().splitlines() if canonical else rows
    widest = max([max(0, display_width(plain(columns)) - fixed)] +
                  [max(0, display_width(plain(row.rsplit('\t', 1)[-1])) - (0 if canonical else fixed))
                   for row in snapshot])
    path = directory / 'pan-offset'
    offset = int(path.read_text()) if path.exists() else 0
    offset = min(max(0, offset), max(0, widest - size))
    path.write_text(str(offset))
    left, right = ('<' if offset else ' '), ('>' if offset + size < widest else ' ')
    def cell(text):
        prefix, body = fixed_prefix(text, fixed)
        if fixed:
            return prefix[:-1] + left + window(body, offset, size) + right
        return left + window(body, offset, size) + right
    return [row + '\t' + cell(row.rsplit('\t', 1)[-1]) for row in rows], cell(columns)


def search(rows, query):
    if not query:
        return rows
    # Use the same installed fzf matcher as the visible picker. Python casefold
    # and Unicode normalization differ for cases such as ß and Greek sigma.
    profile = Path(__file__).resolve().parents[1] / 'ui/fzf.sh'
    result = subprocess.run(['sh', '-c', '. "$1"; _t_fzf_match "$2"', 'sh', str(profile), query],
                            input=''.join(row + '\n' for row in rows), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError('picker search failed')
    return result.stdout.splitlines()


class Mouse:
    """Incremental SGR/X10 decoder; unrelated bytes pass through unchanged."""
    def __init__(self):
        self.pending = b''
        self.paste = False

    def feed(self, data, table):
        self.pending += data
        events = []
        while self.pending:
            if self.pending.startswith(b'\x1b[200~'):
                self.paste = True
                events.append(self.pending[:6]); self.pending = self.pending[6:]; continue
            if self.pending.startswith(b'\x1b[201~'):
                self.paste = False
                events.append(self.pending[:6]); self.pending = self.pending[6:]; continue
            if not self.paste and self.pending.startswith(b'\x1b[<'):
                match = re.match(rb'\x1b\[<(\d+);(\d+);(\d+)([Mm])', self.pending)
                if not match:
                    if len(self.pending) < 40 and not re.search(rb'[Mm]', self.pending): break
                else:
                    code, x, y = map(int, match.group(1, 2, 3))
                    raw = match[0]; self.pending = self.pending[len(raw):]
                    if code in (64, 65, 66, 67) and table(x, y):
                        if match[4] == b'M': events.append(-1 if code in (64, 66) else 1)
                    else: events.append(raw)
                    continue
            if not self.paste and self.pending.startswith(b'\x1b[M'):
                if len(self.pending) < 6: break
                raw, self.pending = self.pending[:6], self.pending[6:]
                code, x, y = (c - 32 for c in raw[3:])
                events.append((-1 if code in (64, 66) else 1)
                              if code in (64, 65, 66, 67) and table(x, y) else raw)
                continue
            if self.pending in (b'\x1b', b'\x1b[', b'\x1b[2', b'\x1b[20', b'\x1b[200', b'\x1b[201'):
                break
            events.append(self.pending[:1]); self.pending = self.pending[1:]
        return events

    def flush(self):
        value, self.pending = self.pending, b''
        return value


def relay(command):  # pragma: no cover - isolated real-PTY lifecycle matrix tests
    """Give fzf its own controlling tty, keeping row stdin and result stdout intact."""
    from t_picker_close import Picker, identity
    from t_picker_filters import render
    terminal = os.open('/dev/tty', os.O_RDWR)
    saved = termios.tcgetattr(terminal)
    master, slave = os.openpty()
    directory = Path(os.environ['T_PICKER_VIEWS'])
    original_height = int(os.environ['T_PICKER_HEADER_HEIGHT'])
    original_width = int(os.environ['T_PICKER_HEADER_WIDTH'])
    small = original_height <= 14 or original_width + 12 <= 60
    inset = 5 if small else 12
    # The child opens /dev/tty, while candidate stdin and selection stdout remain
    # inherited pipes/files, just as with the unwrapped shared picker.
    def child_setup():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = None
    old_handlers = {}
    resized = True
    preview_visible = False
    sync_number = 0
    def on_resize(*_):
        nonlocal resized
        resized = True
    def forward(signum, _frame):
        if process is not None:
            try: os.killpg(process.pid, signum)
            except ProcessLookupError: pass
    def suspend(*_):
        if process is None:
            return
        termios.tcsetattr(terminal, termios.TCSADRAIN, saved)
        try:
            os.killpg(process.pid, signal.SIGSTOP)
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            # Match normal terminal job control. An orphaned/non-job-control
            # caller may ignore TSTP; either way the child is resumed below.
            os.killpg(os.getpgrp(), signal.SIGTSTP)
        finally:
            signal.signal(signal.SIGTSTP, suspend)
            tty.setraw(terminal)
            try: os.killpg(process.pid, signal.SIGCONT)
            except ProcessLookupError: pass
            on_resize()
    def geometry():
        height, width, _, _ = struct.unpack('HHHH', fcntl.ioctl(terminal, termios.TIOCGWINSZ, b'\0' * 8))
        return height, width
    def in_table(x, y):
        height, width = geometry()
        header = len((directory / 'current-header').read_text().splitlines())
        # Full-height reverse layout: border/margin, prompt and info precede
        # the fixed header; inline info on short terminals shares the prompt.
        top = (0 if small else 2) + (1 if original_height <= 14 else 2)
        preview_height = int((height - (0 if small else 4)) * .4) if preview_visible else 0
        left = 5 if small else 9
        right = width if small else width - 4
        return top + header <= y <= height - (0 if small else 2) - preview_height and left <= x <= right
    def write_all(fd, data):
        while data:
            count = os.write(fd, data)
            data = data[count:]
    def drain(stop):
        # HTTP reload/mark restoration needs a live fzf event loop. Drain its
        # tty concurrently so even a large repaint cannot fill the PTY buffer.
        while not stop.is_set():
            if select.select([master], [], [], .01)[0]:
                try: data = os.read(master, 65536)
                except OSError: return
                if not data: return
                write_all(terminal, data)
    def synchronize():
        nonlocal sync_number
        sync_number += 1
        wanted = str(sync_number)
        (directory / 'pan-sync-request').write_text(wanted)
        to_child.extend(b'\x1b[23~')
        deadline = time.monotonic() + 5
        while to_child:
            try:
                written = os.write(master, to_child)
                del to_child[:written]
            except BlockingIOError:
                if time.monotonic() >= deadline: raise TimeoutError('picker input timed out')
                select.select([], [master], [], .01)
        ack = directory / 'pan-sync-ack'
        while not ack.exists() or ack.read_text() != wanted:
            if process.poll() is not None: return False
            if time.monotonic() >= deadline: raise TimeoutError('picker synchronization timed out')
            time.sleep(.005)
        os.environ['FZF_PORT'] = (directory / 'pan-port').read_text().strip()
        picker = Picker(len((directory / 'all').read_text().splitlines()) + 1)
        while True:
            # Discovery may publish another serialized view while the native
            # input barrier settles. Follow that view, including its new count.
            picker.count = len((directory / 'all').read_text().splitlines()) + 1
            expected = [plain(row) for row in (directory / 'current').read_text().splitlines()]
            state = picker.request()
            if (not state['reading'] and state['totalCount'] == len(expected)
                    and [plain(item['text']) for item in state['matches']] == expected):
                return True
            time.sleep(.005)
    def barrier():
        stopped = threading.Event()
        reader = threading.Thread(target=drain, args=(stopped,))
        reader.start()
        try:
            return synchronize()
        finally:
            stopped.set(); reader.join()
    def update(direction=0):
        if not (directory / 'pan-port').exists():
            return
        stopped = threading.Event()
        reader = threading.Thread(target=drain, args=(stopped,))
        reader.start()
        try:
            if not synchronize(): return
            count = len((directory / 'all').read_text().splitlines())
            os.environ['FZF_PORT'] = (directory / 'pan-port').read_text().strip()
            picker = Picker(count + 1)
            before = picker.request()
            marks = [identity(item['text']) for item in before['selected']]
            matches = before['matches']
            position = before['position']
            focus = identity(matches[position]['text']) if 0 <= position < len(matches) else None
            path = directory / 'pan-offset'
            previous = (directory / 'current').read_text()
            path.write_text(str(int(path.read_text()) + direction * 8))
            height, width = geometry()
            os.environ['T_PICKER_HEADER_HEIGHT'] = str(height)
            os.environ['T_PICKER_HEADER_WIDTH'] = str(max(3, width - inset))
            (directory / 'pan-size').write_text(f'{height} {max(3, width - inset)}')
            (directory / 'pan-query').write_text(before['query'])
            render(directory, 'resume', 'current', max(3, width - inset), os.environ.get('T_PICKER_COLUMNS', ''))
            projected = (directory / 'current').read_text()
            if projected != previous:
                picker.request('reload-sync(cat "$T_PICKER_CURRENT")')
                while True:
                    picker.count = len((directory / 'all').read_text().splitlines()) + 1
                    expected = [plain(row) for row in (directory / 'current').read_text().splitlines()]
                    state = picker.request()
                    if (not state['reading'] and state['totalCount'] == len(expected)
                            and [plain(item['text']) for item in state['matches']] == expected):
                        break
                    time.sleep(.005)
                positions = {identity(item['text']): n for n, item in enumerate(state['matches'], 1)}
                actions = ['deselect-all'] + [f'pos({positions[key]})+select' for key in marks if key in positions]
                if focus in positions: actions.append(f'pos({positions[focus]})')
                picker.request('+'.join(actions))
                surviving = [key for key in marks if key in positions]
                while True:
                    restored = picker.request()
                    if ([identity(item['text']) for item in restored['selected']] == surviving
                            and (focus not in positions or restored['position'] == positions[focus] - 1)):
                        break
                    time.sleep(.005)
            picker.request('transform-header(cat "$T_PICKER_CURRENT_HEADER")')
        except (OSError, ValueError, KeyError, TimeoutError):
            # A disappearing picker is normal on cancellation. Never accept a
            # substitute identity or launch anything from a failed repaint.
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
        finally:
            stopped.set(); reader.join()
    try:
        for signum in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGTSTP):
            old_handlers[signum] = signal.signal(signum, on_resize if signum == signal.SIGWINCH else suspend if signum == signal.SIGTSTP else forward)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, fcntl.ioctl(terminal, termios.TIOCGWINSZ, b'\0' * 8))
        process = subprocess.Popen(command, stderr=slave, preexec_fn=child_setup, close_fds=True)
        os.close(slave); slave = -1
        tty.setraw(terminal)
        os.set_blocking(master, False)
        to_child = bytearray()
        mouse = Mouse()
        pasted = False
        repaint_pending = False
        last_input = time.monotonic()
        while True:
            if resized:
                fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(terminal, termios.TIOCGWINSZ, b'\0' * 8))
                resized = False
                repaint_pending = True
            # fzf buffers a bracketed paste until its end delimiter. Injecting
            # the synchronization key inside it would turn the key into text.
            if repaint_pending and not pasted and not mouse.paste:
                if (directory / 'pan-port').exists():
                    update()
                repaint_pending = False
            ready, writable, _ = select.select([terminal, master], [master] if to_child else [], [], .04)
            if writable:
                written = os.write(master, to_child)
                del to_child[:written]
            if master in ready:
                try: data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO: raise
                    data = b''
                if not data: break
                write_all(terminal, data)
            if terminal in ready:
                data = os.read(terminal, 65536)
                if not data: break
                last_input = time.monotonic()
                # Before fzf enters its action loop, its terminal queries need
                # the original replies. The input barrier would wait for fzf
                # while fzf was still waiting for those replies.
                if not (directory / 'pan-port').exists():
                    to_child.extend(data)
                    continue
                # Decode and handle in order so a preview toggle changes the
                # hitbox for mouse events later in this same terminal read.
                def decoded():
                    for byte in data:
                        yield from mouse.feed(bytes([byte]), in_table)
                direction = 0
                for event in decoded():
                    if isinstance(event, int): direction += event
                    else:
                        if direction: update(direction); direction = 0
                        if (not pasted and event and event not in (b'\x03', b'\x1a')
                                and any(c < 32 or c == 127 for c in event)):
                            if not barrier(): break
                        if event == b'\x1b[200~': pasted = True
                        if event == b'\x1f' and not pasted:
                            preview_visible = not preview_visible
                            to_child.extend(event)
                            # Settle the new pane geometry before the wheel
                            # or resize that follows the toggle.
                            if not barrier(): break
                            continue
                        if event == b'\x1a' and not pasted:
                            suspend()
                        else:
                            to_child.extend(event)
                        if event == b'\x1b[201~': pasted = False
                if direction: update(direction)
            elif mouse.pending and time.monotonic() - last_input > .06:
                to_child.extend(mouse.flush())
            if process.poll() is not None and not ready: break
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # The outer terminal can disappear while fzf is still alive.
            # The cleanup below terminates the orphaned picker.
            return 130
    finally:
        try:
            termios.tcsetattr(terminal, termios.TCSADRAIN, saved)
        except termios.error as error:
            if error.args[0] not in (errno.EIO, errno.ENXIO, errno.ENOTTY):
                raise
        for signum, handler in old_handlers.items(): signal.signal(signum, handler)
        if process is not None and process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        for fd in (terminal, master, slave):
            if fd >= 0: os.close(fd)


if __name__ == '__main__':
    if sys.argv[1] == 'relay':
        sys.exit(relay(sys.argv[2:]))
