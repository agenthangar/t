# Session manager design

The primary job is to find a session, understand its repository and host, and open or manage that exact session. The interface remains a session register; conversation belongs in the attached terminal or agent application.

## Direction and critique

Use paper `#ffffff`, ground `#f6f7f7`, slate ink `#263238`, rules `#dbe2e5`, blue `#285d83`, and amber `#865c1b` for attention. System sans keeps native controls and task titles familiar; monospace is reserved for branch and path data. Left-aligned, compact rows put task, session state, agent, host, and this window's terminal attachment on consistent axes. Repository/host navigation stays separate from status filtering. The inspector moves below the register on smaller windows.

```text
Repository / host | Scope                         New session
navigation        | Search             Group / sort
                  | Reported-state filters
                  | Task | State | Agent | Host | This window | Open
                  | Selected session details (beside or below)
                  | Terminal tabs, only after opening a terminal
```

The first critique removed large repeated cards, decorative terminal icons, an always-empty terminal, uppercase headings, and a general workspace slogan. Density serves comparison across sessions rather than adding a dashboard. Blue marks the inspected session; amber is reserved for explicit attention states. Selection inspects; the separate Open button attaches. Detaching a terminal preserves the session.

## Current industry references

Reviewed 28 October 2026 using primary product documentation:

- [Warp's Agent Management Panel](https://docs.warp.dev/platform/managing-cloud-agents/) demonstrates scannable run status, filtering, and drilldown. Borrow the overview-to-detail pattern; do not borrow its activity claims without equivalent backend signals.
- [Linear display options](https://linear.app/docs/display-options) distinguish filtering from grouping and ordering. Keep these controls separate. [Linear selection](https://linear.app/docs/select-issues) distinguishes highlighting from selection; inspection and explicit actions remain separate here.
- [GitButler's branches page](https://docs.gitbutler.com/features/branch-management/branch-lanes) keeps parallel branch context available while inspecting work. Preserve repository, branch, and worktree identity in rows and details.

## Truthful state and scope

The actual core commonly reports runtime states such as `attached`, `detached`, and `app`. These become Tmux attached, Tmux detached, and External app; they do not establish whether an agent is working or waiting for input. Filters for attention, working, or idle appear only if explicitly reported. Missing activity stays unknown; incomplete hosts are unverified. This window's attached terminal is a separate column. External app sessions cannot use the embedded tmux terminal.

The standalone preview uses visibly synthetic activity states and includes an attention filter to explore that future interaction. Its sample Git changes and timestamps are demo data; production shows only actual available fields. Neither UI invents live progress, transcript activity, or Git changes. Preview actions never reach the production bridge.

## Validation

Unit tests cover runtime/activity distinctions and uncertainty. Headless browser tests cover typed review/apply, filters/grouping/search, explicit attachment and detach, exact lifecycle targets while another terminal is active, creation scope, failed attachment reconnect, operation recovery, and issue-result failures. Light/dark screenshots at 1320, 1024, 736, and 320 pixels check layout and document overflow. Native helper and application validation are separate from these frontend fixture checks.
