"""Guided terminal screens for sharing context; transport stays in t_share.py."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap


class ShareUIError(ValueError):
    """A guided interaction cannot safely continue."""


MAX_COMPOSE_BYTES = 10 * 1024 * 1024


def _has_tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _clean(value):
    text = str(value or "")
    return " ".join("".join(" " if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                            for char in text).split())


def _short(value, width):
    value = _clean(value)
    if width <= 1:
        return value[:max(0, width)]
    return value if len(value) <= width else value[:width - 1] + "…"


def _display_location(row):
    cwd = Path(row.get("cwd") or "")
    if cwd.name.isdecimal() and cwd.parent.name:
        return _clean(f"{row.get('repo') or cwd.parent.name}/{cwd.name}")
    if row.get("repo"):
        return _clean(row["repo"])
    return _clean(cwd.name or "unknown")


def _person(row, api):
    agent = _clean(row.get("agent") or "agent")
    names = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}
    glyph = _clean(api["_agent_glyph"](agent))
    title = _clean(row.get("title") or
                   ("Current conversation" if row.get("address") else "New conversation"))
    return f"{glyph} {names.get(agent, agent.title())}", title


def _context_lines(context, width, max_chars=4000, max_lines=80):
    """Actual content, bounded so review stays responsive to a large share."""
    excerpt = context[:max_chars]
    lines = []
    for raw in excerpt.splitlines() or [""]:
        safe = "".join(" " if ord(char) < 32 or 127 <= ord(char) <= 159 else char
                       for char in raw.expandtabs(4))
        lines.extend(textwrap.wrap(safe, max(4, width), break_long_words=True,
                                   break_on_hyphens=False, replace_whitespace=False,
                                   drop_whitespace=False) or [""])
        if len(lines) >= max_lines:
            break
    lines = lines[:max_lines]
    if len(context) > len(excerpt) or len(lines) >= max_lines:
        lines.append("… preview shortened; complete context is sent")
    return lines


class ShareUI:
    """Reuse t's inline rail and its shared searchable single-choice picker."""

    def __init__(self, api):
        self.api = api
        self.rail = None
        self.plain = False

    def __enter__(self):
        if not _has_tty():
            raise ShareUIError("guided sharing needs a terminal; use --to and --file")
        try:
            self.rail = self.api["_RailUI"]()
            self.rail.raw()
        except (OSError, ValueError):
            if self.rail is not None:
                try:
                    self.rail.restore()
                except (OSError, ValueError):
                    pass
            self.rail = None
            self.plain = True
        return self

    def __exit__(self, _type, _value, _traceback):
        if self.rail is not None:
            self.rail.restore()
        return False

    def intro(self, title, note):
        if self.rail is not None:
            self.rail.intro(_clean(title), _clean(note))
        else:
            print(f"\n{_clean(title)}\n{_clean(note)}\n")

    def choose(self, label, rows):
        """Rows are {key, label, detail}; return the selected key or None."""
        if not rows:
            return None
        keys = [row["key"] for row in rows]
        if len(set(keys)) != len(keys):
            raise ShareUIError("choice keys must be unique")
        if self.rail is not None:
            selected = self.rail.pick(_clean(label), [(row["key"], _clean(row["label"])) for row in rows],
                                      values={row["key"]: _clean(row.get("detail")) for row in rows})
            if selected is not None:
                row = next(row for row in rows if row["key"] == selected)
                self.rail.done(_clean(label), _clean(row["label"]))
            return selected
        print(_clean(label))
        for number, row in enumerate(rows, 1):
            print(f"  {number}. {_clean(row['label'])}  {_clean(row.get('detail'))}")
        while True:
            try:
                answer = input("Select a number (q to go back): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if answer.lower() in ("q", "quit", ""):
                return None
            if answer.isdecimal() and 1 <= int(answer) <= len(rows):
                return rows[int(answer) - 1]["key"]

    def conversations(self, label, records, preferred_cwd=""):
        """Search exact records through the shared fzf single-choice profile."""
        if not records:
            return None
        if not _has_tty():
            raise ShareUIError("conversation selection needs a terminal")
        preferred = os.path.normpath(preferred_cwd) if preferred_cwd else None
        ordered = sorted(records, key=lambda row: (
            0 if preferred and os.path.normpath(row.get("cwd") or "") == preferred else 1,
            _clean(row.get("agent")), _clean(row.get("title")), _clean(row.get("id"))))
        width = max(32, shutil.get_terminal_size((80, 24)).columns)
        with tempfile.TemporaryDirectory(prefix="t-share-picker-") as directory:
            rows = []
            for index, row in enumerate(ordered):
                agent = _clean(row.get("agent"))
                glyph = _clean(self.api["_agent_glyph"](agent))
                location = _display_location(row)
                host = _clean(row.get("host") or "local")
                available = max(26, width - 6)  # picker border, margin and pointer
                agent_cell = _short(f"{glyph} {agent.title()}", 4 if width <= 36 else 9)
                place_cell = _short(f"{location}@{host}" if host != "local" else location,
                                    6 if width <= 36 else max(7, min(14, width // 4)))
                title_cell = _short(row.get("title") or "(untitled)",
                                    max(12, available - len(agent_cell) - len(place_cell) - 2))
                preview = Path(directory) / f"{index}.txt"
                preview.write_text("\n".join((
                    f"{glyph} {agent} · {_clean(row.get('title') or '(untitled)')}",
                    f"Conversation: {_clean(row.get('address'))}",
                    f"Workspace: {_clean(row.get('cwd')) or '(unknown)'}",
                    f"Host: {host}",
                )) + "\n")
                preview.chmod(0o600)
                rows.append("\t".join((str(index), title_cell, agent_cell,
                                        place_cell, str(preview))))
            snippet = '. "$1"; shift; _t_fzf_single "$@"'
            argv = ["sh", "-c", snippet, "t-share-picker",
                    str(Path(self.api["_t_source_root"]()) / "ui/fzf.sh"),
                    "choose", _clean(label),
                    "--prompt=share> ", "--delimiter=\\t", "--with-nth=2..-2",
                    "--no-hscroll", "--preview=cat -- {-1}",
                    "--preview-window=hidden:wrap:down:40%"]
            env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
            if self.rail is not None:
                self.rail.restore()
            try:
                result = subprocess.run(argv, input="\n".join(rows) + "\n", text=True,
                                        stdout=subprocess.PIPE, env=env)
            except KeyboardInterrupt:
                return None
            finally:
                if self.rail is not None:
                    self.rail.raw()
                    self.rail.painted[0] = 0
            if result.returncode in (1, 130):
                return None
            if result.returncode:
                raise ShareUIError("conversation picker unavailable; install fzf")
            selected = result.stdout.rstrip("\n")
            if selected not in rows:
                raise ShareUIError("invalid conversation selection")
            row = ordered[int(selected.split("\t", 1)[0])]
            return row

    def compose(self, label, note=""):
        """Paste multiline text; a lone period finishes and EOF always cancels."""
        if not _has_tty():
            raise ShareUIError("writing a note needs a terminal")
        if self.rail is not None:
            self.rail.restore()
        try:
            print(f"\n{_clean(label)}")
            if note:
                print(_clean(note))
            print("Paste text. Enter . alone to finish, or /cancel to go back.")
            lines = []
            size = 0
            while True:
                try:
                    line = sys.stdin.readline(MAX_COMPOSE_BYTES + 2)
                except KeyboardInterrupt:
                    return None
                if line == "":
                    return None
                line = line.rstrip("\r\n")
                if line == "/cancel":
                    return None
                if line == ".":
                    text = "\n".join(lines).strip()
                    return text or None
                try:
                    size += len(line.encode("utf-8")) + (1 if lines else 0)
                except UnicodeError as error:
                    raise ShareUIError("note must be UTF-8 text") from error
                if size > MAX_COMPOSE_BYTES:
                    raise ShareUIError("note exceeds 10 MiB; nothing shared")
                lines.append(line)
        finally:
            if self.rail is not None:
                self.rail.raw()
                self.rail.painted[0] = 0

    def review(self, source, target, context, notes=None, *,
               context_label="Context", action="Share context"):
        """Show the exact direction and a bounded excerpt; Enter shares."""
        if not isinstance(context, str):
            raise ShareUIError("context must be text")
        src_agent, src_title = _person(source, self.api)
        dst_agent, dst_title = _person(target, self.api)
        note_lines = ([notes] if isinstance(notes, str) else list(notes or []))
        width = max(20, (self.rail.cols() if self.rail else shutil.get_terminal_size((80, 24)).columns) - 10)
        body = [("blank", ""), ("section", "From"),
                ("agent", src_agent), ("title", src_title),
                ("section", "To"), ("agent", dst_agent), ("title", dst_title),
                ("blank", ""), ("section", _clean(context_label)),
                ("count", f"{len(context.encode('utf-8')):,} bytes · {len(context.splitlines())} lines")]
        body += [("note", _clean(note)) for note in note_lines]
        body += [("blank", ""), ("section", f"{_clean(context_label)} preview")]
        body += [("context", line) for line in _context_lines(context, width)]
        body.append(("blank", ""))
        if self.rail is not None:
            st, rail = self.rail.st, self.rail.rail
            painted = []
            for kind, line in body:
                value = line[:width] if kind == "context" else _short(line, width)
                color = st.b + st.c if kind == "section" else st.y if kind in ("count", "note") else ""
                painted.append(f"{rail}  {color}{value}{st.r if color else ''}")
            try:
                hint = ("q back" if self.rail.lines() <= 12 else
                        f"Enter {_clean(action).lower()} · q/Esc back")
                return self.rail.page("Review share", painted,
                                      ("enter", "q", "esc", "abort"),
                                      keys=hint,
                                      primary=("↵", _clean(action), "")) == "enter"
            except KeyboardInterrupt:
                return None
        for _, line in body:
            print(line)
        try:
            return input(f"{_clean(action)}? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return None

    def done(self, label, value):
        if self.rail is not None:
            self.rail.done(_clean(label), _clean(value))
        else:
            print(f"{_clean(label)}: {_clean(value)}")
