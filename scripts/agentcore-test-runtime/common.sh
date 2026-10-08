#!/usr/bin/env bash
# Sourced by the explicitly approved deployment/cleanup commands only.
set -euo pipefail
umask 077
bundle=$(cd "${1:?bundle directory required}" && pwd)
profile=${2:?existing AWS profile required}
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$bundle/manifest.json" "$1"; }
account=$(field account)
region=$(field region)
# Used by the sourcing deployment and cleanup scripts.
# shellcheck disable=SC2034
name=$(field name)
# shellcheck disable=SC2034
stack=$(field stack)
export AWS_MAX_ATTEMPTS=1 AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off
aws_cli() { aws --profile "$profile" --region "$region" --no-cli-pager --no-cli-auto-prompt "$@"; }
verify_account() {
  [[ $(aws_cli sts get-caller-identity --query Account --output text) == "$account" ]] || {
    echo 'Wrong AWS account; stopped before mutations.' >&2; exit 2;
  }
}
stack_output() {
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["Stacks"][0]; print(next(x["OutputValue"] for x in s["Outputs"] if x["OutputKey"]==sys.argv[2]))' "$bundle/stack.json" "$1"
}
