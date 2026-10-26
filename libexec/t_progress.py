"""Delayed terminal feedback for quiet blocking work; safe across runpy helpers."""
from contextlib import contextmanager
from functools import lru_cache
import os
from pathlib import Path
import runpy
import shutil
import sys
import threading
import types

# Helpers are loaded with runpy, sometimes more than once. Share one process lock.
_state = sys.modules.setdefault('_t_progress_state', types.ModuleType('_t_progress_state'))
if not hasattr(_state, 'lock'):
    _state.lock = threading.Lock()


@lru_cache(maxsize=1)
def _clipper():
    return runpy.run_path(str(Path(__file__).with_name('t_session_details.py')))['clip_columns']


@contextmanager
def spinner(message, *, machine=False):
    """Animate only a quiet wait, never a picker, passthrough, or data stream."""
    enabled = (not machine and sys.stdout.isatty() and sys.stderr.isatty()
               and os.environ.get("TERM") != "dumb"
               and not {'--json', '--dump', '--statusline', '--quiet', '-q', '__mcp', '__share'} & set(sys.argv[1:]))
    if not enabled or not _state.lock.acquire(blocking=False):
        yield
        return
    stopped = threading.Event()
    painted = threading.Event()
    stream = sys.stderr
    def animate():
        frame = 0
        while not stopped.wait(0.12 if frame else 0.25):
            width = shutil.get_terminal_size((80, 24)).columns
            label = _clipper()(message, max(0, width - 7))
            stream.write("\r\033[2K  %s %s" % ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[frame % 10], label))
            stream.flush()
            painted.set()
            frame += 1
    worker = threading.Thread(target=animate, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        _state.lock.release()
        if painted.is_set():
            stream.write("\r\033[2K")
            stream.flush()
