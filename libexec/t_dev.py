"""Select a development worktree without changing the shared installation."""
import os
from pathlib import Path
import re
import subprocess
import sys


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR')}
    try:
        result = subprocess.run(['git', '-C', str(root), *args], env=env,
                                capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def canonical(root):
    root = Path(root).resolve()
    if (root / '.t-release-version').exists():
        return None  # Archive installs may live beneath an unrelated Git tree.
    common = git(root, 'rev-parse', '--git-common-dir')
    if not common:
        return None
    common = (root / common).resolve()
    primary = common.parent
    return primary if common.name == '.git' and git(primary, 'rev-parse', '--show-toplevel') == str(primary) else None


def valid(root, primary):
    root = Path(root).resolve()
    try:
        if root == primary or not (root / '.git').is_file() or (root / '.t-install-version').read_text().strip() != '1':
            return False
        if not all((root / asset).is_file() for asset in ('bin/t', 't.plugin.zsh', 'libexec/t_dev.py', 'libexec/t_repos.py')):
            return False
    except (OSError, UnicodeError):
        return False
    branch = git(root, 'branch', '--show-current')
    return bool((re.fullmatch(r'dev/t-[A-Za-z0-9_.-]+', branch) or branch.startswith('cursor/')) and canonical(root) == primary)


def invocation_owner(owner):
    """A source pin belongs to an active ancestor, never a tmux server's environment."""
    if not str(owner).isdigit():
        return False
    try:
        result = subprocess.run(['ps', '-axo', 'pid=,ppid=,comm='],
                                capture_output=True, text=True, timeout=2)
        if result.returncode:
            return False
        parents = {}
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                parents[int(fields[0])] = (int(fields[1]), Path(fields[2]).name)
        current, seen = os.getppid(), set()
        while current and current not in seen:
            if current == int(owner):
                return True
            seen.add(current)
            parent, name = parents.get(current, (0, ''))
            if name.startswith('tmux'):
                return False
            current = parent
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def select_root(source, cwd=None, env=None, explicit=False):
    source = Path(source).resolve()
    primary = canonical(source)
    if primary is None:
        return source  # Releases and explicitly selected custom installations.
    if explicit and source != primary:
        return source
    env = os.environ if env is None else env
    pinned = env.get('T_DEV_CONTEXT')
    if pinned and (Path(pinned).resolve() == primary or valid(pinned, primary)) and invocation_owner(env.get('T_DEV_CONTEXT_OWNER', '')):
        return Path(pinned).resolve()
    candidate = git(cwd or os.getcwd(), 'rev-parse', '--show-toplevel')
    return Path(candidate).resolve() if candidate and valid(candidate, primary) else primary


if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve()
    print(select_root(root, explicit='--explicit' in sys.argv[2:]))
