#!/usr/bin/env bash
# Run one leg of the same design matrix used before design PRs and releases.
set -euo pipefail

cd "$(dirname "$0")/.."
case "${1:-}" in
    0.44.1|0.74.5) version=$1 ;;
    *) echo 'Usage: scripts/check_design.sh 0.44.1|0.74.5 (put that fzf on PATH)' >&2; exit 2 ;;
esac
read -r actual _ < <(fzf --version)
if [[ $actual != "$version" ]]; then
    echo "Expected fzf $version on PATH, found $actual" >&2
    exit 1
fi
python3 scripts/design_check.py run

if [[ $version == 0.74.5 ]]; then
    scratch=$(mktemp -d)
    trap 'rm -rf "$scratch"' EXIT
    npm install --prefix "$scratch" --no-package-lock playwright@1.64.0
    "$scratch/node_modules/.bin/playwright" install --only-shell chromium
    python3 scripts/preview_picker.py --output "$scratch/picker.html"
    NODE_PATH="$scratch/node_modules" node tests/picker_preview.cjs "$scratch/picker.html"
fi
