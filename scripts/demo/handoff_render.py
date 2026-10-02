#!/usr/bin/env python3
"""Render a sanitized, verified Mac ↔ mini ↔ desktop launch walkthrough.

The capture is produced by the recipe in docs/media/README.md. This renderer
refuses to advertise a returned conversation unless the capture records all
three one-owner checks and verifies the mini turn after return.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw

from render import BG, INK, MUTED, AMBER, MEDIA, call, font

CAPTURE = MEDIA / "handoff-capture.json"
WHITE = "#f6f3ec"
PANEL = "#1c1d1f"
EDGE = "#393b3d"
GREEN = "#9bca91"
STEPS = ("MAC CLI", "MINI", "MAC CLI", "APP REQUEST")


def _evidence(capture: dict) -> None:
    required = (
        "local_answer", "beam_out", "mini_answer", "beam_in",
        "returned_answer", "desktop_request",
    )
    if any(not capture.get(key) for key in required):
        raise ValueError("Handoff capture is missing observed command output")
    if not all(capture.get(key) is True for key in (
        "local_stopped_on_mini", "mini_stopped_on_return",
        "same_thread_on_mini", "same_thread_on_return",
        "mini_turn_visible_on_return",
    )):
        raise ValueError("Handoff capture does not verify one owner and full turn continuity")
    if "Open request sent" not in capture["desktop_request"]:
        raise ValueError("Desktop stage needs t's actual macOS open-request result")


def _panel(draw: ImageDraw.ImageDraw, stage: dict, width: int, height: int) -> None:
    x0, x1, top, bottom = 54, width - 54, 286, height - 121
    draw.rounded_rectangle((x0, top, x1, bottom), radius=16, fill=PANEL, outline=EDGE, width=2)
    draw.rounded_rectangle((x0, top, x1, top + 46), radius=16, fill="#27292b")
    draw.rectangle((x0, top + 31, x1, top + 46), fill="#27292b")
    draw.line((x0, top + 46, x1, top + 46), fill=EDGE, width=2)
    for i, color in enumerate((AMBER, "#77746f", "#77746f")):
        cx = 78 + 19 * i
        draw.ellipse((cx, top + 18, cx + 10, top + 28), fill=color)
    draw.text((width - 220, top + 14), stage["host"], fill=MUTED, font=font(15, mono=True))
    draw.text((84, top + 75), stage.get("prefix", "$"), fill=AMBER, font=font(26, mono=True))
    draw.text((112, top + 75), stage["command"], fill=WHITE, font=font(24, mono=True))
    y = top + 131
    for line in stage["lines"]:
        color = GREEN if line.startswith("✓") else MUTED
        draw.text((84, y), line, fill=color, font=font(20, mono=True))
        y += 43


def _route(draw: ImageDraw.ImageDraw, active: int) -> None:
    x = (141, 444, 747, 1050)
    y = 216
    for i in range(3):
        draw.line((x[i] + 59, y, x[i + 1] - 59, y),
                  fill=AMBER if i < active else EDGE, width=3)
    for i, label in enumerate(STEPS):
        color = AMBER if i <= active else EDGE
        draw.rounded_rectangle((x[i] - 55, y - 24, x[i] + 55, y + 24),
                               radius=13, fill=PANEL, outline=color, width=2)
        draw.text((x[i] - draw.textlength(label, font=font(14, mono=True)) / 2, y - 10),
                  label, fill=INK if i <= active else MUTED, font=font(14, mono=True))


def frame(stage: dict, tick: float, *, width: int = 1200, height: int = 676) -> Image.Image:
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 42):
        draw.line((x, 0, x, height), fill="#1d1f20", width=1)
    draw.text((54, 34), "AGENTHANGAR  /  t", fill=AMBER, font=font(17, mono=True))
    draw.text((54, 76), "Keep the thread moving.", fill=INK, font=font(47, bold=True))
    draw.text((56, 147), stage["title"], fill=MUTED, font=font(23))
    _route(draw, stage["route"])
    _panel(draw, stage, width, height)
    draw.text((55, height - 74), "RECORDED CLI + HOST HANDOFF", fill=MUTED, font=font(14, mono=True))
    draw.text((55, height - 53), "DESKTOP STAGE: OPEN REQUEST, DELIVERY UNCONFIRMED",
              fill="#8f8a80", font=font(13, mono=True))
    draw.text((width - 381, height - 63), "github.com/agenthangar/t", fill=AMBER,
              font=font(15, mono=True))
    draw.rectangle((54, height - 24, 54 + int((width - 108) * tick), height - 20), fill=AMBER)
    return image


def render(capture: dict) -> None:
    _evidence(capture)
    stages = [
        {"title": "1 / Start an isolated Codex task", "route": 0, "host": "mac",
         "command": capture["open_command"], "lines": [capture["local_answer"]]},
        {"title": "2 / Move the live thread to mini", "route": 1, "host": "mac → mini",
         "command": capture["beam_out_command"], "lines": [capture["beam_out"],
         "✓ Mac owner stopped · mini owner live"]},
        {"title": "3 / Continue on the other host", "route": 1, "host": "mini", "prefix": "›",
         "command": capture["mini_command"], "lines": [capture["mini_answer"],
         "✓ Same thread ID on mini"]},
        {"title": "4 / Bring the thread home", "route": 2, "host": "mini → mac",
         "command": capture["beam_in_command"], "lines": [capture["beam_in"],
         "✓ Mini owner stopped · Mac owner live"]},
        {"title": "5 / The mini turn came back too", "route": 2, "host": "mac", "prefix": "›",
         "command": capture["returned_command"], "lines": [capture["returned_answer"],
         "✓ Remote turn verified after return"]},
        {"title": "6 / Request a Codex desktop handoff", "route": 3, "host": "macOS",
         "command": capture["desktop_command"], "lines": [capture["desktop_request"],
         "CLI stopped · worktree slot reserved"]},
    ]
    MEDIA.mkdir(parents=True, exist_ok=True)
    frames_per_stage = 40
    frames = [
        frame(stage, (stage_index * frames_per_stage + n + 1) /
              (len(stages) * frames_per_stage))
        for stage_index, stage in enumerate(stages)
        for n in range(frames_per_stage)
    ]
    gif = [
        frames[(i + 1) * frames_per_stage - 1].resize((960, 540), Image.Resampling.LANCZOS)
        .quantize(colors=64)
        for i in range(len(stages))
    ]
    gif[0].save(MEDIA / "demo.gif", save_all=True, append_images=gif[1:],
                duration=4000, loop=0, optimize=True, disposal=2)
    frame(stages[4], 1).save(MEDIA / "demo-poster.png", optimize=True)
    frame(stages[5], 1, height=628).save(MEDIA / "social-card.png", optimize=True)
    with tempfile.TemporaryDirectory(prefix="t-handoff-frames-") as directory:
        temp = Path(directory)
        for i, still in enumerate(frames):
            still.save(temp / f"frame-{i:04d}.png")
        call(["ffmpeg", "-v", "error", "-y", "-framerate", "10",
              "-i", str(temp / "frame-%04d.png"), "-c:v", "libx264",
              "-pix_fmt", "yuv420p", "-b:v", "400k", "-minrate", "400k",
              "-maxrate", "400k", "-bufsize", "800k",
              "-x264-params", "nal-hrd=cbr:filler=1",
              "-movflags", "+faststart", str(MEDIA / "demo.mp4")], timeout=60)


if __name__ == "__main__":
    render(json.loads(CAPTURE.read_text(encoding="utf-8")))
