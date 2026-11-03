# CI test scheduling and coverage

The required `CI` check keeps the Linux suite serial within four independent
GitHub-hosted runners, with fzf 0.74.5. Each runner collects the full suite,
then selects complete files. `scripts/ci_shards.py` assigns longest estimated
files first to the least-loaded shard; filename and shard index break ties.
New files receive the median known weight. `tests/ci-file-durations.json`
starts with approximate file timings from [the previous full Linux run](https://github.com/agenthangar/t/actions/runs/41232234489)
(fzf 0.74.5, 638.75 seconds). These estimates affect scheduling only.

Every shard requires real fzf/tmux/zsh before testing and sets
`T_REQUIRE_AWS_CONTRACT=1`, so the AWS contract runs once in its assigned file.
A separate fzf 0.44.1 compatibility job selects all tests in
`test_picker_ui.py`, `test_history_picker.py`, `test_history_details.py` and
`test_share_ui.py`. It writes the same explicit selected-test manifest.

Artifacts persist for 14 days: full collection, selected files and node IDs,
per-test outcomes and setup/call/teardown duration totals, JUnit, and uniquely
named coverage data. The coverage job rejects missing/duplicate shards,
differing collection or commits, overlap, incomplete outcomes, and absent or
corrupt coverage. It normalizes checkout prefixes, combines measured lines,
and enforces the existing scope/exclusions and 97% minimum only on the union.
The job summary reports timings, test totals, coverage and the 30 slowest tests.

To refresh scheduling weights explicitly, download the four `shard-*` artifacts
from a successful run into separate directories and run:

```sh
gh run download RUN_ID --pattern 'shard-*' --dir /tmp/t-ci-timings
python scripts/ci_shards.py refresh /tmp/t-ci-timings
```

Review and commit the updated timing file. Refresh does not run during CI and
cannot silently modify scheduling in an existing revision. Multiple downloaded
runs may be placed under separate parent directories; refresh takes the median
of observed file totals.

For a same-commit equivalence audit, dispatch `CI` with `verify_shards=true`.
This default-off mode runs the original full Linux suite with current fzf,
then compares the exact inventory, outcomes, measured source files and executed
lines with the combined shards. Missing artifacts or differences fail `CI`.
Use this mode when changing scheduling. The performance target is routine Linux
completion below six minutes, at least 40% faster than the approximately
736-second original CI baseline; compare GitHub job/run timestamps, including
setup and aggregation, rather than only pytest execution time.

macOS remains default-off. Dispatch with `run_macos=true` to run the full suite
against both fzf versions with native desktop tests enabled. No local validation
suite is needed for the CI workflow; required pre-merge jobs provide validation.
