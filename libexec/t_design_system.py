"""Read-only repository design discovery and an explicit guidance-drafting task."""

import os
from pathlib import Path, PurePosixPath
import re
import runpy
import subprocess

_spinner = runpy.run_path(str(Path(__file__).with_name('t_progress.py')))['spinner']
EXCLUDED = {'.git', 'node_modules', 'vendor', 'dist', 'build', 'target', 'generated', '.next', '.venv',
            'venv', '.cache', 'coverage', '__pycache__'}
MAX_FILES = 20000
MAX_READS = 128
MAX_BYTES = 65536


def validate_source(value):
    if not isinstance(value, dict) or set(value) != {'path'}:
        raise ValueError('design system requires a repository-relative path')
    path = value['path']
    if (not isinstance(path, str) or not path or len(path) > 1024 or
            any(ord(char) < 32 or ord(char) == 127 for char in path) or
            '\\' in path or PurePosixPath(path).is_absolute() or
            any(part in ('', '.', '..') for part in path.split('/')) or
            any(part in EXCLUDED for part in path.split('/'))):
        raise ValueError('design system path must stay inside the repository, outside generated and dependency directories')
    return value


def source_path(root, relative):
    validate_source({'path': relative})
    root = Path(root).resolve()
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('design system paths must not follow symlinks')
    if not current.resolve().is_relative_to(root) or not (current.is_file() or current.is_dir()):
        raise ValueError('design system source is missing or outside the repository')
    return current


@_spinner('Finding repository design systems')
def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args], capture_output=True,
                            timeout=10, env={key: value for key, value in os.environ.items()
                                            if not key.startswith('COV_CORE_')})
    if result.returncode:
        raise ValueError('Cannot inspect repository: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout


def workspace(canonical, cwd=None):
    """Use the invoking worktree only when its canonical repository matches."""
    canonical = Path(canonical).resolve()
    try:
        common = git(cwd or os.getcwd(), 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
        if Path(common).resolve() == (canonical / '.git').resolve():
            return Path(git(cwd or os.getcwd(), 'rev-parse', '--show-toplevel').decode().strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return canonical


@_spinner('Finding repository design systems')
def discover(root):
    """Bounded, local evidence; never imports or executes repository code."""
    root = Path(root).resolve()
    raw = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
    files = sorted(set(raw.decode('utf-8', errors='replace').rstrip('\0').split('\0')))
    warnings = []
    if len(files) > MAX_FILES:
        warnings.append(f'Discovery limited to {MAX_FILES} files; enter a source path if needed.')
        files = files[:MAX_FILES]
    candidates, groups = {}, {}

    def add(path, evidence):
        try:
            source_path(root, path)
        except ValueError:
            return
        candidates.setdefault(path, set()).add(evidence)

    reads = 0
    for relative in files:
        try:
            path = source_path(root, relative)
        except ValueError:
            continue
        if not path.is_file():
            continue
        parts = PurePosixPath(relative).parts
        name = path.name.lower()
        explicit = name in ('design.md', 'design-system.md', 'design_system.md')
        if explicit:
            add(relative, 'Design-system entry point')
        if path.suffix.lower() in ('.md', '.mdx') and reads < MAX_READS:
            reads += 1
            try:
                with path.open('rb') as stream:
                    body = stream.read(MAX_BYTES).decode('utf-8', errors='replace')
            except OSError:
                warnings.append(f'Cannot read {relative}.')
                continue
            if re.search(r'(?im)^#{1,4}\s+.*(?:design system|command and terminal contract)', body):
                add(relative, 'Design or terminal contract documentation')
            if name in ('agents.md', 'claude.md', 'design.md', 'readme.md'):
                for label, target in re.findall(r'\[([^\]]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)', body):
                    if not re.search(r'design|terminal contract|ui guidance', label, re.I):
                        continue
                    target = target.split('#', 1)[0]
                    if not target or ':' in target or target.startswith('/'):
                        continue
                    linked = os.path.normpath(str(PurePosixPath(relative).parent / target))
                    add(linked, f'Linked from {relative}')
        if len(parts) > 2 and parts[0] in ('packages', 'libs'):
            group = '/'.join(parts[:2])
        elif '.storybook' in parts:
            group = '/'.join(parts[:parts.index('.storybook')])
        else:
            group = next(('/'.join(parts[:i + 1]) for i, part in enumerate(parts[:-1])
                          if part in ('ui', 'design-system', 'design_system')), '')
        if group:
            evidence = groups.setdefault(group, set())
            if 'token' in name:
                evidence.add('Local design tokens')
            if 'components' in parts or '.stories.' in name:
                evidence.add('Local component library')
            if '.storybook' in parts:
                evidence.add('Storybook configuration')
    if reads == MAX_READS:
        warnings.append(f'Document inspection limited to {MAX_READS} files; explicit paths remain selectable.')
    for group, evidence in groups.items():
        if len(evidence) >= 2:
            for item in evidence:
                add(group, item)
    return {'candidates': [{'path': path, 'evidence': sorted(evidence)}
                           for path, evidence in sorted(candidates.items(),
                               key=lambda item: (-('Design-system entry point' in item[1]), -len(item[1]), item[0]))],
            'warnings': warnings}


def draft_prompt():
    return '''Prepare repository design-system guidance for user review.
Read repository instructions and inspect existing UI, terminal interfaces, shared
components, tokens and documentation. Follow issue-before-code and worktree rules.
Draft a root DESIGN.md entry point linking to authoritative design guidance and
shared implementations. Add a concise reference in AGENTS.md directing agents to
read it before changing user-facing behavior. Extend existing design docs, or
draft grounded guidance if absent, including faithful local interactive planning
previews with labeled sample data and isolated configuration. Preserve existing
content. Document evidence and uncertainties; do not invent design rules or checks.
Workflow configuration owns preview timing and approval: do not duplicate those
gates in repository prose. This task is a draft only: present the actual diff and
validation for approval, then stop before committing, pushing, opening a PR or
merging. Do not change workflow configuration or automatically select the source.
After approval, the user can refresh design-system discovery and select DESIGN.md.
'''
