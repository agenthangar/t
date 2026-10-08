#!/usr/bin/env bash
# Removes only this reviewed deployment; requires separate cleanup approval.
set -euo pipefail
script_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/agentcore-test-runtime/common.sh
source "$script_dir/common.sh" "$@"
[[ ${T_AGENTCORE_APPROVE_CLEANUP:-} == "$name" ]] || {
  echo 'Requires cleanup approval; set T_AGENTCORE_APPROVE_CLEANUP to the reviewed run name afterward.' >&2; exit 2;
}
python3 "$script_dir/render.py" verify-manifest "$bundle"
verify_account
if [[ -e $bundle/runtime-create.started ]]; then
  # A failed/ambiguous create with no valid receipt must be reconciled, not ignored.
  runtime_id=$(python3 "$script_dir/render.py" runtime-id "$bundle")
  identity=$(python3 "$script_dir/render.py" identity-name "$bundle")
  if ! aws_cli bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$runtime_id" \
      > "$bundle/runtime-delete.json" 2> "$bundle/delete-error.txt"; then
    grep -q 'ResourceNotFoundException' "$bundle/delete-error.txt" || {
      echo 'Runtime deletion failed; inspect delete-error.txt. Other resources retained.' >&2; exit 1;
    }
  fi
  gone=false
  for ((attempt=0; attempt<60; attempt++)); do
    if ! aws_cli bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$runtime_id" \
        > "$bundle/runtime-status.json" 2> "$bundle/delete-error.txt"; then
      if grep -q 'ResourceNotFoundException' "$bundle/delete-error.txt"; then gone=true; break; fi
      echo 'Cannot verify runtime deletion. Other resources retained.' >&2; exit 1
    fi
    sleep 5
  done
  [[ $gone == true ]] || { echo 'Runtime deletion still pending. Other resources retained.' >&2; exit 1; }
  # Service-managed identity cleanup is explicit, confined to the creation receipt.
  if [[ -n $identity ]]; then
    if ! aws_cli bedrock-agentcore-control delete-workload-identity --name "$identity" \
        > "$bundle/identity-delete.json" 2> "$bundle/delete-error.txt"; then
      grep -q 'ResourceNotFoundException' "$bundle/delete-error.txt" || {
        echo 'Workload identity cleanup failed; inspect receipts.' >&2; exit 1;
      }
    fi
  fi
  prefix="/aws/bedrock-agentcore/runtimes/$runtime_id"
  aws_cli logs describe-log-groups --log-group-name-prefix "$prefix" > "$bundle/log-groups.json"
  python3 -c 'import json,sys; names=[x["logGroupName"] for x in json.load(open(sys.argv[1]))["logGroups"]]; assert all(n.startswith(sys.argv[2]+"-") for n in names); print("\n".join(names))' \
    "$bundle/log-groups.json" "$prefix" > "$bundle/log-groups.txt"
  while IFS= read -r group; do
    [[ -z $group ]] || aws_cli logs delete-log-group --log-group-name "$group"
  done < "$bundle/log-groups.txt"
fi
if [[ -e $bundle/stack.json ]]; then
  bucket=$(python3 "$script_dir/render.py" stack "$bundle")
  aws_cli s3api delete-object --bucket "$bucket" --key agent.zip --expected-bucket-owner "$account" > "$bundle/object-delete.json"
fi
aws_cli cloudformation delete-stack --stack-name "$stack"
aws_cli cloudformation wait stack-delete-complete --stack-name "$stack"
: > "$bundle/cleanup.complete"
echo 'Recorded runtime, identity, logs, artifact, and bootstrap stack cleanup completed. Keep local receipts.'
