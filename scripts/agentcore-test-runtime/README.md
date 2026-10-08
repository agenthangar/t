# Disposable AgentCore acceptance runtime — deployment proposal

**Prepared, not deployed.** This directory supplies a tiny deterministic target for
PR #72 / issue #9. It is deliberately separate from the shipped `t` runtime.
`prepare.py` only writes local files. The deployment and cleanup scripts require
separate explicit approval flags; those flags are operator safeguards, not a
substitute for obtaining the user's account, IAM, deployment, and cost approval.

## Target and resources

No AWS CLI, AWS profile, account ID, or region was configured in the saved executor
when this proposal was prepared on 2026-10-08. **No target account is verified.**
The example account below is synthetic. Proposed region: **us-east-1**, subject to
approval and a read-only STS/account/region preflight on the deployment machine.
Use an existing organization-approved sandbox profile, with no new access keys.
If a permissions boundary or first-use service role is required, stop and have the
owner approve an updated template/access proposal first.

For a unique run name such as `review01`, deployment creates:

| Resource | Quantity and scope | Lifetime / cleanup |
| --- | --- | --- |
| CloudFormation stack | `t-agentcore-test-review01`, bootstrap only | Deleted last, with its bucket and role |
| S3 bucket | One generated `t-agentcore-test-review01-codebucket-*` bucket | Public access blocked, owner enforced, AES256 encryption, no versioning; object expires after one day as fallback |
| S3 object | `agent.zip`, containing only `agent.py` | Explicitly deleted before bucket deletion |
| IAM execution role + inline policy | One generated `t-agentcore-test-review01-ExecutionRole-*` role | Deleted with bootstrap stack |
| AgentCore runtime | `t_accept_review01-<generated suffix>`, Python 3.13 ZIP, HTTP, IAM authentication, platform **V1** | Idle timeout 60 seconds, maximum session lifetime 600 seconds; runtime explicitly deleted |
| Default endpoint and workload identity | Service-created resources for that runtime | Runtime deletion handles dependents; cleanup checks/deletes the recorded identity if it remains |
| CloudWatch log groups/streams | Only `/aws/bedrock-agentcore/runtimes/<recorded-runtime-id>-*` | Deleted during cleanup; set one-day retention on groups created during acceptance if keeping the test overnight |
| Test execution environments | At most three microVM lifetimes in the four-invocation sequence below | Explicit `t agentcore stop`, plus the timeout fallback |

No model invocation, ECR repository, image build, VPC/NAT, managed instance,
capacity provider, AgentCore Memory, gateway, OAuth provider, secrets, API keys,
tracing setup, or persistent filesystem is part of this proposal. PUBLIC networking
is the AWS runtime networking mode; invocation still requires IAM authentication.
The agent has no code that makes outbound network calls or reads credentials.

AWS references: [direct ZIP deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html),
[HTTP contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html),
[session/platform behavior](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html).
V1 is explicit to keep the proposed billing and session behavior stable. The agent
initializes its process marker lazily on the first invocation, avoiding a shared
marker if a future proposal uses snapshots.

## Exact execution-role permissions

The generated `bootstrap.json` is the reviewable policy source. Its trust policy
allows `bedrock-agentcore.amazonaws.com` to assume the role only from the chosen
account and `arn:aws:bedrock-agentcore:REGION:ACCOUNT:runtime/t_accept_NAME-*`.
Its inline permissions are:

- `s3:GetObject` on that generated bucket's **single `agent.zip` object**.
- `logs:CreateLogGroup`, `logs:DescribeLogStreams` on the test runtime's log-group prefix.
- `logs:CreateLogStream`, `logs:PutLogEvents` on streams under that prefix.
- `logs:DescribeLogGroups` on log groups in the selected account/region (read-only discovery).

There are no model, IAM, STS credential-issuance, X-Ray, metric-write, workload-token,
or credential-provider permissions in the execution policy. The plain Python
server does not use the AgentCore SDK or OpenTelemetry. AWS's general-purpose
[execution-role examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
include broader optional capabilities. This narrower policy still needs a live
creation/invocation test; if AWS requires another action, stop and review it rather
than automatically attaching FullAccess or broadening the role.

## Temporary deployment-operator permissions to approve

These are permissions for an **existing deployment principal**, separate from the
new execution role. Prefer a time-bounded approved role/session; do not attach a
permanent administrator policy or create credentials for these scripts.

| Service | Actions used or needed by resource lifecycle | Scope |
| --- | --- | --- |
| STS | `GetCallerIdentity` | Identity preflight only |
| CloudFormation | `CreateStack`, `DescribeStacks`, `DeleteStack`; `DescribeStackEvents` for diagnostics | The named bootstrap stack |
| IAM | `CreateRole`, `GetRole`, `TagRole`, `UntagRole`, `PutRolePolicy`, `GetRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `DeleteRolePolicy`, `DeleteRole` | Generated execution-role prefix for this run |
| IAM | `PassRole` | Only that execution role, with `iam:PassedToService=bedrock-agentcore.amazonaws.com` |
| S3 bucket | `CreateBucket`, `DeleteBucket`, `ListBucket`, `GetBucketLocation`, `GetBucketTagging`, `PutBucketTagging`, `GetBucketPublicAccessBlock`, `PutBucketPublicAccessBlock`, `GetBucketOwnershipControls`, `PutBucketOwnershipControls`, `DeleteBucketOwnershipControls`, `GetEncryptionConfiguration`, `PutEncryptionConfiguration`, `GetLifecycleConfiguration`, `PutLifecycleConfiguration` | Generated bucket prefix for this bootstrap stack |
| S3 object | `PutObject`, `GetObject`, `DeleteObject` | Only that bucket's `agent.zip` |
| AgentCore lifecycle | `CreateAgentRuntime`, `GetAgentRuntime`, `DeleteAgentRuntime`, `TagResource`, `ListTagsForResource`; dependent `CreateAgentRuntimeEndpoint`, `DeleteAgentRuntimeEndpoint`, `CreateWorkloadIdentity`, `DeleteWorkloadIdentity` | This runtime name/prefix, its DEFAULT endpoint, and its service-created workload identity; restrict region/account and supported request tags |
| AgentCore acceptance | `InvokeAgentRuntime`, `StopRuntimeSession` | Exact runtime ARN from the creation receipt |
| CloudWatch Logs | `DescribeLogGroups`, `DescribeLogStreams`, `GetLogEvents` for diagnosis; `PutRetentionPolicy`, `DeleteLogGroup` for retention/cleanup | Read discovery as required by AWS; mutations only on this runtime's exact log-group prefix |

Consult the current [service authorization mapping](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html)
when materializing the caller policy: some create/discovery actions do not support
an exact resource ARN before creation. Scope those with the available region,
account, and request-tag conditions rather than assuming all actions accept the
same ARN. No caller policy is installed by these artifacts. First-use
`iam:CreateServiceLinkedRole`, permission-boundary changes, resource-policy changes,
and any action beyond this inventory are **not preapproved**. They require a new
concrete permission review if the target account needs them.

## Build and inspect locally — no AWS access

```sh
# Replace the synthetic account after the owner chooses the real target.
python3 scripts/agentcore-test-runtime/prepare.py \
  --account 111122223333 --region us-east-1 --name review01 \
  --output /tmp/t-agentcore-review01

python3 -m pytest -q tests/test_agentcore_fixture.py
shellcheck -x scripts/agentcore-test-runtime/*.sh
```

The output directory must be new. Review `bootstrap.json`, `runtime-template.json`,
`manifest.json`, and the ZIP's SHA-256 before approving. `runtime-template.json`
contains two deliberate placeholders; `render.py` replaces them from the verified
bootstrap stack outputs. The ZIP contains only the standard-library agent, with
fixed archive metadata and mode 0644. No pip dependencies or architecture-specific
binaries are needed. Account-bearing bundles and deployment receipts stay outside
the repository; never commit real account configuration or authentication output.

## Deploy only after approval

Prerequisite: AWS CLI v2 supporting direct code configuration and `platformVersion`,
an already authorized profile, and approval for the resources/permissions/cost
above. The executor does not currently have AWS CLI; a prior official download
attempt returned HTTP 403. Use an approved machine/tool installation. Do not create
credentials or work around access restrictions to run this proposal.

```sh
# Only after the owner approves this exact bundle, target, permissions, and cost:
T_AGENTCORE_APPROVE_DEPLOY=review01 \
  bash scripts/agentcore-test-runtime/deploy.sh /tmp/t-agentcore-review01 sandbox
```

The script verifies the caller account before mutations, creates a new bootstrap
stack with `CAPABILITY_IAM`, uploads the ZIP with an expected-bucket-owner check,
creates the runtime, and waits for READY. It records receipts with private local
permissions. It performs no model requests or test invocations. It refuses
accidental reruns, sets `AWS_MAX_ATTEMPTS=1`, and preserves receipts on failure.
Inspect `runtime-status.json` for the expected artifact, role, V1 platform, and
lifecycle values before acceptance. Review any AWS endpoint overrides in the
selected profile/environment as part of the action-time approval.

## Bounded live acceptance after deployment approval

The payload contract is `{"prompt":"TEXT"}`, with at most 256 characters. The
response contains a fixed fixture marker, `reply: "ack:TEXT"`, a per-process
`boot_id`, and an invocation `count`. The agent serves `/ping` and `/invocations`,
logs no requests, and stores only a counter and process marker in memory.

Use **four invocations**, two session IDs, and at most three microVM lifetimes:

1. Fresh A: `hello` → count 1, boot marker A.
2. Continue A immediately: `again` → count 2, same boot marker.
3. Stop A using `t agentcore stop`, then invoke A once: `after-stop` → count 1,
   a different boot marker. Complete steps 1–3 within 60 seconds and record timing;
   otherwise an idle/lifetime expiry could explain the reset and stop evidence is
   inconclusive. Do not automatically retry a failed invocation.
4. Fresh B: `isolated` → count 1, a different boot marker from both A processes.
5. Stop the renewed A and B explicitly, including on failures. Record exit codes.

Use this checkout's `python3 bin/t agentcore ...`, the exact runtime ARN in
`runtime-create.json`, the approved `--profile`, and **new** `--output` files for
each invocation. UUIDs from `python3 -c 'import uuid; print(uuid.uuid4())'` are
suitable session IDs. Keep the IDs and sanitized result/timing evidence locally so
that sessions can be stopped even after interruption.

The optional error-case payload is `{"prompt":"fixture-error"}` and deliberately returns HTTP
400. An error-case request is an **additional invocation**, outside the four-call
cost scenario below, and must fit the approved allowance. Test IAM denial only
with an existing approved principal known to lack access; do not create users,
credentials, roles, or policies solely to manufacture a denied request without
separate approval. No live acceptance results exist yet.

## Cleanup, including partial deployment

Stop recorded sessions first. After reviewing the receipts and cleanup scope:

```sh
T_AGENTCORE_APPROVE_CLEANUP=review01 \
  bash scripts/agentcore-test-runtime/cleanup.sh /tmp/t-agentcore-review01 sandbox
```

Cleanup rechecks account identity; deletes only the recorded runtime; waits until
AWS reports it absent; removes its recorded workload identity if still present;
deletes matching runtime log groups; removes only `agent.zip`; then deletes and
waits for the bootstrap stack, which removes the execution role and bucket.
`cleanup.complete` is written only after those steps succeed. No recursive S3
removal, general log-group purge, role enumeration, or shared identity-directory
deletion is used. A full bucket causes stack deletion to fail instead of deleting
unknown objects. Preserve the private local receipts until verification completes.

If bootstrap fails before uploading, inspect its events and delete that exact
stack after approval. If runtime creation is ambiguous or its receipt is missing,
cleanup deliberately stops: reconcile the runtime by exact run name using an
approved read-only AWS inventory, recover its creation receipt/ARN, and review its
ownership before proceeding. Do not assume a CLI timeout means nothing was
created, and do not blindly rerun deployment. A workload identity with an
unexpected name also requires operator review before deletion.

The 60-second idle and 600-second maximum session lifetime bound individual
sessions; they do not delete the runtime, bootstrap resources, or allow-list
further invocations. S3 lifecycle expiration is a storage fallback, not complete
cleanup. Verify the stack/runtime are absent, no recorded identity/log groups
remain, and remove any temporary caller grants after the run. No cleanup has been
performed against AWS because nothing has been deployed.

## Cost proposal, checked 2026-10-08

For proposed US East (N. Virginia), use on-demand **V1**, with no model calls and no
free-tier assumptions. AWS lists V1 CPU at **$0.0895/vCPU-hour** and memory at
**$0.00945/GB-hour**. CPU reflects active use; memory remains billable while a
session exists. Using the published per-session ceiling of 2 vCPU/8 GB and allowing
all three microVM lifetimes the full ten minutes gives the conservative runtime
scenario:

`3 × (10/60) × (2 × $0.0895 + 8 × $0.00945) = $0.1273`.

Actual usage of this tiny server should be lower, but it has not been measured in
AWS. This is a scenario estimate, **not a hard account-level spending cap**. Extra
invocations, delays beyond assumptions, retained resources, and region changes
need a revised estimate. Sources: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/),
[session resource quota](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html).

The ZIP is only a few KB. S3 adds storage and a handful of PUT/GET requests; allow
**$0.01** for that small test workload rather than claiming it is free. At the US
East CloudWatch example rate of **$0.50/GB ingested**, even 1 MB of logs is about
**$0.0005**, plus negligible short-lived storage. Budget another **$0.01** for logs
and small transfer overhead, and verify region-specific rates before deployment.
Sources: [S3 pricing](https://aws.amazon.com/s3/pricing/),
[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).

Expected bounded-test envelope: **under $0.15** for the stated workload, excluding
tax and any unrelated account usage. Request approval for a **$1 test allowance**
with immediate cleanup; that allowance is an operator limit, not an AWS-enforced
budget. No budget/alert resources, subscriptions, or commitments are created.

## Current evidence and remaining choices

Local tests use real loopback HTTP and a fake AWS executable: health, deterministic
responses, continuity, reset/isolation, malformed input, reproducible ZIPs, scoped
policy structure, approval guards, wrong-account refusal, deployment/cleanup
receipts, and ambiguous-create refusal. They prove local behavior, not that AWS
will accept the templates or the role policy. CloudFormation service validation,
actual AWS CLI schema validation, and real deployment remain unperformed.

Before cloud actions, the operator must obtain:

- The target sandbox account, approved region (proposed `us-east-1`), existing
  profile/role and execution machine; any mandatory permissions boundary.
- Approval to create the listed temporary resources and scoped IAM role/policy,
  grant the operator's listed actions temporarily, run the bounded acceptance
  sequence, and delete those same resources afterward.
- Approval for the $1 test allowance and the person responsible for cleanup if the
  executor disconnects. The saved executor resumed successfully for preparation.

Merging this fixture does not authorize deployment. It creates no credentials,
grants no permissions, and makes no AWS API calls during local preparation or
tests. The live workflow remains unverified until an authorized operator runs it.
