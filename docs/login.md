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

## Acceptance still required before merging

Automated tests use synthetic credentials and fake executables. They demonstrate
planning, subprocess orchestration, and redaction; they do not prove live renewal.
On an authorized disposable Linux account with supported vendor CLIs and test
accounts, and on macOS for native credential-store behavior:

1. Record CLI versions, confirm status/login commands (including the installed
   Cursor binary name), and compare reported state to the vendor CLI.
2. Exercise logged-out, healthy, and naturally expiring file-backed authentication.
   Verify the detected deadline corresponds to the active credential. Check
   automatic refresh behavior and whether explicit login actually renews the login.
3. Verify unknown-expiry behavior for macOS Keychain/other credential stores and
   API-key configurations, with no spurious file-derived expiry claims.
4. Run an authorized browser login and the supported headless flows; verify
   cancellation, status after success, and a real vendor request after renewal.
5. Ignore a deliberately failing installed tool and confirm other selected tools
   still work without invoking it.

Do not paste tokens, account identifiers, or real authentication output into the
PR. Record only versions, sanitized outcomes, and which cases were exercised.
