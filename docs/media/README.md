# Launch media

The editable [hero](hero.svg) and rendered [demo](demo.mp4), [GIF](demo.gif),
[poster](demo-poster.png), and [social card](social-card.png) use the AgentHangar
palette (`#141516`, `#1c1d1f`, `#f2a93b`) and system font fallbacks for Space
Grotesk / IBM Plex Mono. Graphics were drawn in SVG and Pillow; no stock or
generated artwork is embedded.

The published media comes from a real authenticated Codex CLI in a disposable
repository. `scripts/demo/real.py` used the installed `t` command to create an
isolated Git worktree and tmux slot, sent a harmless prompt asking for “Session
ready,” verified Codex’s response, checked that `t ls demo` marked the session
active, and ran `t cd demo 1`. The only retained output is the path-normalized
[`demo-capture.json`](demo-capture.json); temporary paths, account details, auth
state, and raw terminal logs are absent from the committed assets. The demo makes
no remote-handoff claim.

Run `python3 scripts/demo/real.py --render-capture` to regenerate all media from
the verified output without another model request. To capture afresh, use
`python3 scripts/demo/real.py --capture-only` first; a successful run writes a
new sanitized capture, then `python3 scripts/demo/real.py` renders it. This path
requires Git, zsh, tmux, ffmpeg, Pillow, an installed `t`, and a logged-in Codex
CLI. `python3 scripts/demo/render.py` remains a deterministic contributor demo
with an explicitly labeled idle agent stub.

The 1200×676 MP4 is silent, 18 seconds long, H.264 at 10 fps and about 397 Kbps
(894 KB). It meets [LinkedIn’s current video upload requirements](https://www.linkedin.com/help/linkedin/answer/a7486279)
for MP4, at least 75 KB, at least 192 Kbps, and 10–60 fps. The 960×540 GIF loops
through five readable beats in under 200 KB. The 1200×628 social card is static. The
website uses the MP4 with controls and a poster and does not autoplay it.
