# Standalone nosleep

Installed alongside `t`; no dotfiles or shell plugin is required. Start it manually
with `nosleep`. `sleep-manager` remains a separate dotfiles utility.

## Sleep and connection recovery

`nosleep` requests sudo when starting its system sleep hold and when needed to
restore normal sleep. Installing `t` does not grant passwordless sudo or start a
monitor. Existing narrowly scoped dotfiles sleep permissions continue to work.

```sh
nosleep                       # lock and turn the display off on lid close
nosleep -d on                  # display on, normal brightness
nosleep -d dim -l off          # dim without requesting a lock
nosleep -c none                # keep awake until Ctrl-C; skip all checks
nosleep -d dim -l off -c agent # dim, no lock, check only agent activity
nosleep -c internet            # check only internet connectivity
nosleep --require 1            # keep awake while busy OR online
nosleep --require 2            # require both enabled checks to pass
nosleep config                # arrow-key menu for this machine's defaults
nosleep config --show          # print effective defaults
nosleep config --edit          # edit defaults with VISUAL/EDITOR
```

Related choices use one mode flag per setting:

| Setting | Flag | Modes |
| --- | --- | --- |
| Display on lid close | `-d`, `--display` | `off`, `on`, `dim` |
| Lock on lid close | `-l`, `--lock` | `on`, `off` |
| Hotspot fallback | `--hotspot` | `on`, `off` |
| Automatic checks | `-c`, `--checks` | `all`, `agent`, `internet`, `none` |

Use a separate value or `=`: `-d dim`, `-d=dim`, and `--display=dim` are equivalent.
A display mode replaces the whole display choice: `on` clears saved or earlier
dimming, `off` turns the display off, and `dim` keeps it awake and dimmed.
`--checks agent` selects only agent activity, replacing saved check choices and
clearing forever; `none` disables both checks and holds until Ctrl-C.
Repeating a mode applies the last selection. Display and locking remain independent.
`--lock off` skips nosleep's lock request; it never unlocks an existing lock or
changes macOS's own automatic locking settings. Dimming only changes brightness.

Check settings remain separate: `-e` / `--every`, `-g` / `--grace`,
`-r` / `--retries`, and `-b` / `--backoff`, each taking a number.
Only enabled checks can trigger retries or allow automatic sleep. Lid/display
handling, sleep protection, sudo refresh, and cleanup continue with checks disabled.

Existing switches and groups still work. `-mLI` means dim, no lock, and no internet
check; `-dML` means display on, no saved dimming, and no lock. Bare `-d` preserves
saved dimming, while `-d on` explicitly clears it. Bare `-l` / `--lock` enables
locking, and bare `-c` / `--checks` enables both checks and clears forever.
The individual `-a` / `-A` and `-i` / `-I` check toggles are also retained.
`-f` / `--forever` and `-C` / `--no-checks` override individual check settings;
`-F` / `--no-forever` resumes the selected checks.

Grouped numeric options accept attached or separate values: `-mLIe10` and
`-mLIe 10` both use a 10-second interval. Mode values need a separate argument
or `=`: `-Ld=dim` and `-Ld dim` combine no-lock with display dimming.
Put further flags in a new argument after a value. The legacy `-dim` spelling
still means only `--dim`; it does not enable the internet check.

Among legacy display switches, `-m` / `--dim` / `-dim` wins regardless of order.
`-M` / `--undim` clears saved dimming without changing the display mode;
`-D` / `--display-off` / `--no-dim` turns the display off. A later
`--display MODE` replaces the whole choice, including an earlier explicit dim
request. A later legacy `--dim` requests dimming again.

`nosleep config` follows `t config`: select a setting with arrow keys, stage
changes, then review and save. Cancel leaves defaults unchanged. Defaults live
in `${XDG_CONFIG_HOME:-~/.config}/nosleep/config.json`, apply to the next run,
and stay local to that machine. Command-line flags override them for one run.
The display setting cycles through Off, On, and Dim (`display_on` and `dim`
in JSON); dimming implies display-on. Use `--display-off` to override saved
dimming, or `--display-off -d` to keep the display on without saved dimming.
An explicit `--dim` still takes precedence over legacy overrides; `--display on`
or `--display off` replaces it. Use `--lock on` /
`--no-forever` to override the other modes.
Timing defaults use `--grace`, `--every`, `--retries`, and
`--backoff`. `nosleep --help` highlights options that change the built-in defaults.
Configuration commands never change power settings or an already-running instance.
The menu groups display and locking under **On close**, the automatic-check
switch and individual agent/internet toggles under **Checks**, and their
interval, grace threshold, retry limit, and backoff under **Check settings**.
The toggles are `agent` and `internet` booleans in JSON, both defaulting to true.
Automatic checks can be disabled without forgetting those preferences.
Help, saved defaults, and live status use the same separation. Check settings
are marked inactive when neither check runs.

`nosleep` shows a live snapshot instead of a long startup message:

```text
nosleep

Live status
  Sleep     Prevented
  Agents    Sampling activity… need a second check
  Internet  Online · checked 0s ago
  Next      Check in 30s

On close
  Display   Off
  Locking   Lock screen

Checks
  Mode      Activity and internet checks

Check settings
  Interval  30s
  Grace     15m before retrying
  Retries   3
  Backoff   30s, doubles to 5m

  Options   nosleep --help · nosleep config
  Stop      Ctrl-C
```

Live status refreshes every two seconds with current activity and connection checks
and a countdown to the next check. Sleep reports whether sleep prevention is
active. On close shows the selected display and locking behavior. Checks
shows the monitoring mode; Check settings shows its timing and retry thresholds.
Use `nosleep --help` for one-run
options or `nosleep config` to save defaults. In `--forever` mode, unused timing
controls are marked inactive.
Agents shows activity observed at the last check, then switches to quiet
with a last-seen time when activity stops. Internet shows the latest connection
result; Next shows remaining grace or the pending retry when needed. Failures to
hold sleep off, lock, dim, or restore brightness appear as a Notice when relevant.
`--forever` explicitly marks agent and internet checks as disabled. Redirected
output and basic terminals get plain snapshots at each check, without cursor codes.
Interactive terminals use green for healthy states, amber for sampling, quiet
periods, grace and retries, and red for lost connectivity or sleep-protection
warnings. Bold labels, cyan section headings, and a muted footer keep the current
state prominent.
Use `NO_COLOR=1 nosleep` for an unstyled snapshot with live updates.

While the laptop is open, `nosleep` keeps the display on and inhibits idle locking,
so an unlocked session can stay unlocked. It never unlocks an existing lock.
Closing the laptop normally requests a screen lock and turns the display off;
opening it restores the display hold. Unlocking is manual.
`nosleep --display-on` keeps the display awake on close without dimming;
`nosleep --dim` keeps it awake and dims the built-in panel. The independent
locking setting controls whether it also requests a lock; `--no-lock` skips it. Docked clamshell mode keeps the external
display on and skips the lock/dim action when you close the laptop. The On close
section shows the selected display and locking behavior.

`nosleep` keeps the Mac awake until Ctrl-C while the laptop is open. Failed activity
or internet checks only produce warnings in that state. Automatic sleep is allowed
only when the laptop is closed and, by default, activity or internet connectivity remains absent
through the 15-minute grace period and three extra checks (after 30, 60, and 120 seconds).
Closing the laptop still locks or dims its display during retries.
Opening the laptop cancels pending retries; closing it starts a fresh retry
budget. Recovery resets the retry budget; a restored connection also restarts
activity sampling and grace so an agent has time to resume.

```sh
nosleep --grace 900 --retries 3 --backoff 30
```

`--require all` (the default) keeps awake while all enabled checks pass.
`--require 1` keeps awake while any enabled check passes; `--require N`
requires at least N enabled checks to pass. Passing means agent activity is busy
or internet connectivity is online. Each failing check retains its grace period
(and activity needs two samples) before it counts against the requirement.
All enabled checks continue to run, even after enough pass. Counts must be positive
and cannot exceed the final enabled check count when monitoring is active.
`--require none` skips monitoring until Ctrl-C while preserving selected checks.
Save the policy in `nosleep config` under **Passing checks required** (all, none,
1, or 2), or set `requirement` in JSON to `"all"`, `"none"`, or a positive integer.
Command-line `--require` values override that saved policy for one run; separate
and `=` values work, and the last value wins. The policy is independent of
`--checks` selection and `--forever`: `--require all` does not clear forever,
and `--no-forever` does not clear `--require none`. Timing and hotspot fallback
are inactive whenever monitoring is disabled. A numeric policy is checked against
the final selection when monitoring starts; `--checks all` can restore both checks.

`--every` sets the normal check interval (30 seconds). `--backoff` sets the first
retry delay (1–300 seconds), which doubles up to five minutes; `--retries` accepts
0–10. `--forever` holds until Ctrl-C. Connectivity checks try both Anthropic and
OpenAI, so a single provider being unreachable does not mean the network is down.

For native reconnection, enable **System Settings → Wi-Fi → Details → Automatically
join this network** for each trusted Wi-Fi network. On macOS Tahoe 26 or later,
set **Wi-Fi → Ask to join hotspots → Automatic**. Keep the nearby iPhone/iPad's
Wi-Fi and Bluetooth on and use the same Apple Account (or configured Family
Sharing), with a cellular plan that supports Personal Hotspot. See Apple's
[Wi-Fi settings](https://support.apple.com/guide/mac-help/mh11935/mac) and
[Instant Hotspot guide](https://support.apple.com/en-us/109321).

macOS attempts auto-join while `nosleep` waits. Hotspot auto-join applies when no
known Wi-Fi is available; it may not leave connected Wi-Fi whose internet uplink
is broken. `nosleep` does not toggle Wi-Fi or change those auto-join settings.
These auto-join settings are per-machine and are not changed by `dots --all`.

Configure fallback from **t config → Nosleep** or `nosleep config`; no separate
setup command is needed. The nosleep editor saves independently of the hub, so
save or discard pending hub changes before opening it. Settings sections are
separated by blank rail lines and remain navigable in small terminals.

Select **Hotspot Wi-Fi name** to discover nearby networks, refresh the list, or
enter a name manually. Select one trusted fallback network. The current connection
is marked, duplicate SSIDs are combined, and other networks are ordered by signal
strength. A saved network remains selectable when it is not currently detected.

Use **Enable network discovery** to set up the local macOS helper and request
Location permission. Apple requires Location access to expose Wi-Fi names; allow
**Nosleep Wi-Fi** under **System Settings → Privacy & Security → Location Services**.
If permission is denied or Location Services are off, config explains how to fix
it. Discovery does not collect geographic coordinates. The helper is built locally
on explicit setup using Apple command-line tools (`xcode-select --install` if
missing). It is stored under `~/Library/Application Support/t/nosleep`, with separate
identities for different source checkouts. Unchanged builds reuse the helper;
a rebuild or source change may require permission again. No background service is
installed, and normal monitoring never requests permission or installs the helper.

Selecting a new network stages its password setup. **Set/change hotspot password**
also stages a credential update. Review and save first, then macOS `security`
prompts privately for the password. Configuration is written only after credential
storage succeeds. Passwords never appear in arguments, logs, review output or JSON.
The existing `nosleep hotspot setup` command remains compatible.

```sh
t config                       # select Nosleep
nosleep config                 # open the same editor directly
nosleep --hotspot off           # disable fallback for this run
nosleep --hotspot=on            # enable the configured fallback for this run
```

The default outage delay is five minutes. Configure the delay and bounded attempt
limit (1–300 seconds, default 30) in the same editor.

The live **Hotspot** row reports fallback policy; **Network** reports the selected
SSID and whether it is connected, available but not connected, not detected, or
unknown. Wi-Fi off and permission problems have separate messages. Availability
is checked every 30 seconds, including when fallback or automatic checks are off.
Scans have a 10-second native deadline and do not block the sleep-control loop.
Expired observations become unknown. Availability means the SSID was observed;
it does not prove credentials, internet access, or the phone's hotspot toggle.
Without a configured network, the row says **Not configured**.

After the delay, `nosleep` makes one directed join to that exact SSID with
`networksetup`, even if the current Wi-Fi uplink is broken. This can work with the
lid closed when the hotspot is advertising, Wi-Fi is already on, Keychain is
accessible and macOS permits the Wi-Fi change. Discovery sees broadcasting Wi-Fi networks; it does not wake a dormant Instant Hotspot. Turn on Personal Hotspot's **Allow Others to
Join** and keep it broadcasting nearby. A locked Keychain or permission policy
can prevent joining; unlock the Mac and use config to repair credentials.

Internet checks verify recovery independently of a successful association.
Automatic sleep waits for the outage delay and bounded attempt/recovery window,
including when activity retries finish early. Recovery resets the timer for the
next outage. Ctrl-C cancels the owned helper and releases the sleep hold.
Fallback is inactive with `--forever`, `--no-internet`, or `--checks none`.
It is off by default and is never enabled on other Macs by `dots --all`.
