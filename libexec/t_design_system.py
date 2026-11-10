"""Read-only repository design discovery and an explicit guidance-drafting task."""

import os
from pathlib import Path, PurePosixPath
import re
import runpy
import subprocess
import tempfile

_spinner = runpy.run_path(str(Path(__file__).with_name('t_progress.py')))['spinner']
EXCLUDED = {'.git', 'node_modules', 'vendor', 'dist', 'build', 'target', 'generated', '.next', '.venv',
            'venv', '.cache', 'coverage', '__pycache__'}
MAX_FILES = 20000
MAX_READS = 128
MAX_BYTES = 65536


def validate_source(value):
    if value == {'mode': 'off'}:
        return value
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
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=5')
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args], capture_output=True,
                            input=b'', timeout=10, env=env)
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
    return _discover_files(root, files)


def _discover_files(root, files):
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


def discover_revision(root, revision):
    """Inspect a bounded snapshot without checking out files or running filters."""
    entries = git(root, 'ls-tree', '-rlz', revision).decode('utf-8', errors='replace').rstrip('\0').split('\0')
    files, warnings, reads = [], [], 0
    with tempfile.TemporaryDirectory(prefix='t-design-scan-') as directory:
        snapshot = Path(directory)
        for entry in entries[:MAX_FILES]:
            if not entry:
                continue
            metadata, relative = entry.split('\t', 1)
            mode, kind, oid, size = metadata.split()
            if kind != 'blob' or mode not in ('100644', '100755'):
                continue
            try:
                validate_source({'path': relative})
            except ValueError:
                continue
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = b''
            if target.suffix.lower() in ('.md', '.mdx') and reads < MAX_READS:
                reads += 1
                if int(size) <= MAX_BYTES:
                    body = git(root, 'cat-file', 'blob', oid)
                else:
                    warnings.append(f'Document too large to inspect: {relative}.')
            target.write_bytes(body)
            files.append(relative)
        result = _discover_files(snapshot, sorted(files))
    if len(entries) > MAX_FILES:
        warnings.append(f'Discovery limited to {MAX_FILES} files; enter a source path if needed.')
    result['warnings'].extend(warnings)
    return result


@_spinner('Refreshing repository design systems')
def refresh(canonical):
    """Explicit rescan includes canonical and latest origin HEAD; never moves a checkout."""
    root = workspace(canonical)
    result = discover(root)
    known = {item['path'] for item in result['candidates']}

    def include(found, location):
        for item in found['candidates']:
            if item['path'] not in known:
                result['candidates'].append(dict(item, location=location))
                known.add(item['path'])
        result['warnings'].extend(found['warnings'])

    try:
        if root.resolve() != Path(canonical).resolve():
            include(discover(canonical), 'Registered checkout')
        if 'origin' in git(canonical, 'remote').decode().splitlines():
            # Fetch objects by the advertised commit without updating refs or FETCH_HEAD.
            advertised = git(canonical, 'ls-remote', '--symref', 'origin', 'HEAD').decode().splitlines()
            revision = next((line.split()[0] for line in advertised
                             if re.fullmatch(r'[0-9a-f]{40,64}\s+HEAD', line)), None)
            if revision is None:
                raise ValueError('origin has no default-branch HEAD')
            git(canonical, 'fetch', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head', 'origin', revision)
            include(discover_revision(canonical, revision), 'Default branch · origin/HEAD')
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result['warnings'].append('Default-branch scan unavailable: ' + str(exc))
    return result


def automatic_source(result):
    """Deterministic preference, without silently resolving ambiguous evidence."""
    candidates = result['candidates']
    paths = [item['path'] for item in candidates]
    if 'DESIGN.md' in paths:
        return 'DESIGN.md'
    if result.get('warnings'):
        return None
    roots = [path for path in paths if '/' not in path and path.lower() in
             ('design-system.md', 'design_system.md')]
    if len(roots) == 1:
        return roots[0]
    return paths[0] if len(paths) == 1 else None


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
