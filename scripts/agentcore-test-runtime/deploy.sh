#!/usr/bin/env bash
# DO NOT RUN before resource, IAM, account, and cost approval.
set -euo pipefail
script_dir=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/agentcore-test-runtime/common.sh
source "$script_dir/common.sh" "$@"
[[ ${T_AGENTCORE_APPROVE_DEPLOY:-} == "$name" ]] || {
  echo 'Requires explicit deployment/IAM/cost approval; set T_AGENTCORE_APPROVE_DEPLOY to the reviewed run name afterward.' >&2; exit 2;
}
python3 "$script_dir/render.py" verify "$bundle"
verify_account
# Fail closed on reruns. Partial failures require receipt inspection and cleanup.
(set -o noclobber; : > "$bundle/deploy.started")
aws_cli cloudformation create-stack --stack-name "$stack" \
  --template-body "file://$bundle/bootstrap.json" --capabilities CAPABILITY_IAM \
  --tags Key=Purpose,Value=t-agentcore-acceptance > "$bundle/stack-create.json"
aws_cli cloudformation wait stack-create-complete --stack-name "$stack"
aws_cli cloudformation describe-stacks --stack-name "$stack" > "$bundle/stack.json"
python3 "$script_dir/render.py" runtime "$bundle"
bucket=$(stack_output Bucket)
aws_cli s3api put-object --bucket "$bucket" --key agent.zip --body "$bundle/agent.zip" \
  --expected-bucket-owner "$account" > "$bundle/upload.json"
: > "$bundle/runtime-create.started"
aws_cli bedrock-agentcore-control create-agent-runtime \
  --cli-input-json "file://$bundle/runtime-request.json" > "$bundle/runtime-create.json"
runtime_id=$(python3 "$script_dir/render.py" runtime-id "$bundle")
for ((attempt=0; attempt<60; attempt++)); do
  aws_cli bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$runtime_id" > "$bundle/runtime-status.json"
  status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$bundle/runtime-status.json")
  if [[ $status == READY ]]; then
    echo 'Runtime READY. Review runtime-status.json and follow README acceptance steps; no invocations were made.'
    exit 0
  fi
  [[ $status != *FAILED ]] || { echo 'Runtime creation failed. Inspect receipts and run approved cleanup.' >&2; exit 1; }
  sleep 5
done
echo 'Runtime creation timed out; inspect receipts and clean up. No retry was made.' >&2
exit 1
