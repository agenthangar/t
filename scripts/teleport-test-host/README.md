# Staged AWS acceptance: AgentCore #9, then Teleport #10

Prepared 2026-10-10. **Nothing provisioned; no cloud acceptance passed.**
This proposal and `host.json` are review artifacts, not deployment authorization.
The template creates a base Linux host only; Teleport installation/authentication
remain gated on the software and security decisions below.

## Verified starting point

- Clean saved checkout `0db915ccf869509ffa8765d88ccae9cbfbe502ee`, matching remote main.
  PR #72 supplies AgentCore support; PR #73 supplies the disposable fixture.
- Reuse [the AgentCore fixture](../agentcore-test-runtime/README.md) unchanged.
  Its prepare/deploy/render/cleanup scripts already separate local preparation,
  explicit deployment approval, receipts, identity checks, and scoped cleanup.
- Remote `dev/t-14` is `2ab54f5d4494ed68c61d0b78046a07ff06ddd33d`.
  Its two unmerged commits concern Codex rollout integrity/schema compatibility;
  changes touch `zsh/remote.zsh`, `zsh/resume.zsh`, `tests/test_agent_seam.py`.
  It overlaps beam paths, but is not a Teleport implementation. Do not merge its
  old tree or duplicate its changes. Coordinate before changing those paths.
- Host registry maps aliases to OpenSSH targets (`bin/t`); execution preserves
  remote `zsh -lic`. Session transfers use rsync over OpenSSH, including BatchMode
  in background sync (`zsh/remote.zsh`, `zsh/resume.zsh`). First test Teleport's
  generated OpenSSH configuration; replacing every SSH invocation with `tsh ssh`
  would miss rsync and host-discovery behavior.
- No repository `.agents/skills`, or SKILL.md under `/workspace/.agents` or
  `/workspace/.codex`, was found. AGENTS.md and CONTRIBUTING.md were read.
- AWS CLI is absent, `/workspace/.aws` is empty, and no AWS environment variables
  are set. No account, profile, role, region, key pair, AMI or client IP is verified.
  Do not infer an account from GitHub ownership. No secret files were printed.

## Stage A: existing AgentCore acceptance

Materialize the existing offline bundle with a synthetic account for review.
After approval, regenerate for the verified sandbox account and us-east-1 (only
proposed), inspect its hash and policies, then use the existing deployment scripts.
Use precisely four real invocations, two session IDs and at most three microVM
lifetimes. Prove first invocation, same-session counter continuity, explicit stop
and same-ID reset within the 60-second idle window, and independent-session
isolation. Stop both remaining sessions. Record actual payloads, timing, exit
codes, response boot IDs and counters in private receipts. Do not label fake AWS
CLI tests or local HTTP tests as cloud acceptance.

Cleanup immediately using the receipt-validated existing cleanup script. Confirm
runtime/identity/log groups and stack are absent before Stage B. This also keeps
the approval and failure diagnosis independent. AgentCore is not a host for the
Teleport control plane or an interactive SSH server.

## Stage B: separate minimal Teleport lab

`host.json` proposes one disposable Ubuntu 22.04 x86_64 `t3.small` EC2 instance
(2 vCPU, 2 GiB), standard CPU credits, encrypted 8 GiB gp3 root, IMDSv2 required,
and no instance profile. The reviewed AMI must have `/dev/sda1` root <=8 GiB and no
extra disks or product codes; resolve owner/image ID from AWS before approval.

A new isolated /24 VPC, /28 subnet, internet gateway, route table/default route,
association and security group have no connection to production. One ephemeral
public IPv4 permits outbound installation without a NAT gateway. Only TCP 22
(bootstrap with an EXISTING approved key) and TCP 443 (Teleport TLS multiplexing)
are reachable from one explicitly approved operator/test-client IPv4 /32. No
0.0.0.0/0 ingress, IPv6, peering, load balancer, public DNS changes, EIP or NAT.
Outbound TCP 80/443 to the Internet allows OS/package downloads; VPC resolver DNS
is available. This is an explicit egress proposal, not destination allowlisting.
If the execution environment cannot reach SSH/443 through its allowed network,
use an approved execution machine; do not open wider ingress or change allowlists.

Run Auth, Proxy and SSH services together on the host for this single-node demo.
Bind Auth privately/loopback; expose only the multiplexed Proxy 443 via the SG.
Local backend, session recordings and audit logs remain on the disposable disk.
This proves a single Teleport-managed node, not HA, cross-node routing or production
scale. A second node requires a new resource/cost approval.

User data ONLY installs a systemd four-hour shutdown timer. EC2 is configured to
terminate on OS shutdown and delete the root volume. Verify timer and shutdown
behavior before installing software. Reboots reset OnBootSec; no reboot is allowed
without a revised deadline. The timer is a fallback, not guaranteed cleanup or a
hard spending cap. No reboot, extra disk, snapshot, AMI creation or backup planned.

### Software, TLS and authentication choices still required

Preferred candidate: official Teleport Community Edition binaries, after the owner
confirms eligibility under its current license and expressly authorizes their use.
The official licensing notice restricts company use by size/revenue and prohibits
resale/embedding; being a public repository does not establish eligibility.
Enterprise self-hosted requires a valid existing license or separately approved
evaluation. Teleport Cloud/trial requires separate account/terms approval and is
not this AWS-hosted proposal. Do not buy a license, start a trial, subscribe in
Marketplace or accept new agreements automatically.

Before provisioning, select and record an exact supported server/tsh/tctl version,
artifact URLs and verified digest/signature; no curl-pipe-shell or floating latest.
The official demo currently names 18.11.3 as a candidate; verify its support and
security status and obtain the matching artifact digest before freezing the bundle.
Prepare the concrete installer/configuration for review once edition is chosen.

Proposed test authentication: one new disposable local Teleport user with MFA and
a narrowly scoped role permitting only Linux login `taccept` on lab-labelled nodes;
one local `taccept` Linux user (zsh shell, no sudo), one-hour or shorter cert TTL,
no SSH agent forwarding, no production SSO or trusted clusters. Auth creates a
cluster CA and issues credentials: **this requires explicit approval**. Existing
bootstrap SSH key remains unchanged. Never put tokens, passwords, private keys,
license material or MFA setup in CloudFormation parameters/user data/outputs.

Proposed TLS: disposable lab-only CA/certificate trusted only by an isolated test
HOME/client configuration, never installed in the workstation/system trust store.
Use a chosen test name and temporary isolated hosts mapping to the instance IP,
or a reviewed IP-SAN certificate/client configuration. Validate hostname matching
and OpenSSH host CA pins. No `--insecure`, StrictHostKeyChecking=no, public domain
purchase, ACME enrollment or production DNS modification. Exact name/client trust
mechanism must be resolved and approved before deployment. If the client cannot
support scoped trust, stop and propose existing approved DNS/TLS instead.

## Required operator actions and persistent effects

Use an already authorized sandbox principal; no keys, users or permanent policies
are created for AWS access. Stage A's exact operator/execution-role inventory and
trust conditions are in the existing AgentCore README/generated bootstrap JSON.
It includes scoped iam:PassRole; first-use service roles/boundary changes need a
new review. Do not attach AdministratorAccess.

Stage B proposes no IAM role/policy or PassRole. Its operator action inventory is:

- sts:GetCallerIdentity; CloudFormation CreateStack, DescribeStacks,
  DescribeStackEvents, ListStackResources, DeleteStack for the exact lab stack;
  ValidateTemplate for pre-deployment validation (does not provision resources).
- EC2 DescribeImages, DescribeKeyPairs, DescribeInstanceTypes, DescribeInstances,
  DescribeInstanceStatus, DescribeVolumes, DescribeVpcs, DescribeSubnets,
  DescribeInternetGateways, DescribeRouteTables, DescribeSecurityGroups,
  DescribeNetworkInterfaces, DescribeTags for preflight/verification.
- EC2 CreateVpc/DeleteVpc/ModifyVpcAttribute; CreateSubnet/DeleteSubnet;
  CreateInternetGateway/DeleteInternetGateway/AttachInternetGateway/
  DetachInternetGateway; CreateRouteTable/DeleteRouteTable/CreateRoute/DeleteRoute;
  AssociateRouteTable/DisassociateRouteTable; CreateSecurityGroup/
  DeleteSecurityGroup/AuthorizeSecurityGroupIngress/RevokeSecurityGroupIngress/
  AuthorizeSecurityGroupEgress/RevokeSecurityGroupEgress; RunInstances/
  TerminateInstances; CreateTags/DeleteTags.

Scope mutations to the sandbox region, run stack and owned resource IDs, with
supported request/resource tags; RunInstances also needs approved AMI, subnet,
security group, key pair, instance, volume and network-interface resources.
Describe actions often need `*`. Materialize and review an exact IAM policy using
AWS's service authorization mapping once account/AMI/key are known; this inventory
is not a claim that a wildcard policy is approved or that AWS has accepted it.
Encrypted root uses existing AWS-managed EBS encryption; if the account forces a
customer KMS key, stop for a concrete KMS/grant review rather than changing defaults.
No CreateKeyPair, ImportKeyPair, SSM, Route53, Secrets Manager or IAM writes in B.

Temporary cloud resources, Linux/Teleport users, cluster CA, MFA credential and
client trust files persist until cleanup. Explicitly approve these effects even
though they are disposable. Neither stage changes production authentication.

## Bounded cost proposal (USD, before tax; no free-tier assumptions)

Proposed region us-east-1; recheck live pricing before approval/provisioning.

| Item | Planned bound | Estimated amount |
| --- | --- | ---: |
| AgentCore V1 | 3 lifetimes x 10 min x (2 vCPU x .0895/h + 8 GiB x .00945/h) | 0.1273 |
| A S3 and CloudWatch/transfer contingency | Tiny ZIP, four requests, short logs | 0.02 |
| B t3.small Linux standard | 4 h x .0208 | 0.0832 |
| B ephemeral public IPv4 | 4 h x .005 | 0.0200 |
| B gp3 root | 8 GiB x .08/month x 4/720 | 0.0036 |
| B transfer/package/test overhead allowance | <=100 MiB outbound test data | 0.05 |
| Teleport software | 0 only if CE eligibility/terms are confirmed | unresolved |
| Total modeled infrastructure envelope | Both stages, prompt cleanup | about 0.31 |

Expected use should be below this resource-time scenario; actual usage is not
measured. Propose **$1 allowance for A + $1 for B = $2 operator maximum**, A cleaned
within one hour of deployment and B within four hours of launch. This is NOT an
AWS-enforced billing ceiling. Billing lags, failed teardown, taxes and retained
resources can exceed it. No Budget, alerts, recurring scheduler or commitment is
created. Abort new work when resource/time limits are reached; do not wait for
billing to catch up. If a hard billing guarantee is required, do not deploy.
Rates are supported by the official sources below; gp3 .08 is a published pricing
example and still needs region-specific verification at approval time.

## Real acceptance for issue #10

Keep all local config under an isolated HOME/XDG/TSH_HOME; use a disposable repo,
synthetic transcript, dedicated tmux socket and non-production test session.
Install this pinned t checkout remotely after approval, with zsh/git/tmux/rsync.
Use an inert agent fixture for transport/ownership checks; do not imply that this
validates provider login, model execution or actual Codex/Claude resume.

| Gate | Evidence required |
| --- | --- |
| Trust/auth | Validated TLS + SSH CA; interactive MFA login; tsh status showing identity/expiry; no secret values in report |
| SSH transport | tsh ssh and OpenSSH via tsh config both execute a nonce command with captured output and exit 0; failing command preserves nonzero exit |
| Host setup | Register explicit Teleport target alias in isolated t config; check wildcard tsh config is not silently mistaken for host inventory |
| t on | Remote zsh -lic executes quoting-sensitive args, resolves installed t, returns correct status; no accidental local fallback |
| Sessions | Remote session create/list/attach/detach/reconnect with PTY; assert only one writer/owner, expected worktree and socket |
| File transfer | OpenSSH rsync both directions on small fixtures including spaces; SHA256 equality; existing destination/mtime protection preserved; test tsh scp separately if documented |
| Beam | Synthetic transcript/worktree move each direction; source ownership released before destination starts; interruption cannot create duplicate writer or lose source artifacts |
| Expiry/denial | Real short-TTL cert expires; new SSH, t on, background BatchMode sync and beam fail promptly with useful re-login error; failed transfer does not kill source owner; re-login recovers |
| RBAC | Unapproved Linux login/node denied; does not broaden role to make test pass |
| Network failure | Unreachable proxy gives bounded error; no direct production SSH fallback |
| Cleanup | Both stacks/resources absent; temporary local identity/trust removed; no active lab sessions |

Capture pinned versions, sanitized argv/status/timing, hashes and Teleport audit
identifiers. Do not publish real session recordings or credentials. Local mocks
only establish CLI behavior. A supported claim requires real TLS/auth/SSH/transfer
and session acceptance. If application changes are needed, implement a separate
focused #10 change after coordinating the overlapping beam branch. README must
continue to describe Teleport as unsupported until coverage/docs actually ship.

## Cleanup and rollback owner

Proposed executor owns immediate teardown; a named account owner must accept
fallback responsibility if it disconnects. Owner name is unresolved, not assumed.
Stage A uses its existing receipt-scoped cleanup. Stage B records stack ARN,
instance/volume/network IDs and creation time in private local receipts; on failure
inspect stack events, reconcile ambiguous creates, never blindly rerun CreateStack.
Delete exactly the recorded B stack after collecting sanitized evidence, including
on setup/test failure. Verify EC2 terminated, EBS/ENI/IP gone and lab VPC/SG/routes
removed; do not use bulk account deletion. Root DeleteOnTermination and no snapshots
mean cluster keys and recordings disappear with the disk. Remove only this run's
local client credentials/CA files/hosts mapping and any temporary operator grants.
Leave the existing SSH key pair untouched. Retain sanitized evidence, not auth.

## Recommended defaults for the deployment decision

Use these defaults unless the sandbox owner's policy requires a change. They are
recommendations to approve together, not permissions already granted:

| Decision | Recommended default |
| --- | --- |
| Sequence | Finish and clean up AgentCore A before starting Teleport B |
| AWS region | us-east-1, standard on-demand pricing |
| Teleport host | Template as written: one Ubuntu 22.04 x86_64 t3.small, standard credits, 8 GiB gp3, no instance role |
| Teleport edition/version | CE 18.11.3 candidate from the official demo, subject to eligibility and security/artifact verification |
| Auth | Local MFA user, Linux login taccept without sudo, lab-only RBAC, one-hour maximum certificate TTL |
| TLS | Private lab CA, scoped client trust in an isolated Linux test environment; hostname taccept.test with isolated name resolution |
| Access | Existing sandbox SSH key; one operator/test-client public IPv4 /32 for ports 22/443 |
| Run bounds | A: four calls and one-hour teardown; B: four-hour teardown, no reboot or extension |
| Allowance | $1 per stage, $2 total operator allowance; no hard AWS billing cap |
| Rollback | Executor deletes receipt-identified resources immediately on success/failure; named owner covers disconnection |

The owner still needs to supply the account, existing profile/role and accessible
execution machine, client /32, existing SSH key name, any required permissions
boundary/customer KMS policy, CE eligibility, fallback cleanup owner and run window.
Once account access is available, the executor can resolve the official Ubuntu
AMI ID, current artifact hashes and exact resource ARNs for approval; the owner
need not research these technical identifiers. Client-scoped TLS trust must be
validated locally before starting the four-hour cloud clock. An unsupported trust
mechanism, license mismatch or newly required IAM action means stop and revise.

## Bundled confirmation required before any provisioning

Ask the parent/owner to fill and approve all of the following together:

1. Sandbox account ID, approved us-east-1 or replacement region, existing AWS
   profile/role and execution machine; mandatory boundary/KMS requirements.
2. A's exact generated bundle and listed IAM actions, four-invocation acceptance,
   $1 allowance/one-hour teardown; B's reviewed template, owner-verified AMI ID,
   existing key pair, operator/test /32, listed EC2/CloudFormation permissions,
   $1 allowance/four-hour teardown. Combined proposed maximum allowance: $2.
3. B's public /32-only TCP 22/443 ingress and Internet TCP 80/443 egress; edition,
   exact version/artifacts, CE eligibility or existing enterprise license, approved
   disposable CA/TLS trust method, test user/MFA/RBAC/cert TTL. No new legal terms
   are accepted by this proposal. No persistent workstation/global auth changes.
4. Executor teardown authorization for exactly these new resources, named fallback
   cleanup owner, fixed start/end UTC window, receipt-based rollback on failure.

Unresolved values prevent final policy materialization and deployment, not local
preparation. There is no deploy command here to run by accident. Validate template
locally, then with CloudFormation using the authorized read-only validation action
before CreateStack. Confirm current identity and reject a mismatching account.

## Local validation and handoff

Working branch: `cursor/staged-aws-acceptance`, separate worktree based on PR #73.
Local-only review bundle: `/tmp/t-agentcore-staged01` uses synthetic account
111122223333; it must never be mistaken for an approved target. Its agent ZIP
SHA256 is `e1849afd29c26ab055a51c22cdbb5025a05c6f6d9a53e50ffc22213538a332dc`.
The fixture/runtime code was reused unchanged.

`host.json` passes cfn-lint 1.57.2 for us-east-1, JSON parsing, cloud-config YAML
parsing, and local assertions covering /32 restriction, allowed ingress ports,
standard credits, encrypted delete-on-termination disk, IMDSv2 and absence of IAM
resources/profile. This is schema/static validation, not AWS acceptance or proof
that the timer runs on the chosen AMI. No changed Bash or zsh files; optional lint
of the unchanged AgentCore shell scripts could not run because shellcheck is absent.

Repository check: `python -m pytest --cov --cov-fail-under=97` produced
1825 passed, 57 skipped, 3 failed; coverage 97.58%. All three failures reproduce
on the unchanged `/workspace/t` checkout: two beam tests require missing rsync;
`test_git_roots_plain_repo_subdir_and_outside` unexpectedly resolves `/tmp` as a
Git root. The 14 existing AgentCore fixture tests pass. Full log:
`/tmp/t-staged-pytest.log`; baseline reproduction: `/tmp/t-staged-baseline.log`.
No application code was changed to mask these failures. tmux and AWS CLI are absent,
so skipped/inapplicable live tests are not acceptance evidence.

The owner explicitly approved publishing this infrastructure draft under the
repository CLA and MIT terms. Keep the PR in draft while deployment decisions are
pending; publication is not provisioning, IAM, security or spending approval.
No GitHub allowlist or release changes are part of this task.

## Official references checked 2026-10-10

- [Issue 9](https://github.com/agenthangar/t/issues/9) (closed by #72; live acceptance still needed)
- [Issue 10](https://github.com/agenthangar/t/issues/10) (open)
- [Single-instance Teleport demo](https://goteleport.com/docs/get-started/deploy-community/)
- [tsh reference: config/login/status/ssh/scp](https://goteleport.com/docs/reference/cli/tsh/)
- [Certificate lifetimes](https://goteleport.com/docs/connect-your-client/teleport-clients/tsh/)
- [Community licensing notice](https://goteleport.com/blog/teleport-community-license/)
- [Current pricing/enterprise options](https://goteleport.com/pricing/)
- [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [AWS t3.small price example](https://aws.amazon.com/disaster-recovery/pricing/)
- [EBS pricing](https://aws.amazon.com/ebs/pricing/)
- [Public IPv4 pricing](https://aws.amazon.com/vpc/pricing/)
- [EC2 IAM action/resource mapping](https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonec2.html)
