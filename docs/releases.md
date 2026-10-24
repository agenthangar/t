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

The workflow compares the fetched main commit with the highest published stable
`vX.Y.Z` release, ignoring drafts and prereleases. No new commits means no release.
All commits count, including documentation and workflow changes. When changes
exist, the newest push-triggered CI run for that exact commit must have completed
successfully. Pending, failed, cancelled, or missing CI leaves the commits for a
later run. The workflow does not enable macOS CI or native desktop tests.

For eligible changes, automation increments only the patch number. It pins the
commit, builds the runtime archive, verifies checksums and installation in an
isolated home, and checks the installed version. It then creates a draft with
commit notes and a comparison link, uploads `t.tar.gz` and `SHA256SUMS`, publishes
the release, and dispatches the existing Homebrew formula update. It does not
commit a version bump back to main.

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
  succeeds but the tap update fails, rerun the failed job or use the explicit
  tag invocation above. An unchanged main does not generate a replacement release.

The implementation is in [scripts/release.py](../scripts/release.py). Its planning
command is read-only; publishing is confined to the workflow's write-permitted job.
