# Disposable t playground

The playground starts an Ubuntu 24.04 container with a clean, unprivileged user
account. It creates a local Git repository at `~/code/playground` and a bare
`origin/main` at `~/remotes/playground.git`, so the normal `t session open` worktree flow
can run without a GitHub account. The account, repos, t installation, and tmux
sessions disappear when the container exits. Docker keeps the built image; each
run creates a new account and repos.

Install and start Docker Desktop on macOS, or Docker Engine on Linux. Then run
these commands from this checkout:

```sh
python3 scripts/playground.py             # fresh shell; t is uninstalled
python3 scripts/playground.py installed   # install current source before opening a shell
python3 scripts/playground.py smoke       # automated install and CLI flow
python3 scripts/playground.py build       # build the image without starting a container
```

The launcher builds an image from the tracked runtime files in your current
working tree. Edits to tracked runtime files are included; a new runtime file
must be added with `git add` first. The build context contains the release
manifest's runtime files, the live-checkout Git hook, and playground helpers. It does not include the host
repository's Git history, untracked files, personal configuration, or
credentials. No host directory, SSH agent, tmux socket, or environment variable
is forwarded to the container. The container's `~/code/t` is an independent
snapshot with its own disposable Git history and a local `~/remotes/t.git`
origin. Its `t system update` exercises the Git update path against that local snapshot;
use the release install below to test downloads from GitHub.

In the fresh shell, exercise installation and setup:

```sh
playground-install
exec zsh -l
t --version
t config setup ~/code/playground --no-hosts --no-instructions
```

In the setup picker, press **Space** to select `playground`, **Enter** to review,
and **y** to confirm. The `installed` mode starts after `playground-install` has
run; continue at `t config setup`. To test the latest official release instead of this
working tree, use `playground-install --release` in a fresh shell. That step
needs network access.

For a terminal session without an agent account, install the included echo
stub. It acts as a `claude` executable only inside the container:

```sh
playground-demo-agent
t session open playground
```

Type a line to see it echoed. Detach with **Ctrl-b d**, then inspect and clean up
from the container shell:

```sh
t session list
t session read playground 1 --dump
t session open playground 1       # detach again with Ctrl-b d before the next command
t session close playground 1
```

If you want to test a real agent, remove the stub with `rm ~/bin/claude` and run
`t agent install` in the container. The agent's own installer and sign-in flow may need
network access. The Linux playground covers installation, shell integration,
repository setup, worktrees, and tmux. macOS desktop handoff requires macOS or a
macOS VM.
