# rnnr for macOS

rnnr is a small menu-bar companion for `t`. The pane shows detached sessions
from local and registered remote hosts. It follows the Mac’s Light or Dark
appearance and refreshes while the pane is open.

## Build

Use macOS 13 or later, Python 3, and the Xcode Command Line Tools. From the
repository root, build and open the app.

```sh
python3 apps/rnnr/build.py
open apps/rnnr/.build/rnnr.app
```

The locally built app uses this checkout’s `t` backend and the existing user
configuration. Build again after moving the checkout. Click the fox in the menu
bar to open the pane. macOS may ask for permission to control Terminal the first
time you use a session action.

Click a session to attach in a new Terminal window. **New**, **All sessions**,
**Open**, **Resume**, and **Move** open the corresponding `t` flows in Terminal.
**×** confirms the session and opens the existing close flow in Terminal. Closing a session preserves its history and worktree.

The bundle is built for the current Mac and signed for local use. The normal
`t` installer is independent of this companion.

Run the Foundation-only checks without opening windows.

```sh
swift run --package-path apps/rnnr RnnrCoreChecks
```
