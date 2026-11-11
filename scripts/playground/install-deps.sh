#!/usr/bin/env bash
# Disposable Ubuntu test hosts only. Keep archive signature verification enabled.
set -euo pipefail

options=(-o Acquire::Retries=1 -o Acquire::http::Timeout=15
         -o Acquire::https::Timeout=15 -o APT::Update::Error-Mode=any)
install_packages() {
  timeout 60 apt-get "${options[@]}" update &&
    timeout 180 apt-get "${options[@]}" install -y --no-install-recommends "$@"
}

if install_packages "$@"; then
  exit 0
fi

echo 'Default Ubuntu archives unavailable; retrying the Kernel.org Ubuntu mirror.'
# Use a temporary source list; do not alter the host's repository configuration.
# shellcheck disable=SC1091
source /etc/os-release
test "$ID" = ubuntu
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
chmod 755 "$temporary"
for suite in "$VERSION_CODENAME" "$VERSION_CODENAME-updates" "$VERSION_CODENAME-security"; do
  printf 'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://mirrors.kernel.org/ubuntu %s main universe restricted multiverse\n' "$suite"
done > "$temporary/sources.list"
options+=(-o "Dir::Etc::sourcelist=$temporary/sources.list"
          -o 'Dir::Etc::sourceparts=-' -o APT::Get::List-Cleanup=0)
install_packages "$@"
