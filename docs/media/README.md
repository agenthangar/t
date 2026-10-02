# Launch media

The editable [hero](hero.svg) and rendered [demo](demo.mp4), [GIF](demo.gif),
[poster](demo-poster.png), and [social card](social-card.png) use the AgentHangar
palette (`#141516`, `#1c1d1f`, `#f2a93b`) and system font fallbacks for Space
Grotesk / IBM Plex Mono. Graphics were drawn in SVG and Pillow; no stock or
generated artwork is embedded.

The video is an **edited, rendered walkthrough from actual CLI and host output**,
not a literal screen recording. Its source is the sanitized
[handoff capture](handoff-capture.json). On 2026-10-02, a disposable Codex CLI
0.160.0 session in `t` slot 41 answered on a Mac, moved to a configured Mac mini,
answered there, returned to the Mac with that mini reply visible in the same
thread, and answered again. Both `t beam` commands exited successfully. Each
source tmux owner stopped before its destination became the live owner. We
checked the thread ID on both hosts and verified the rollout files met Codex's
SQLite projection checkpoints before each transfer. The returned Mac JSONL and
terminal pane both contained the mini reply. The capture retains short observed
excerpts only; full private transcripts, account identity, absolute home paths,
thread UUID, host address, authentication state, and unrelated sessions are not
committed.

The final `t app t 41 --reuse-window --no-preview --no-plan` command exited 0,
stopped the Codex CLI, and kept the worktree slot reserved. It reported an
**open request sent to macOS**, not confirmed delivery inside the Codex desktop
app. The recording could not inspect the app UI. `--reuse-window` was needed in
the disposable one-shot terminal context because automatic new-window creation
requires macOS Accessibility permission for the invoking terminal. The video
labels this stage as an app request and says delivery is unconfirmed. It does
not show a desktop conversation or a web preview.

Run `python3 scripts/demo/handoff_render.py` to regenerate the media from the
committed sanitized capture without another model request. To make a new capture,
use a disposable repository slot on two configured hosts, run the observed
commands, verify one live owner and transcript continuity on each transfer,
redact the output into `handoff-capture.json`, then render. The renderer refuses
to advertise a returned conversation unless its evidence flags and observed
output are present. It requires Python 3, Pillow, and ffmpeg. The earlier
[local-only capture](demo-capture.json) and `scripts/demo/real.py` remain as a
historical source for the original local demo; do not use that script to
regenerate these handoff assets. `scripts/demo/render.py` is a deterministic
contributor mock with an explicitly labeled idle agent stub.

The silent 1200×676 MP4 runs 24 seconds at 10 fps, H.264, about 397 Kbps and
1.2 MB. It meets [LinkedIn's current video upload requirements](https://www.linkedin.com/help/linkedin/answer/a7486279)
for MP4, at least 75 KB, at least 192 Kbps, and 10–60 fps. The 960×540 GIF
loops through six four-second beats and is 279 KB. The 1200×628 social card is
static. The website uses the MP4 with controls and a poster; it does not autoplay.
