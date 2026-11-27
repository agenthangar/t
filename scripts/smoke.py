#!/usr/bin/env python3
"""Bounded essential flows for routine CI and focused, opt-in macOS checks.

Every entry is an exact node ID, including parameter IDs. Adding a regression or
parameter never silently enlarges this selection. Missing, skipped, or extra
cases fail closed. Full validation still collects the entire suite nightly.
"""
import argparse
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
    'tests/test_dev_isolation.py::test_interrupted_reload_does_not_dispatch_stale_source[t open example --new]',
    'tests/test_dev_isolation.py::test_selector_does_not_probe_unrelated_repositories',
    'tests/test_update_prompt_shell.py::test_shell_command_reloads_after_update_then_runs_once',
    'tests/test_live_inventory.py::test_public_wait_isolates_servers_and_retries_failed_scan',
    'tests/test_installed_session_details.py::test_installed_first_list_and_picker_have_metadata',
    'tests/test_session_discovery.py::test_mcp_warm_context_never_scans_and_uses_memory_generation',
    'tests/test_session_discovery.py::test_local_snapshot_includes_unindexed_live_sessions_and_repo_scopes',
    'tests/test_picker_ui.py::test_public_open_wait_selects_from_partial_discovery[marked]',
    'tests/test_session_lifecycle.py::test_public_cli_open_and_reattach_keep_one_owner[codex-repo-1]',
    'tests/test_session_lifecycle.py::test_concurrent_alias_opens_keep_one_worktree_owner',
    'tests/test_session_lifecycle.py::test_public_close_stops_owner_and_preserves_uncommitted_work[codex]',
    'tests/test_session_lifecycle.py::test_public_remote_open_uses_login_shell_and_reattaches[codex]',
    'tests/test_session_lifecycle.py::test_public_resume_revives_saved_thread_once[codex-False-repo-1]',
    'tests/test_session_lifecycle.py::test_public_resume_revives_saved_thread_once[claude-True-1]',
    'tests/test_auto_recovery.py::test_holding_panes_resume_exact_conversation_with_offline_peer',
    'tests/test_auto_recovery.py::test_holding_notice_explains_local_scan_failure_without_ssh_diagnostics',
    'tests/test_open_recovery.py::test_captured_recovery_shell_preserves_terminal_foreground',
    'tests/test_offline_open.py::test_interactive_numbered_open_on_incomplete_scan_uses_resume_picker[selected-0]',
    # Small policy checks keep the smoke/nightly and publication boundaries gated.
    'tests/test_ci_policy.py::test_routine_scope_ignores_changed_paths',
    'tests/test_ci_policy.py::test_required_gate_rejects_failures_and_skips',
    'tests/test_ci_policy.py::test_workflow_modes_preserve_nightly_release_and_security_boundaries',
    'tests/test_ci_policy.py::test_only_successful_full_gate_writes_same_attempt_evidence',
    'tests/test_smoke.py::test_smoke_selection_is_bounded_and_fail_closed',
    'tests/test_local_smoke.py::test_changed_code_merge_and_failed_smoke_invalidate_evidence',
    'tests/test_local_smoke.py::test_emitted_instructions_require_fresh_local_smoke',
)

MACOS_NODES = {
    'discovery': (
        'tests/test_installed_session_details.py::test_installed_first_list_and_picker_have_metadata',
        'tests/test_installed_session_details.py::test_installed_61_slots_have_metadata_while_history_is_blocked',
        'tests/test_agent_seam.py::test_sweep_preserves_detached_agent_cwd_until_exit[daemon-False]',
        'tests/test_nightr_packaging.py::test_darwin_process_identity_uses_kernel_birth_and_boot_session',
    ),
    'terminal': (
        'tests/test_terminal_close.py::test_real_terminal_loss_differs_from_detach',
        'tests/test_terminal_close.py::test_public_terminal_closure[detach-close]',
        'tests/test_picker_ui.py::test_resume_pan_suspension_and_bracketed_paste_restore_terminal',
    ),
    'native': (
        'tests/test_nosleep_runtime.py::test_zsh_nosleep_reads_live_macos_power_assertions',
    ),
}


def check_manifest(data, nodes=NODES):
    if (len(nodes) != len(set(nodes)) or data['exitstatus'] != 0
            or sorted(data['full']) != sorted(nodes)
            or sorted(data['selected']) != sorted(nodes)
            or data['outcomes'] != dict.fromkeys(nodes, 'passed')):
        raise ValueError('every exact selected case must pass (no skips)')


def main(macos_scope=None):
    if macos_scope is not None and macos_scope not in MACOS_NODES:
        raise ValueError(f'unknown macOS scope {macos_scope!r}')
    nodes = NODES if macos_scope is None else MACOS_NODES[macos_scope]
    flags = ['--run-macos-ui'] if macos_scope == 'native' else []
    name = 'smoke' if macos_scope is None else f'macos-{macos_scope}'
    destination = ROOT / 'artifacts' / name
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / 'manifest.json'
    manifest.unlink(missing_ok=True)
    result = subprocess.run([
        sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.ci_shards',
        '--ci-manifest=' + str(manifest), '--junitxml=' + str(destination / 'junit.xml'),
        '--durations=10', *flags, *nodes,
    ], cwd=ROOT)
    if result.returncode:
        return result.returncode
    check_manifest(json.loads(manifest.read_text()), nodes)
    label = 'Essential smoke' if macos_scope is None else f'macOS {macos_scope}'
    print(f'{label}: {len(nodes)} exact cases passed')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--macos', choices=MACOS_NODES, help='run the selected macOS checks')
    args = parser.parse_args()
    if args.macos and sys.platform != 'darwin':
        parser.error('--macos requires a Mac')
    sys.exit(main(args.macos))
