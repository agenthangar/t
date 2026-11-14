"""Explicit PR test tiers; new test files join the fast suite by default.

Only the expensive integration matrices below are deferred. Changes to their
implementation, test harness or CI itself request the complete Linux suite.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess


EXTENDED = {
    'tests/test_lifecycle_transfer_e2e.py',
    'tests/test_session_lifecycle.py',
    'tests/test_agent_seam.py',
    'tests/test_nosleep_runtime.py',
    'tests/test_nosleep_config.py',
    'tests/test_onboarding_pty.py',
}
# Exercise real concurrent ownership, read-only listing, preservation of dirty
# work and the remote login-shell contract even on the fast path.
FAST_KEEP = {
    'test_run_under_pty_answers_the_compinit_prompt',
    'test_zsh_nosleep_invalid_arguments_leave_power_untouched',
    'test_concurrent_alias_opens_keep_one_worktree_owner',
    'test_public_list_keeps_live_owner_and_worktree_unchanged',
    'test_public_close_stops_owner_and_preserves_uncommitted_work',
    'test_public_remote_open_uses_login_shell_and_reattaches',
}
FULL_PREFIXES = (
    '.github/', 'scripts/', 'bin/', 'zsh/', 'ui/',
    'tests/test_ci_', 'tests/test_release_workflow.py',
    'install.sh', 't.plugin.zsh', 'pyproject.toml', 'requirements',
    'libexec/t_lifecycle', 'libexec/t_remote', 'libexec/t_codex_',
    'libexec/t_resume', 'libexec/t_history', 'libexec/t_candidates',
    'libexec/t_picker', 'libexec/t_share', 'libexec/nosleep',
    'libexec/t_shell_setup', 'libexec/t_onboarding', 'libexec/t_dev',
    'libexec/t_repos', 'libexec/t_recovery', 'libexec/t_updates',
)
FULL_TEST_FILES = EXTENDED | {
    'tests/test_picker_ui.py', 'tests/test_history_picker.py',
    'tests/test_history_details.py', 'tests/test_share_ui.py',
}


def requires_full(paths):
    return any(path.startswith(FULL_PREFIXES) or path in FULL_TEST_FILES
               or (path.startswith('tests/') and not path.rsplit('/', 1)[-1].startswith('test_'))
               for path in paths)


def fast_node(node):
    parts = node.split('::')
    return parts[0] not in EXTENDED or parts[-1].split('[')[0] in FAST_KEEP


def check_results(results, *, full, macos=False, reference=False):
    optional = {'smoke'} if full else {'pytest', 'fzf-legacy', 'coverage'}
    if not macos:
        optional.add('macos')
    if not reference:
        optional.update(('reference', 'verify-shards'))
    required = {'scope', 'cli-contract', 'shell', 'startup-smoke', 'pytest', 'smoke', 'fzf-legacy',
                'coverage', 'reference', 'verify-shards', 'macos', 'playground',
                'gitleaks', 'pip-audit', 'codeql'}
    if set(results) != required:
        raise ValueError('missing or unexpected CI job results')
    for name, job in results.items():
        expected = 'skipped' if name in optional else 'success'
        if job['result'] != expected:
            raise ValueError(f"{name}: {job['result']} (expected {expected})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['scope', 'gate'])
    args = parser.parse_args()
    if args.command == 'scope':
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        full = os.environ.get('FORCE_FULL') == 'true'
        base = os.environ.get('PR_BASE', '')
        if base:
            changed = subprocess.check_output(['git', 'diff', '--name-only', '-z', base, 'HEAD'])
            full |= requires_full(changed.decode().rstrip('\0').split('\0'))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'sha={sha}\nfull={str(full).lower()}\n')
        print(f"{'Full Linux validation' if full else 'Fast regression suite'}: {sha}")
    else:
        full = os.environ['EXPECT_FULL'] == 'true'
        check_results(json.loads(os.environ['NEEDS_JSON']), full=full,
                      macos=os.environ['EXPECT_MACOS'] == 'true',
                      reference=os.environ['EXPECT_REFERENCE'] == 'true')
        # Only a successful full gate produces release evidence. The artifact is
        # consumed in this same workflow attempt, never from a PR or older run.
        if full:
            Path('validation').mkdir(exist_ok=True)
            Path('validation/validation.json').write_text(json.dumps({
                'sha': os.environ['TESTED_SHA'], 'full': True,
                'run_id': os.environ['GITHUB_RUN_ID'],
                'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
            }) + '\n')


if __name__ == '__main__':
    main()
