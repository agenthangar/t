# Automatic session recovery

Open **`t config` → Recovery → Automatic recovery**, then review and save.
This is an opt-in setting for the current Mac. Installation leaves it disabled;
each remote Mac must be configured separately. Enable it from the installed
version outside a development worktree so the login service uses a stable source.

The user LaunchAgent runs after login, including after a reboot. It restores
eligible CLI conversations in detached tmux sessions on their original host and
in their existing workspace. It does not unlock FileVault, log you in, open
desktop applications, or recreate deleted worktrees. `nosleep` remains manual.

```sh
t system recovery status
t system recovery status --json
t session recovery status api 1
t session recovery pause api 1
t session recovery resume api 1
t session open api 1
```

Session controls also accept an exact conversation ID, a slot inside a registered
repository, or no target when the current workspace identifies one enrolled
conversation. Ambiguous targets fail without a picker. Pause and resume change
recovery eligibility; they never stop an agent or submit a task. Disabling recovery
in `t config` removes its service and hooks while running agents continue.

## When work continues

Codex, Claude, and Cursor CLI launches managed by `t` record their exact
conversation, workspace identity, explicit launch settings, and native lifecycle
events. A verified unfinished turn may receive one continuation instruction after
reboot or a final transport failure. It tells the agent to inspect existing files
and tool results and verify uncertain external actions before repeating them.
Idle conversations and conversations waiting for approval or user input are
restored without a continuation. User interruptions and explicit closes remain
stopped. Moving a session or switching to local fallback retires its original
recovery intent.

Recovery reuses live owners and the existing local/remote launch locks. It saves
visible pane text before replacing a verified failed client, waits for that
client's supervisor to exit, and never kills a shared Codex server. Restored Codex
sessions use a private backend. Native approvals stay native; recovery does not
answer them. A pending continuation with uncertain delivery requires manual
inspection instead of another automatic submission.

Connectivity waits have no attempt limit. Exponential backoff with jitter is
capped at five minutes. Three other startup failures stop automatic attempts and
show an attention reason in status. Correct the cause and use session recovery
resume to restore eligibility. An uncertain delivery remains blocked until a
manual native resume establishes a new session generation.

## Capability boundaries

Existing sessions are enrolled conservatively when enabled. They can be restored
without a new prompt when their launch settings can be recovered exactly; they
need a subsequent managed launch for full lifecycle observation. Unknown settings
or evidence require manual recovery. Codex profile, remote-server, OSS, and other
unsupported invocation modes still launch normally without automatic supervision.
Cursor recovery waits while a native Cursor CLI could still own a conversation.
Desktop applications and independently launched CLIs are outside this service.

For an existing remote tmux session opened through `t`'s managed attach path,
connection recovery pins the conversation and workspace before attaching. SSH
transport loss retries that same destination, without creating an agent. A
changed/reused slot, authentication failure, host-key error, normal detach, or
Ctrl-C stops reconnecting. Remote foreground and initial creation paths keep
their existing behavior. Remote reboot recovery requires enabling recovery on
that remote Mac as well.

The adapters use [Codex app-server lifecycle events](https://developers.openai.com/codex/app-server),
[Claude hooks](https://code.claude.com/docs/en/hooks), and
[Cursor hooks](https://cursor.com/docs/hooks). Missing or ambiguous lifecycle
evidence never authorizes a continuation.

## Local state and diagnostics

The setting lives in `${XDG_CONFIG_HOME:-~/.config}/t/recovery.json`.
Private journal, locks, heartbeat, diagnostics, and captured drafts live in
`${XDG_STATE_HOME:-~/.local/state}/t/recovery/`. Files are written atomically with
private permissions. The journal stores allowlisted launch options, not initial
prompts or credentials; status excludes command lines, transcripts, and drafts.
The LaunchAgent is `~/Library/LaunchAgents/com.agenthangar.t.recovery.plist`.
Owned hooks are merged with existing settings and removed individually on disable.

If the service reports stopped, revisit Recovery and save to repair its login
registration. Status is read-only and never starts the service. No machine reboot
or desktop automation is needed for the isolated test suite.
