# Logging in agent tools

`t login` (also `t agent login`) reviews local Claude Code, Codex, and Cursor
CLI authentication and offers the vendor's login command for logged-out tools
or cached access tokens expiring within **24 hours**. Use `--within-hours N`
to change that window (0 means already expired, maximum 8760 hours).

```sh
t login --dry-run                       # inspect the plan without sign-in
t login --ignore codex                  # skip Codex entirely this invocation
t login claude codex --within-hours 2   # inspect only these tools
t login codex --force --headless        # explicitly sign in again
t login --ignore codex --ignore cursor  # ignore flags can be repeated
```

`--ignore` is deliberately **invocation-only**, including for broken tools: an
ignored executable is never probed. No preference file is written. Repeat the
flag on later runs, or name just the tools you want. Missing tools are skipped
when scanning all tools; an explicitly named missing tool produces a nonzero
exit. Nothing is installed, updated, or propagated to remote hosts.

The plan prints each login command before asking for confirmation. `-y` skips
that confirmation, but the vendor may still require a browser/device-code step.
`--dry-run` and `--status` only inspect state and print the plan. Failed or
unrecognized status probes are reported separately from logged-out accounts;
`t` does not automatically attempt login for those errors. `--force` requires
named tools and explicitly requests login even if status is unknown or healthy.
One failed login does not stop other selected tools. Interrupting stops the run.
A successful login exit is followed by a fresh status probe. Status/login failures
return 1; invalid arguments or a required confirmation without a terminal return 2.

## What expiry means

There is no uniform vendor CLI expiry interface. Cached access-token expiry is
an **advisory hint**, not a prediction of session failure: the vendor may refresh
the access token automatically. `t` does not refresh tokens, log out accounts,
write credential stores, or print captured status output or token contents.
The vendor login process inherits the terminal and manages its own credentials.
A logged-in status is not proof that a model request will succeed.

| Tool | Status and login | Best-effort expiry hint |
| --- | --- | --- |
| Claude Code | `claude auth status`, `claude auth login` | On non-macOS systems, `claudeAiOauth.expiresAt` (milliseconds) from `$CLAUDE_CONFIG_DIR/.credentials.json`, default `~/.claude/.credentials.json`. This cache format is not a documented stable CLI contract. |
| Codex | `codex login status`, `codex login` | JWT `exp` from `tokens.access_token` in `$CODEX_HOME/auth.json`, default `~/.codex/auth.json`, for file-based ChatGPT authentication. JWT contents are decoded only as a local hint, not verified as authentication. |
| Cursor | `cursor-agent status`, `cursor-agent login` | Unknown: no supported expiry field has been established. |

Keychain/alternate stores, opaque tokens, missing/unreadable/malformed metadata,
and recognized API-key/environment authentication paths leave expiry unknown.
Claude on macOS always leaves expiry unknown rather than reading a possibly stale
file alongside Keychain. Codex skips file expiry when `cli_auth_credentials_store`
is not `file`; parsing an existing config requires Python 3.11+ (`tomllib`), otherwise
expiry is unknown. File metadata can still be stale, and cannot establish refresh
or revocation behavior. Unknown expiry is displayed and does not trigger login;
use `t login TOOL --force` if you need to sign in again.

Over SSH or with `--headless`, Codex uses `login --device-auth`; Cursor uses
`NO_OPEN_BROWSER=1 cursor-agent login`; Claude uses `auth login` and its own
instructions. This does not enable device authentication in account settings.

References inspected for the draft:

- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference): auth status and login commands.
- [Codex authentication](https://developers.openai.com/codex/auth/): file/credential-store modes and device authentication.
- [Codex auth storage source](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/storage.rs) and [token source](https://github.com/openai/codex/blob/main/codex-rs/login/src/token_data.rs): cache structure and JWT expiration.
- [Cursor authentication](https://cursor.com/docs/cli/reference/authentication): status/login and `NO_OPEN_BROWSER`; current docs use the `agent` spelling, while `t` retains its existing `cursor-agent` binary contract.

## Automated acceptance runner

Run the opt-in runner from this branch on the machine with the vendor CLIs and
accounts you want to verify. Prefer a dedicated OS test user with a clean vendor
configuration, particularly on macOS where Keychain is per-user. Existing vendor
accounts are sufficient; separate paid subscriptions are not inherently required.
The runner uses that user's existing credential stores and never copies them.

```sh
# Unattended status, dry-run plan, and ignore checks; no sign-ins or model requests.
python3 scripts/login-acceptance.py --report /tmp/login-inspect.json

# Authorize real browser sign-ins, then one small model request per tool.
# Run in your terminal and complete the vendors' browser/MFA prompts.
python3 scripts/login-acceptance.py --login --request --report /tmp/login-browser.json

# Exercise the supported headless sign-in paths separately.
python3 scripts/login-acceptance.py --login --headless --request --report /tmp/login-headless.json

# Recheck one already-authorized account without starting a new login.
python3 scripts/login-acceptance.py --agents codex --request --report /tmp/login-codex.json
```

Each report path must be new. Reports have mode 0600 and contain only revision,
platform, normalized versions, status categories, expiry buckets, and fixed check
results. Raw vendor output, account identifiers, credential contents, and private
paths are never included. Vendor login prompts are displayed directly in your
terminal and are not recorded by the runner. Do not record or upload that terminal.

`--login` explicitly authorizes re-login of the selected accounts and requires a
terminal; it invokes this checkout's `t login TOOL --force -y`, then checks status.
`--request` separately authorizes a small real model request, which may consume
paid quota. Requests run in an empty temporary working directory; Claude disables
built-in/MCP tools, Codex uses a read-only sandbox and ignores user configuration,
and Cursor uses ask mode with its sandbox enabled. No permission-bypass flags are
used. Older CLIs that reject these flags fail the request check instead of silently
weakening it. Do not use a test account configuration with unrelated hooks or MCP
integrations. The vendor may still store its own runtime state outside the temporary
working directory. Reference: [Claude flags](https://code.claude.com/docs/en/cli-reference)
and [Cursor parameters](https://cursor.com/docs/cli/reference/parameters).

Exit status is 0 when the requested automated checks pass, 1 for a failed check,
2 for missing prerequisites/invalid invocation, or 130 for interruption. A status-only
run can pass while login/request checks are explicitly skipped. **Exit 0 never means
full live acceptance:** every report keeps `acceptance_complete: false` and lists the
remaining observations. A later cached deadline after login is evidence of changed
metadata, not proof that the previous login was unusable or that refresh failed.

Normal PR CI exercises this runner with fake executables and synthetic accounts;
it does not run the opt-in commands against real accounts. Live reports must be
produced on authorized machines. No credentials belong in GitHub Actions secrets
for this PR, and a credential-bearing runner must not execute arbitrary PR code.

### Remaining live observations

The runner automates command execution, state comparisons, ignore checks, post-login
status, and optional real requests. It cannot manufacture natural expiry or approve
OAuth/MFA on your behalf. The following live checks remain unverified; automated
tests do not establish these behaviors:

1. Run on Linux and macOS, including native credential stores. Confirm installed CLI
   versions and the expected Cursor executable (`cursor-agent`).
2. Run browser and headless login/request checks after reviewing the checkout.
3. Observe naturally expiring file-backed authentication and vendor automatic refresh.
   Verify that the cached deadline corresponds to the active credential. Do not alter
   real tokens or the system clock to simulate this; synthetic boundary tests run in CI.
4. Observe vendor cancellation once; automated tests cover t's cancellation handling.
5. Review the reports' skipped/blocked checks and unsupported expiry cases. A logged-in
   status alone does not prove a usable request or successful renewal.

Only share the sanitized JSON reports, never real vendor authentication output.
