#!/usr/bin/env python3
"""Bounded essential flows shared by local snapshots and routine CI.

Every entry is an exact node ID, including parameter IDs. Adding a regression or
parameter never silently enlarges this selection. Missing, skipped, or extra
cases fail closed. Full validation still collects the entire suite nightly.
"""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
NODES = (
    'tests/test_dev_isolation.py::test_shell_helpers_switch_before_prompt_without_changing_path',
    'tests/test_dev_isolation.py::test_source_selection_interrupt_exits_quietly[libexec/t_dev.py]',
    'tests/test_dev_isolation.py::test_source_selection_interrupt_exits_quietly[bin/t]',
    'tests/test_dev_isolation.py::test_interrupted_initial_load_keeps_retryable_shell_function',
    'tests/test_dev_isolation.py::test_ctrl_c_during_startup_recovers_in_same_interactive_shell[source]',
    'tests/test_dev_isolation.py::test_ctrl_c_during_startup_recovers_in_same_interactive_shell[login]',
    'tests/test_dev_isolation.py::test_interrupted_reload_does_not_dispatch_stale_source[_t_reload]',
    'tests/test_dev_isolation.py::test_interrupted_reload_does_not_dispatch_stale_source[t open example --new]',
    'tests/test_dev_isolation.py::test_selector_does_not_probe_unrelated_repositories',
    'tests/test_update_prompt_shell.py::test_shell_command_reloads_after_update_then_runs_once',
    'tests/test_session_discovery.py::test_mcp_warm_context_never_scans_and_uses_memory_generation',
    'tests/test_session_discovery.py::test_local_snapshot_includes_unindexed_live_sessions_and_repo_scopes',
    'tests/test_session_lifecycle.py::test_public_cli_open_and_reattach_keep_one_owner[codex-repo-1]',
    'tests/test_session_lifecycle.py::test_concurrent_alias_opens_keep_one_worktree_owner',
    'tests/test_session_lifecycle.py::test_public_list_keeps_live_owner_and_worktree_unchanged[codex]',
    'tests/test_session_lifecycle.py::test_public_close_stops_owner_and_preserves_uncommitted_work[codex]',
    'tests/test_session_lifecycle.py::test_public_remote_open_uses_login_shell_and_reattaches[codex]',
    'tests/test_session_lifecycle.py::test_public_resume_revives_saved_thread_once[codex-False-repo-1]',
    'tests/test_session_lifecycle.py::test_public_resume_revives_saved_thread_once[claude-True-1]',
    # Small policy checks keep the smoke/nightly and publication boundaries gated.
    'tests/test_ci_policy.py::test_routine_scope_ignores_changed_paths',
    'tests/test_ci_policy.py::test_required_gate_rejects_failures_and_skips',
    'tests/test_ci_policy.py::test_workflow_modes_preserve_nightly_release_and_security_boundaries',
    'tests/test_ci_policy.py::test_only_successful_full_gate_writes_same_attempt_evidence',
    'tests/test_smoke.py::test_smoke_selection_is_bounded_and_fail_closed',
    'tests/test_local_smoke.py::test_changed_code_merge_and_failed_smoke_invalidate_evidence',
    'tests/test_local_smoke.py::test_emitted_instructions_require_fresh_local_smoke',
)


def check_manifest(data):
    if (len(NODES) != len(set(NODES)) or data['exitstatus'] != 0
            or sorted(data['full']) != sorted(NODES)
            or sorted(data['selected']) != sorted(NODES)
            or data['outcomes'] != dict.fromkeys(NODES, 'passed')):
        raise ValueError('essential smoke requires every exact selected case to pass (no skips)')


def main():
    destination = ROOT / 'artifacts/smoke'
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / 'manifest.json'
    manifest.unlink(missing_ok=True)
    result = subprocess.run([
        sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.ci_shards',
        '--ci-manifest=' + str(manifest), '--junitxml=' + str(destination / 'junit.xml'),
        '--durations=10', *NODES,
    ], cwd=ROOT)
    if result.returncode:
        return result.returncode
    check_manifest(json.loads(manifest.read_text()))
    print(f'Essential smoke: {len(NODES)} exact cases passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
