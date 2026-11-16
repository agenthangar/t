#!/usr/bin/env python3
"""Read native CLI trust decisions without running the full configuration sync."""
import json
import os
from pathlib import Path
import runpy
import sys


def roots(cwd):
    directory = Path(cwd).resolve()
    for root in (directory, *directory.parents):
        marker = root / '.git'
        if marker.is_dir():
            return root, root
        if not marker.is_file():
            continue
        text = marker.read_text().strip()
        if text.startswith('gitdir: '):
            gitdir = (root / text[8:]).resolve()
            common_file = gitdir / 'commondir'
            if common_file.is_file():
                common = (gitdir / common_file.read_text().strip()).resolve()
                if common.name == '.git' and (common / 'config').is_file():
                    return root, common.parent
        return root, root
    return directory, directory


def decision(agent, cwd):
    """Return trusted, untrusted or unknown without inferring parent-folder trust."""
    try:
        root, canonical = roots(cwd)
        if agent == 'codex':
            config = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml'
            loads = runpy.run_path(str(Path(__file__).with_name('t_toml.py')))['loads']
            projects = loads(config.read_text()).get('projects', {})
            decisions = {Path(path).resolve(): value.get('trust_level')
                         for path, value in projects.items()
                         if Path(path).is_absolute() and isinstance(value, dict)}
            values = [decisions.get(path) for path in (Path(cwd).resolve(), root, canonical)]
            if 'untrusted' in values:
                return 'untrusted'
            if 'trusted' in values:
                return 'trusted'
        if agent == 'claude':
            home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home())
            projects = json.loads((home / '.claude.json').read_text()).get('projects', {})
            if any(Path(path).is_absolute() and Path(path).resolve() in (root, canonical)
                   and isinstance(value, dict) and value.get('hasTrustDialogAccepted') is True
                   for path, value in projects.items()):
                return 'trusted'
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return 'unknown'


def covered(agent, cwd):
    """An existing denial is a decision too: automatic sync must not bypass it."""
    return decision(agent, cwd) != 'unknown'


def trusted(agent, cwd):
    return decision(agent, cwd) == 'trusted'


if __name__ == '__main__':
    sys.exit(0 if len(sys.argv) == 3 and covered(*sys.argv[1:]) else 1)
