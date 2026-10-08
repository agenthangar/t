# AgentCore work submission

The supported workflow submits JSON to an existing
Amazon Bedrock AgentCore Runtime using IAM authentication and AWS CLI v2.
`t` does not deploy an agent, create AWS credentials, change IAM permissions, or
install runtime Python dependencies.

## Prerequisites and authentication

- A deployed, available AgentCore runtime and its full runtime ARN. Its agent must
  accept a JSON object; the schema is specific to your agent framework.
- AWS CLI v2 with `bedrock-agentcore invoke-agent-runtime` and
  `bedrock-agentcore stop-runtime-session` available. Older versions fail explicitly.
- Existing AWS CLI credentials with access to that runtime, including
  `bedrock-agentcore:InvokeAgentRuntime` and `bedrock-agentcore:StopRuntimeSession`.
  Use your organization's approved role/profile and resource-scoped permissions.
  Configure credentials outside `t`; for an existing SSO profile, complete your
  normal `aws sso login --profile PROFILE` flow yourself when necessary.
- IAM/SigV4 inbound authentication. OAuth-authenticated runtimes, MCP sessions,
  user impersonation headers, runtime deployment, and endpoint administration are
  outside this first workflow.

Pass `--profile` to select an existing profile, or let AWS CLI use its normal
credential chain (`AWS_PROFILE`, workload roles, etc.). Region comes from the
runtime ARN. `--runtime-arn` overrides `T_AGENTCORE_RUNTIME_ARN`. No configuration
is written. AWS CLI endpoint/configuration environment overrides are inherited;
review them before submitting work. A dry run does not authenticate or prove access.

References: AWS's [invoke command](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore/invoke-agent-runtime.html),
[stop command](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore/stop-runtime-session.html),
and [invoke API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html).

## Submit, continue, and stop

Replace the example ARN and profile with an authorized sandbox runtime. This
example assumes its agent accepts `{"prompt": "..."}`. For another schema, create
an appropriate JSON object and use `--payload request.json` instead of `--prompt`.
Agent invocations may consume paid AWS/model resources and can perform whatever
work the deployed agent is authorized to perform.

```sh
export T_AGENTCORE_RUNTIME_ARN='arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/Example-ABCDEFGHIJ'
session_id=$(python3 -c 'import uuid; print(uuid.uuid4())')

# Validates local arguments/payload, prints metadata, creates no files or requests.
t agentcore invoke --profile sandbox --session-id "$session_id" \
  --prompt 'Reply with a short greeting' --output greeting.json --dry-run

# Submit to the existing runtime. stdout is JSON metadata; response is a private file.
t agentcore invoke --profile sandbox --session-id "$session_id" \
  --prompt 'Reply with a short greeting' --output greeting.json
cat greeting.json

# Continue the same runtime session, if the deployed agent maintains session context.
t agentcore invoke --profile sandbox --session-id "$session_id" \
  --prompt 'What did I just ask?' --output continuation.json

t agentcore stop --profile sandbox --session-id "$session_id"
```

An omitted invoke `--session-id` generates a UUID. The ID is printed to stderr
before submission and included in successful JSON metadata. Save it to continue
or stop the session. `--qualifier NAME` selects a runtime endpoint; default is
`DEFAULT`. Session lifetime and conversation memory follow the deployed runtime;
reusing an ID after expiry does not promise restoration of previous state.

Responses are buffered, with a default 900-second timeout (`--timeout 1..28800`).
The raw response bytes may contain JSON, event-stream text, or application-level
errors; a successful HTTP status is not proof that the agent completed its task.
`t` requests JSON but does not invent a universal agent response schema. Inspect
the output according to your agent's contract.

Output paths must be new, including on retries; existing files and symlinks are
refused before submission. Files are created with mode 0600. An empty or partial
file can remain after a failure. Payloads are staged in a private temporary
directory and removed afterward. Raw AWS errors are not printed because they can
contain request/account details. No credential material is copied or persisted.

## Session boundaries and failures

These are remote runtime sessions, separate from numbered Git worktree/tmux
sessions. Supported operations are invoke, continue by ID, and stop. Local
`session open`, `list`, `move`, `push`, `pop`, `resume`, and desktop handoff do not
operate on AgentCore sessions. No local worktree is uploaded or synchronized.
Use your deployed agent's own remote workspace/storage integration if required.

A local per-user lock prevents simultaneous invocations of the same runtime,
endpoint, and session ID by `t` on one machine. It does not coordinate other
machines or direct AWS clients. `stop` can run while an invocation is outstanding.
It requests runtime termination; it cannot undo external side effects already made
by the agent. After a timeout or Ctrl-C, the remote agent may still be running.

AWS retry attempts are disabled for these commands to avoid silently submitting
work twice. On an ambiguous failure, inspect the runtime's state/logs before any
manual resubmission. The session ID is not an idempotency token for invocations.
Exit codes: 0 for successful transport, 2 for invalid input/missing prerequisites,
1 for request/local I/O failures, and 130 for interruption.

## Validation and remaining live checks

`tests/test_agentcore.py` exercises the actual `bin/t` entrypoint with disposable
fake AWS executables, invoke/continuation/stop, payload transport, output safety,
local locking, redacted errors, and no retry. A separate test runs the installed
AWS CLI against a loopback HTTP fixture with synthetic credentials and empty AWS
configuration. Linux CI requires that contract test. It checks request headers,
SigV4 transport, JSON payloads, session continuity, and streamed response files;
it never connects to AWS. The fixture does not emulate runtime behavior.

The integration is covered by automated tests; live AWS behavior remains unverified.
An authorized operator can validate the documented example against a deployed
sandbox runtime: verify the response and session continuity, stop it, and verify
termination in AWS. Rejected permissions and runtime errors also need live checks.
Record CLI version, platform, revision, and sanitized results without credentials
or transcripts. No live deployment, account access, or billable invocation was
performed during this implementation. Automated contract tests do not establish
that a particular runtime, role policy, or agent payload works in AWS.

A [disposable test runtime proposal](../scripts/agentcore-test-runtime/README.md)
provides a model-free target, offline packaging, guarded deployment and cleanup,
and a bounded live acceptance sequence. It requires separate AWS/IAM/cost approval.
