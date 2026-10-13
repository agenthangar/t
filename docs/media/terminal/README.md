# Featured: a real bug fix across surfaces

`workflow.mp4` / `workflow.gif` follow one disposable **Fix task CLI status
filtering** conversation using GPT-6.1-Sol.

1. `t open demo 1`; ask the agent to reproduce and fix `--status` filtering.
2. The agent runs three tests (two fail), edits `tasks.py`, adds two regression
   tests, and reruns the suite (all five pass). The real CLI prints only open tasks.
3. `t beam demo 1 --host mini --detach`; run the same five tests and CLI on mini;
   `t beam demo 1 --from mini --detach` returns the same task.
4. `t app demo 1 --reuse-window --no-preview --no-plan`; `t ls demo` verifies the
   same model, active context, task title, and desktop owner.
5. Archive only the demo chat, `t app pull demo 1 --thread <demo-thread>`,
   end that demo CLI, then
   `t resume demo 1`. Ask the resumed agent to verify the same fix and tests.

The `t app` invocation is the configurable desktop-handoff form; ordinary use is
`t open demo 1 --app`. Window reuse avoids creating another app window during capture.
The resume picker selects the named coding task when multiple saved chats exist.
The explicit `t app pull --thread` selects this demo conversation because the
fixture also contains the earlier checklist chat; `t open demo 1 --cli` is the
ordinary return command for an unambiguous worktree.

The before-state source and actual agent-produced patch are in
[`fixture`](fixture/README.md). The fixture was
independently replayed: two failures before the patch, five passing tests after it.
`t beam` checkpoints the working edits into a local WIP commit when transferring.

Captured October 11, 2026. The task ID was checked against the copied rollout on
mini. The desktop's loaded-thread writer ownership was independently checked.
The resumed terminal contains the same code-fix conversation.

**Limitations:** mini's Codex is not logged in. The task, transcript, and code really
moved to mini and back; tests ran there, but no remote model turn ran. The app window
was not filmed. Desktop archival and ending the returned demo CLI happened between
chapters. Terminal output is original PTY output. The video adds editorial chapter
labels, joins captures in order, and removes idle gaps and repeated output.
The featured cut is 66 seconds, including a two-second editorial title card.
`render-cover.py` generates that card and the matching video poster. Reading sections play at normal speed. Session setup plays at 2×, marked
on screen. The desktop-to-CLI return occurs between chapters. Setup retries are
omitted; the final transfer retry follows the initial WIP checkpoint. This is an
edited demonstration, not an uninterrupted screen recording. No command results
are invented. The replay preserves ANSI foreground/background colors, bold,
reverse video, and underlines. A presentation theme adds cyan prompts, green
additions/passing results, and red removals/failures to monochrome source text;
these accents are editorial styling, not a claim about the original CLI theme.

The fixture uses isolated configuration, repositories, and tmux sockets on both
hosts, a demo-scoped SSH wrapper to pass that environment, and a temporary Codex
`--no-daemon` launcher. Repository paths, host address, and task IDs are redacted.
The private fixture configuration and authentication material are not included.

Capture actual commands using `capture-step.py` with a private state
file and output directory. `--select-after` presses Enter once for a previously
observed picker selection; `--detach` detaches the tmux client. Inspect the real
outputs and verify ownership before sanitizing sources for the repository.

Regenerate offline (Python 3, Pillow, pyte, ffmpeg):

```sh
python3 -m venv /tmp/t-media-venv
/tmp/t-media-venv/bin/pip install Pillow pyte
/tmp/t-media-venv/bin/python docs/media/terminal/render-flow.py
```

`workflow.json` defines chapter sources, trims, evidence, and limitations.
Related work: [site #5](https://github.com/agenthangar/agenthangar.github.io/issues/5)
and [t #107](https://github.com/agenthangar/t/issues/107).
