# Release policy and automation

During pre-1.0 development, routine fixes, polish, installation improvements,
and incremental additions ship as patch releases. Minor versions mark substantial
milestones explicitly chosen by a maintainer. Version 1.0 is an explicit stability
commitment, not an automatic consequence of a date, commit count, or feature label.

Each version component is an integer. For example, `v0.1.9` is followed by
`v0.1.10`, then `v0.1.11`; reaching nine does not require a minor increment.
Published tags and artifacts remain immutable. Existing releases through
`v0.6.0` retain their numbers, so the next routine release is `v0.6.1`.
This is the project's pre-1.0 policy; revisit API compatibility rules explicitly
when declaring 1.0.

## Nightly releases

The [Releases workflow](../.github/workflows/release.yml) runs daily at **09:17 UTC**
(01:17 Pacific standard time / 02:17 Pacific daylight time). GitHub may delay
scheduled runs. It also supports **Run workflow** on `main` with an empty `tag`
input to perform the same check immediately.
Select `validate_only=true` to exercise the complete nightly validation without
publishing a release.

The workflow compares the fetched main commit with the highest published stable
`vX.Y.Z` release, ignoring drafts and prereleases. No new commits means no release.
All commits count, including documentation and workflow changes. Every nightly
run invokes full Linux CI for the pinned commit, even when there is nothing new
to publish. This includes all tests, combined 97% coverage, real compatibility
tests on both fzf versions, install smoke, CLI/shell contracts and security scans.
Failed or cancelled validation fails the workflow and prevents publication.
The workflow does not enable macOS CI or native desktop tests.

For eligible changes, automation increments only the patch number. It pins the
commit, waits for full validation, builds the runtime archive, verifies checksums and installation in an
isolated home, and checks the installed version. It then creates a draft with
commit notes and a comparison link, uploads `t.tar.gz` and `SHA256SUMS`, publishes
the release, and dispatches the existing Homebrew formula update. It does not
commit a version bump back to main.

The successful full CI gate produces a `release-validation` artifact. Publication
requires its commit, workflow run ID and attempt to match the archive checkout
and current run. A fast PR/push check, evidence from a different commit, or a
previous attempt cannot authorize a release. Testing precedes tag creation;
separate cron schedules are never used to order validation and publication.

Publication and tap dispatch run in the same workflow because releases created
with `GITHUB_TOKEN` do not start a second release-triggered workflow. The existing
`T_FORMULA_APP_CLIENT_ID` variable and `T_FORMULA_APP_PRIVATE_KEY` secret provide
the tap dispatch; no additional credential is required.

## Manual milestones and recovery

- Choose minor or major milestones explicitly and publish their tags/releases
  manually from a tested main commit. Subsequent nightly releases increment that
  version's patch component. Commit-message labels never cause an automatic
  minor or major increment.
- To verify/reconcile an existing published stable release and retry its tap
  update, run **Releases → Run workflow** on `main` with its exact `tag`, such as
  `v0.6.1`. This preserves the release's tag, notes, and existing asset bytes.
- An interrupted automatic draft is resumed at its original tested commit on the
  next run, even if main has advanced. Newer commits wait for a subsequent release.
  Automation never takes over an unrelated draft or an existing bare tag.
- Missing assets may be uploaded on retry. Existing archives must match the
  pinned Git tree and their checksums; conflicting assets cause failure instead
  of being overwritten. Investigate such a failure before retrying.
- Runs are serialized without cancelling an active publication. If publication
  succeeds but the tap update fails, rerun **all jobs** or use the explicit
  tag invocation above. An unchanged main does not generate a replacement release.
  Rerunning only a failed publication job cannot reuse validation from an older
  attempt. Recovery revalidates the exact original commit before any asset writes.

## Scheduled failure alerts

[Scheduled health](../.github/workflows/scheduled-health.yml) runs when Releases
or Scorecard completes and every six hours. Set the repository Actions variable
`CI_ALERT_ASSIGNEE` to the maintainer's GitHub login. It creates or reopens one
assigned, mentioned issue per workflow, with the run, commit, attempt and failed
job links. Repeated checks of the same incident do not notify again; successful
recovery closes the issue. A new failed attempt posts a fresh mention.

The watchdog also alerts when a workflow is disabled, a run remains incomplete
for two hours, no nightly run has started within 36 hours, or no weekly Scorecard
run has started within eight days. A pending recovery does not close an incident.
GitHub schedules are best effort: this watchdog also runs on GitHub, so a platform
outage that prevents every workflow from starting requires external monitoring.

Use **Scheduled health → Run workflow → test_notification=true** to send a
clearly labeled delivery test. The recipient should subscribe to the issue and
enable GitHub web/email notifications for mentions and assignments. The workflow
cannot change personal email or notification preferences. It uses `GITHUB_TOKEN`
with issues write access; no mail or chat secret is needed. Only trusted main
code executes; source-run logs and artifacts are not executed by the notifier.

The implementation is in [scripts/release.py](../scripts/release.py). Its planning
command is read-only; publishing is confined to the archive job. The planner's
token also needs push access because GitHub only lists drafts to push-capable
callers, which is necessary to resume an interrupted release.
