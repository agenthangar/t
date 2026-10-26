# Repository-tool advisory checks (opt-in)

`t` inventories installed executables relevant to **registered local repositories**
and matches verified public package identities and installed versions against
[OSV's structured advisory API](https://google.github.io/osv.dev/post-v1-query/).
It never runs discovered binaries, repository scripts, package installers or
manifest hooks. Cloudflare motivated this feature; it is not a scanner target
allowlist and there is no special Cloudflare-only verdict.

## Startup checks

```sh
export T_VULNERABILITY_CHECK=1
t
# Disable future startup checks:
unset T_VULNERABILITY_CHECK
```

Only exact `1` opts interactive **bare `t`** into cached reporting and detached
refresh. Explicit commands, help, redirected output and machine protocols do
not trigger startup checks. The foreground only reads bounded regular cache
files and starts a detached worker. The first startup is pending; later startups
show new/changed results, with at most ten lines and a full-report hint. Identical
results are not reannounced. This setting does not enable the periodic service.

## Optional periodic web checks

```sh
t system advisories enable                     # default: every 24 hours
t system advisories enable --interval-hours 6  # 1–720 hours
t system advisories status                     # read-only status + full report
t system advisories disable
```

Enable explicitly starts a detached per-user process which continues checking
without further interactive `t` launches. **Shipping/installing t does not enable
it.** There is no launchd/systemd registration, boot autostart, external scheduled
automation, desktop notification permission or external messaging integration.
After logout/reboot/process termination, status can show enabled but stopped;
run enable again to restart it. Enable requests startup; verify running status.
Disable stops the loop after the current bounded request and prevents more
requests. It does not kill an arbitrary cached PID. Startup environment opt-in
is separate: unset it too to disable both paths. A worker already in a request
may finish that request. SIGTERM/SIGINT also stop the periodic worker cleanly.

A per-user lock prevents overlapping daemon owners; a second lock prevents
startup and periodic workers from issuing duplicate simultaneous checks. A rapid
re-enable allows up to ten seconds for a previous owner to drain. Successful
checks use the configured interval; errors retry after at most an hour. Startup
checks alone use a daily cache. Requests have eight-second socket timeouts,
maximum 1 MiB responses, a 50-distinct-package and 120-second inter-request work
budget (an in-flight request can finish afterward). Pagination/oversized or
malformed responses are incomplete, never clean. A stopping worker checks for
cancellation between package requests.

New/changed findings enter a **local notices inbox**; unchanged findings do not
create new events. Failed checks retain the prior notice baseline. Disappearance
of a finding is labeled a coverage change, not proof it was fixed. Use status
for the cached report and recent notice history; opted-in bare t also announces
changed results. There are no off-machine or desktop push notifications.

## Inventory and identity provenance

The detached worker reads t's existing generated config bridge
`$XDG_CONFIG_HOME/t/config.sh` (default `~/.config/t/config.sh`) as data, never as
shell code. `DEV_REPOS` selects checkouts and explicit `DEV_AGENT` entries add
repo-associated agent executables. No unrelated repository or PATH-wide binary
scan is performed. Results retain local repository provenance and installed
package versions. Queries deduplicate by ecosystem/name/version across repos.

| Metadata | Installed evidence and advisory coverage |
| --- | --- |
| npm `package-lock.json` v2/v3 | Enumerates installed locked packages with `bin` declarations, including nested packages. Reads actual installed `package.json`, verifies bin files, exact locked/installed version agreement and an npm registry tarball URL for that package. Public unscoped packages map to OSV `npm`. Missing binaries, private/local/git/custom-registry sources and version drift are unknown. Scoped packages remain unsupported because the npm hostname alone cannot establish that a scoped package is public. |
| Python `.venv` or `venv`, plus `uv.lock` | Reads `*.dist-info/METADATA` and `entry_points.txt`, verifies installed console-script files, and requires exact installed name/version agreement with a public `https://pypi.org/simple` lock source. Normalizes PyPI names; maps to OSV `PyPI`. Editable/direct-URL installs and unverified/private sources are unsupported. No Python code or entry point is imported/executed. |
| `.tool-versions`, `mise.toml`/`.mise.toml` `[tools]`, `package.json` engines, explicit t agents | Resolves those declared tools on PATH without executing them. Global npm executables may map through their installed package metadata and `.package-lock.json`. Cargo-installed executables may map through `$CARGO_HOME/.crates2.json` with a verified crates.io registry record to OSV `crates.io`. Everything else is visible as missing or unsupported, including native/system/Homebrew binaries. Desired tool versions are never substituted for installed versions. |
| Literal `brew "formula"` lines in a root `Brewfile` | Reads installed Cellar `bin`/`sbin` files at `$HOMEBREW_PREFIX`, `/opt/homebrew` or `/usr/local`. Reports formula/version provenance and executable paths, **without inventing an OSV ecosystem mapping for formula names**. Dynamic Ruby, casks and other Brewfile constructs are explicitly unsupported and never evaluated. |

This is not universal system coverage. Unsupported metadata forms (for example
pnpm/yarn-only installs, arbitrary Makefiles/shell scripts, global Python tools
without supported installed metadata, native distro packages and container/remote
images) do not establish a clean scan. Repo declarations and metadata determine
relevance; binaries used only implicitly in scripts cannot be inferred safely.
Reports explicitly call out missing supported metadata, unmappable tools, unknown
versions and inventory limits. TOML adapters require Python 3.11+; older Python
keeps JSON/tool-version discovery and reports TOML coverage unavailable.

OSV performs ecosystem-specific matching using the exact installed version in
the query. t verifies returned affected-package identity, ignores withdrawn
records, links to the source record, and displays its affected/fixed ranges and
available platform/build conditions. It does not reinterpret Python, npm or Rust
versions using a single numeric comparator. A match requires review of vendor
platform/build applicability; the displayed source fixes are not an automatic
upgrade plan. Empty OSV results mean **no known findings for that queried
package/version**, never that all binaries or the instance are secure.

## Cache, privacy and limits

The report is at `$XDG_CACHE_HOME/t/vulnerabilities.json` (default `~/.cache/t`),
with atomic mode-0600 writes. It includes local provenance, results and timestamps;
stale results/errors are labeled. Delete that report to request a new startup
check. Inventory/installed-version changes can take until the next scheduled
check to appear. The schema invalidates earlier cloudflared-only caches. Reports
are bounded and explicitly mark truncation. Metadata files are limited to 1 MiB;
inventory is bounded to 100 registered repos and 500 executable rows.

Periodic configuration, the worker lock and the bounded local notices inbox live
under `$XDG_STATE_HOME/t/advisories` (default `~/.local/state/t/advisories`). No
credentials are stored. Disabling does not delete historical reports/notices.

Only a verified public **ecosystem, package name and installed version** are sent
in HTTPS POST requests to `api.osv.dev/v1/query`, with a generic user agent and no
auth token. OSV receives the client IP and those package identities/versions.
Repository paths/aliases, manifests, lockfiles, scripts, file hashes, source
contents, environment variables and credentials are never uploaded. Private or
unverified identities are not queried. Normal Python TLS/proxy settings apply.
This is advisory reporting only: no upgrades, service restarts, credential
rotation, security/trust changes or host remediation.
