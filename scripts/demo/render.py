#!/usr/bin/env python3
"""Capture a real local t workflow in a throwaway HOME and render launch media.

The agent process is an explicit idle stub. No conversation, remote host, or model
result is represented. The t open/worktree/tmux/list/cd commands are real.
Requires git, zsh, tmux, ffmpeg, and Pillow. Run from any directory.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "docs/media"
BG, PANEL, BORDER = "#141516", "#1c1d1f", "#3a3c3e"
INK, MUTED, AMBER = "#f6f3ec", "#b8b3a8", "#f2a93b"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def call(argv, *, cwd=None, env=None, timeout=20):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{argv!r}: {result.stderr or result.stdout}")
    return result.stdout


def open_in_pty(command, *, cwd, env):
    master, slave = os.openpty()
    process = subprocess.Popen(["zsh", "-fc", command], cwd=cwd, env=env,
                               stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    raw = bytearray()
    sent_detach = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    raw.extend(os.read(master, 32768))
                except OSError:
                    break
            if b"Starting dev-demo-1" in raw and not sent_detach:
                time.sleep(1)
                if process.poll() is not None:
                    raise RuntimeError("t open exited before tmux detach: " + raw.decode(errors="replace")[-800:])
                os.write(master, b"\x02d")  # tmux's normal Ctrl-b d detach
                sent_detach = True
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)
    finally:
        os.close(master)
    if not sent_detach:
        raise RuntimeError("t open did not create its tmux session")
    return ANSI.sub("", raw.decode(errors="replace"))


def capture():
    with tempfile.TemporaryDirectory(prefix="t-launch-demo-") as directory:
        base = Path(directory)
        home, repo = base / "home", base / "home/code/demo"
        repo.mkdir(parents=True)
        call(["git", "init", "-q", "-b", "main"], cwd=repo)
        call(["git", "config", "user.email", "demo@example.invalid"], cwd=repo)
        call(["git", "config", "user.name", "Demo"], cwd=repo)
        (repo / "README.md").write_text("# Demo repository\n", encoding="utf-8")
        call(["git", "add", "."], cwd=repo)
        call(["git", "commit", "-qm", "Start demo"], cwd=repo)
        origin = base / "origin.git"
        call(["git", "init", "-q", "--bare", str(origin)])
        call(["git", "remote", "add", "origin", str(origin)], cwd=repo)
        call(["git", "push", "-qu", "origin", "main"], cwd=repo)

        config = home / ".config/t/local.zsh"
        config.parent.mkdir(parents=True)
        config.write_text(f'DEV_REPOS[demo]="{repo}"\nDEV_WORKTREE_ROOT="{home}/code/.worktrees"\n', encoding="utf-8")
        (home / ".zshrc").write_text(f'export PATH="{ROOT}/bin:$PATH"\nsource "{ROOT}/t.plugin.zsh"\n', encoding="utf-8")
        stubs = base / "bin"
        stubs.mkdir()
        agent = stubs / "claude"
        agent.write_text("#!/bin/sh\nprintf 'Demo agent stub: waiting in this tmux slot\\n'\nsleep 20\n", encoding="utf-8")
        agent.chmod(0o755)
        socket = base / "tmux"
        socket.mkdir()
        env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".state"),
               "TMUX_TMPDIR": str(socket), "TMUX": "", "TERM": "xterm-256color", "T_NO_AUTORELOAD": "1",
               "PATH": f"{stubs}:{ROOT / 'bin'}:{os.environ['PATH']}"}
        source = f'source "{ROOT}/t.plugin.zsh"; '
        try:
            before = call(["zsh", "-fc", source + "t ls demo"], cwd=repo, env=env)
            opened = open_in_pty(source + "t open demo --new --local", cwd=repo, env=env)
            after = call(["zsh", "-fc", source + "t ls demo"], cwd=repo, env=env)
            worktree = call(["zsh", "-fc", source + "t cd demo 1; pwd"], cwd=repo, env=env).strip()
            if not (Path(worktree) / ".git").exists() or "demo-1" not in after:
                raise RuntimeError("real t worktree/session validation failed")
        finally:
            subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        return {
            "before": before.strip(),
            "opened": opened.strip(),
            "after": after.strip(),
            "worktree": worktree.replace(str(home), "~"),
        }


def font(size, mono=False, bold=False):
    candidates = (["/System/Library/Fonts/Menlo.ttc"] if mono else
                  ["/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/SFNS.ttf"])
    candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf" if mono else
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for file in candidates:
        if Path(file).exists():
            return ImageFont.truetype(file, size, index=1 if bold and file.endswith(".ttc") else 0)
    return ImageFont.load_default()


def frame(stage, lines, tick, width=1200, height=676, footer="LOCAL DEMO  ·  AGENT PROCESS IS A STUB"):
    canvas = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(canvas)
    for x in range(0, width, 42):
        d.line((x, 0, x, height), fill="#1d1f20", width=1)
    d.text((54, 36), "AGENTHANGAR  /  BAY 04", font=font(16, mono=True), fill=AMBER)
    d.text((54, 77), "t", font=font(80, bold=True), fill=INK)
    d.text((127, 108), "Coding agent sessions, in one place.", font=font(35, bold=True), fill=INK)
    d.text((56, 183), stage, font=font(21), fill=MUTED)
    box = (54, 238, width - 54, height - 95)
    d.rounded_rectangle(box, radius=13, fill=PANEL, outline=BORDER, width=2)
    d.rounded_rectangle((54, 238, width - 54, 284), radius=13, fill="#25272a")
    d.line((54, 284, width - 54, 284), fill=BORDER)
    for x, color in [(77, AMBER), (96, "#7d786f"), (115, "#7d786f")]:
        d.ellipse((x, 257, x + 10, 267), fill=color)
    d.text((width // 2 - 80, 250), "demo  ·  local", font=font(14, mono=True), fill=MUTED)
    y = 315
    for line in lines[:9]:
        if line.startswith("$ "):
            d.text((83, y), "$", font=font(22, mono=True), fill=AMBER)
            d.text((111, y), line[2:], font=font(22, mono=True), fill=INK)
        elif "⬡" in line and Path("/System/Library/Fonts/Apple Symbols.ttf").exists():
            left, right = line[:86].split("⬡", 1)
            mono = font(18, mono=True)
            glyph = ImageFont.truetype("/System/Library/Fonts/Apple Symbols.ttf", 22)
            d.text((83, y), left, font=mono, fill=MUTED)
            gx = 83 + d.textlength(left, font=mono)
            d.text((gx, y - 2), "⬡", font=glyph, fill=MUTED)
            d.text((gx + d.textlength("⬡", font=glyph), y), right, font=mono, fill=MUTED)
        else:
            d.text((83, y), line[:86], font=font(18, mono=True), fill=MUTED)
        y += 35
    d.text((55, height - 62), footer, font=font(14, mono=True), fill="#8f8a80")
    d.text((width - 385, height - 62), "github.com/agenthangar/t", font=font(15, mono=True), fill=AMBER)
    d.rectangle((54, height - 24, 54 + int((width - 108) * tick), height - 20), fill=AMBER)
    return canvas


def render(outputs, *, real_agent=False):
    MEDIA.mkdir(parents=True, exist_ok=True)
    opened = next((line for line in outputs["opened"].splitlines() if "Starting dev-demo-1" in line), "")
    opened = re.sub(r" in \S+ \(logging to \S+\)", " in " + outputs["worktree"], opened)
    table = [line for line in outputs["after"].splitlines() if "demo-1" in line or "STATUS" in line]
    open_command = "t open demo --codex --new --local" if real_agent else "t open demo --new --local"
    stages = [("1 / Start from a registered repository", ["$ t ls demo", outputs["before"].splitlines()[0]]),
              ("2 / Open a new isolated task", ["$ " + open_command, opened])]
    if real_agent:
        if not outputs.get("response", "").startswith("• Session ready."):
            raise RuntimeError("Real media needs a verified Codex response")
        stages.append(("3 / Codex answers in the slot", ["$ " + open_command,
                                                       "Codex: " + outputs["response"].lstrip("• ")]))
    stages += [("4 / See the live tmux slot" if real_agent else "3 / See the live tmux slot",
                ["$ t ls demo", *table]),
               ("5 / Jump into its worktree" if real_agent else "4 / Jump into its worktree",
                ["$ t cd demo 1", "$ pwd", outputs["worktree"]])]
    footer = "REAL CODEX SESSION  ·  LOCAL WORKFLOW" if real_agent else "LOCAL DEMO  ·  AGENT PROCESS IS A STUB"
    frames = []
    frames_per_stage = 36 if real_agent else 20
    for caption, lines in stages:
        for i in range(frames_per_stage):
            frames.append(frame(caption, lines, i / (frames_per_stage - 1), footer=footer))
    gif_frames = [frames[i * frames_per_stage + frames_per_stage - 1].resize((960, 540), Image.Resampling.LANCZOS).quantize(colors=64)
                  for i in range(len(stages))]
    gif_frames[0].save(MEDIA / "demo.gif", save_all=True, append_images=gif_frames[1:],
                       duration=3600 if real_agent else 2000, loop=0, optimize=True, disposal=2)
    poster_stage = 3 if real_agent else 2
    poster = frame(stages[poster_stage][0], stages[poster_stage][1], 1, footer=footer)
    poster.save(MEDIA / "demo-poster.png", optimize=True)
    social = frame("Open. Find. Resume. Move.",
                   ["$ " + open_command, "$ t ls demo", *(table[-1:] or []),
                    "$ t cd demo 1"], 1, height=628, footer=footer)
    social.save(MEDIA / "social-card.png", optimize=True)
    with tempfile.TemporaryDirectory(prefix="t-media-frames-") as directory:
        path = Path(directory)
        for i, image in enumerate(frames):
            image.save(path / f"frame-{i:04d}.png")
        call(["ffmpeg", "-v", "error", "-y", "-framerate", "10", "-i", str(path / "frame-%04d.png"),
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "400k", "-minrate", "400k",
              "-maxrate", "400k", "-bufsize", "800k", "-x264-params", "nal-hrd=cbr:filler=1",
              "-movflags", "+faststart",
              str(MEDIA / "demo.mp4")], timeout=60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    args = parser.parse_args()
    outputs = capture()
    for key, value in outputs.items():
        print(f"{key}: {value}")
    if not args.capture_only:
        render(outputs)
        for name in ("demo-poster.png", "social-card.png", "demo.mp4", "demo.gif"):
            print(name, (MEDIA / name).stat().st_size, "bytes")
