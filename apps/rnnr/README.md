# rnnr for macOS

rnnr shows your coding sessions in the menu bar. Open the app to check the
bundled runtime, enable Terminal actions, and optionally connect Codex or
Claude Code. Right-click the fox and choose **Set Up rnnr…** to run setup again.
Existing user configuration and session history carry over.

## Contributor build

On macOS, use Python 3.12 or later and the Xcode command-line tools. Build from
a tagged Git repository with network access for the pinned runtime dependencies.

```sh
python3 apps/rnnr/build.py
swift run --package-path apps/rnnr RnnrCoreChecks
```

The locally signed universal bundle is in `apps/rnnr/.build/rnnr.app`. It
includes the committed CLI runtime, Python, tmux and fzf for Intel and Apple
Silicon. Click a session to attach in Terminal. **New**, **All**, **Open**,
**Resume**, and **Move** use the existing session flows and ownership checks.

## Release

Set `RNNR_VERSION`, `RNNR_SIGN_IDENTITY` (Developer ID Application) and
`RNNR_NOTARY_PROFILE` (a configured notarytool keychain profile). Run
`python3 apps/rnnr/release.py`. The release directory contains the stapled DMG
and its checksum-pinned cask. Verify on a clean Mac before publishing the DMG
under the matching `rnnr-vX.Y.Z` tag and adding the cask to the AgentHangar tap.
The guided installer and `t install rnnr` use that same cask.
