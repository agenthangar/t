# Opt-in installed-tool advisory checks

Enable explicitly in your shell (or add this export to your shell configuration):

```sh
export T_VULNERABILITY_CHECK=1
t
```

Disable with `unset T_VULNERABILITY_CHECK` or `export T_VULNERABILITY_CHECK=0`.
Only the exact value `1` enables checks. Interactive **bare `t`** shows cached
results on stderr before onboarding or the session screen. Explicit commands,
help, redirected output, JSON and remote protocols do not trigger this check.
There is no new command or automatic opt-in prompt.

The foreground only reads a bounded local cache and launches a detached worker.
The first invocation reports pending; results appear on subsequent startups.
The worker checks at most once per day, or once per hour after errors. A
nonblocking process lock prevents concurrent workers from doing duplicate probes
or requests. Cache writes are atomic. Results include the last attempt time;
expired results are labeled stale. A failure retains previous successful results
as stale, never as a current clean bill of health. Disabling prevents new workers;
an already-running worker may finish its bounded check.

## Coverage and actions

Initial coverage is the **local `cloudflared` executable found on PATH**, on
Linux and macOS. Version discovery runs `cloudflared --version` with a three
second timeout. Cache results describe that sampled installation, not necessarily
a running daemon. PATH changes or upgrades may take up to one day to appear.
Remove `$XDG_CACHE_HOME/t/vulnerabilities.json` (default
`~/.cache/t/vulnerabilities.json`) to request another check on the next startup.

Sources:

* Published [Cloudflare repository advisories](https://github.com/cloudflare/cloudflared/security/advisories), fetched from the unauthenticated GitHub API.
* Cloudflare's [eMudhra certificate incident guidance](https://blog.cloudflare.com/emudhra-certificate-compromise/), reviewed October 26, 2026. The bundled recommendation flags cloudflared versions older than **2026.10.2** for review/update. This is a vendor remediation recommendation, not proof every older/custom build is exploitable. This rule changes through t updates; it is not a live scrape of the blog.

The matcher understands numeric three-part versions and comma-separated numeric
comparisons. Prerelease/custom versions and unrecognized advisory ranges or
platform/package labels are unknown and require manual review. The upstream
`windows 32-bit` installer advisory is not applied to Linux/macOS. Advisories
with an empty ecosystem are not silently treated as platform-independent.
Other tools, WARP (whose platform-specific versions differ), containers, remote
hosts, OS trust stores, private advisories and repository dependencies are not
covered. An empty response means no known findings in the checked sources,
never that the machine is secure. New tools require an explicit probe and
source/platform mapping with fixtures; unknown source formats fail honestly.

Findings include the installed version, source-provided affected/fixed versions
when interpretable, and an advisory URL. Follow the linked vendor guidance and
verify the deployed/running version yourself. t does not upgrade anything,
restart services, rotate credentials, or change trust/security settings.

## Network and privacy

The worker sends one bounded HTTPS GET to
`api.github.com/repos/cloudflare/cloudflared/security-advisories?per_page=100`
with an eight second timeout and a generic `t-advisory-check` user agent. It uses
no GitHub token. GitHub sees the requesting IP and ordinary HTTP metadata, but
not the installed version, PATH, repository contents, credentials or tool output.
Standard Python proxy/TLS environment settings apply. An incomplete/paginated,
malformed, rate-limited or offline response is unavailable, not a clean result.
The cache contains local findings and timestamps in a mode-0600 file; it can be
deleted without losing settings. No repository files are inspected or uploaded.
