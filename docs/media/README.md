# Launch media

The editable [hero](hero.svg) and rendered [demo](demo.mp4), [GIF](demo.gif),
[poster](demo-poster.png), and [social card](social-card.png) use the AgentHangar
palette (`#141516`, `#1c1d1f`, `#f2a93b`) and system font fallbacks for Space
Grotesk / IBM Plex Mono. Graphics were drawn in SVG and Pillow; no stock or
generated artwork is embedded.

Run `python3 scripts/demo/render.py` from a checkout with Git, zsh, tmux, ffmpeg,
and Pillow available to regenerate the raster and video assets. The script creates
a disposable repository, local Git origin, home directory, config, and tmux socket;
it invokes the real `t ls`, `t open --new`, and `t cd` commands. A shell script
named `claude` stays idle inside the tmux slot. It never invokes an actual model,
records a conversation, contacts a remote host, or sends data elsewhere. Paths in
the visual are shortened from the disposable home to `~`; the displayed command
output is otherwise taken from the captured run. The demo therefore proves the
local worktree/session flow, while real-agent and cross-host release checks remain
separate.

After a tested standalone release is installed on the recording machine, run
`python3 scripts/demo/real.py --capture-only` to verify a real Codex session. It
uses the existing Codex login, a disposable local Git repository and tmux socket,
and a prompt that asks only for “Session ready.” It refuses to replace media unless
the session starts, answers, appears active in `t ls`, and its worktree resolves.
Then run `python3 scripts/demo/real.py` to render the real-agent version. Keep the
stub version as the contributor reproduction path. A separate cross-host smoke
must validate `t beam` before a remote beat can be added to any video; the local
recording makes no remote claim.

The 1200×676 MP4 is silent, eight seconds long, and encoded with H.264; the
960×540 GIF loops through four readable beats and stays well under 5 MB. The
1200×628 social card is static. The website uses the MP4 with controls and a
poster, and does not autoplay it.
