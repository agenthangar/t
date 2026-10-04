#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/tester
export ZDOTDIR="$HOME"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_STATE_HOME="$HOME/.local/state"
export TMUX_TMPDIR="$XDG_CACHE_HOME/tmux"
export PATH="$HOME/bin:$PATH"

mkdir -p "$HOME/bin" "$HOME/code" "$HOME/remotes" \
    "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" \
    "$XDG_STATE_HOME" "$TMUX_TMPDIR"
chmod 700 "$TMUX_TMPDIR"

cat > "$HOME/.zshrc" <<'ZSHRC'
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
PROMPT='playground%# '
ZSHRC

# The copied source is a snapshot of the caller's working tree. Its Git history
# starts here so install.sh sees a canonical checkout owned by this container.
cp -a /opt/t-source "$HOME/code/t"
if [[ -e "$HOME/code/t/.git" ]]; then
    echo 'playground: source snapshot must not include .git' >&2
    exit 1
fi
git -C "$HOME/code/t" init -q -b main
git -C "$HOME/code/t" config user.name 'Playground Tester'
git -C "$HOME/code/t" config user.email 'tester@playground.invalid'
git -C "$HOME/code/t" add -A
git -C "$HOME/code/t" commit -qm 'Playground t snapshot'
git init -q --bare -b main "$HOME/remotes/t.git"
git -C "$HOME/code/t" remote add origin "$HOME/remotes/t.git"
git -C "$HOME/code/t" push -q -u origin main

# A real local origin/main makes worktree creation and fetch work offline.
git init -q --bare -b main "$HOME/remotes/playground.git"
git init -q -b main "$HOME/code/playground"
git -C "$HOME/code/playground" config user.name 'Playground Tester'
git -C "$HOME/code/playground" config user.email 'tester@playground.invalid'
printf 'Disposable repository for testing t.\n' > "$HOME/code/playground/README.md"
git -C "$HOME/code/playground" add README.md
git -C "$HOME/code/playground" commit -qm 'Initial commit'
git -C "$HOME/code/playground" remote add origin "$HOME/remotes/playground.git"
git -C "$HOME/code/playground" push -q -u origin main

case "${1:-fresh}" in
    fresh)
        shift || true
        echo 'Playground ready. t is uninstalled.'
        echo 'Run playground-install; exec zsh -l; then t config setup and t session open playground.'
        exec zsh -l "$@"
        ;;
    installed)
        shift
        playground-install
        echo 'Playground ready. t is installed; run t config setup to register repositories.'
        exec zsh -l "$@"
        ;;
    smoke)
        shift
        exec python3 /opt/playground/smoke.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
