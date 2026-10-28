"""Account-local staged agent preferences and managed native instructions.

These instructions guide agents; t does not switch models or enforce execution.
"""
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

BEGIN = '<!-- t agent-workflow: begin -->'
END = '<!-- t agent-workflow: end -->'
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
# One catalogue drives validation, menu choices, summaries and native instructions.
# Absent keys mean no preference, not a directive to do the opposite.
BEHAVIORS = {
    'worktree': {'label': 'Worktree', 'choices': {
        'isolated': ('Isolated worktree', 'Reuse an appropriate existing isolated worktree, or create one before implementation.'),
        'current': ('Current worktree', 'Use the current worktree, subject to explicit task instructions and repository safety rules.'),
    }},
    'preview': {'label': 'Local preview', 'choices': {
        'ui_changes': ('Preview UI changes', 'For UI changes, run the available local preview and inspect the changed UI. Report unavailable preview tools or verification limitations accurately.'),
        'on_request': ('Only when requested', 'Run a local preview only when explicitly requested, unless higher-priority instructions require it.'),
    }},
    'validation': {'label': 'Validation', 'choices': {
        'relevant': ('Relevant checks', 'Run the relevant documented checks for the change and report their results.'),
        'full': ('Full documented checks', 'Run the full documented validation suite and report its results and any unavailable checks.'),
    }},
    'delivery': {'label': 'Delivery', 'choices': {
        'review': ('Leave for review', 'Leave changes uncommitted for user review unless explicit task instructions or repository rules require otherwise.'),
        'pull_request': ('Open a pull request', 'Commit, push and open a pull request. This preference alone does not authorize merging.'),
        'auto_merge': ('Auto-merge when authorized', 'Commit, push and open a pull request under repository merge policy and required checks. When authorized and supported, enable auto-merge and monitor it through completion; report any blocker accurately.'),
    }},
}
PRECEDENCE = ('Explicit task instructions > repository rules > custom Markdown > behavior preferences > model-stage guidance. '
              'The selected repository workflow replaces the global default; exclusion suppresses all workflow instructions.')
DEFAULT = {
    'enabled': True,
    'planning': {'model': 'gpt-6-astra', 'effort': 'auto'},
    'implementation': {'model': 'gpt-6.1-sol', 'effort': 'auto'},
    'review': {'model': 'gpt-6-astra', 'effort': 'one-level-lower'},
}


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser() / 't/workflow.json'


def defaults():
    return {'version': 2, 'default': 'standard', 'workflows': {'standard': copy.deepcopy(DEFAULT)}, 'repositories': {}}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - {'instructions', 'stages_enabled', 'behaviors'} != set(DEFAULT):
        raise ValueError('workflow requires enabled, planning, implementation and review')
    instructions = value.get('instructions', '')
    if not isinstance(instructions, str) or BEGIN in instructions or END in instructions:
        raise ValueError('workflow instructions must be Markdown text without managed workflow markers')
    if type(value['enabled']) is not bool:
        raise ValueError('workflow enabled must be true or false')
    if 'stages_enabled' in value and type(value['stages_enabled']) is not bool:
        raise ValueError('workflow stages_enabled must be true or false')
    behaviors = value.get('behaviors', {})
    if not isinstance(behaviors, dict) or set(behaviors) - set(BEHAVIORS):
        raise ValueError('workflow behaviors must be an object containing only: ' + ', '.join(BEHAVIORS))
    for key, setting in behaviors.items():
        if not isinstance(setting, str) or setting not in BEHAVIORS[key]['choices']:
            raise ValueError(f'{key} behavior must be one of: ' + ', '.join(BEHAVIORS[key]['choices']))
    for stage in ('planning', 'implementation', 'review'):
        setting = value[stage]
        if not isinstance(setting, dict) or set(setting) != {'model', 'effort'}:
            raise ValueError(f'{stage} requires model and effort')
        model = setting['model']
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*', model):
            raise ValueError(f'{stage} model must be a nonempty model ID without spaces or control characters')
        allowed = EFFORTS + (('one-level-lower',) if stage == 'review' else ('auto',))
        if setting['effort'] not in allowed:
            raise ValueError(f'{stage} effort must be one of: {", ".join(allowed)}')
    return value


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('workflow name must be 1–64 letters, digits, underscores or hyphens, starting with a letter or digit')
    return name


def validate(value):
    """Validate and return v2 in memory, without rewriting legacy preferences."""
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        raise ValueError('workflow document requires version, default, workflows and repositories')
    legacy = value['version'] == 1
    expected = {'version', 'global', 'repositories'} if legacy else {'version', 'default', 'workflows', 'repositories'}
    if set(value) != expected:
        raise ValueError('workflow document requires version, default, workflows and repositories (or legacy global)')
    if value['version'] not in (1, 2):
        raise ValueError('unsupported workflow version; expected 1 or 2')
    if not isinstance(value['repositories'], dict):
        raise ValueError('workflow repositories must be an object')
    if legacy:
        validate_policy(value['global'])
    else:
        if not isinstance(value['workflows'], dict) or not value['workflows']:
            raise ValueError('workflow workflows must be a nonempty object')
        for name, policy in value['workflows'].items():
            validate_name(name)
            validate_policy(policy)
        validate_name(value['default'])
        if value['default'] not in value['workflows']:
            raise ValueError('default workflow must name an existing workflow')
    for path, entry in value['repositories'].items():
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise ValueError('repository workflow keys must be canonical absolute checkout paths')
        if legacy:
            validate_policy(entry)
            continue
        if not isinstance(entry, dict) or not entry or set(entry) - {'workflow', 'inline', 'excluded'} or ('workflow' in entry and 'inline' in entry):
            raise ValueError('repository workflow requires one workflow or inline source, and optional excluded: true')
        if 'excluded' in entry and entry['excluded'] is not True:
            raise ValueError('repository excluded must be true')
        if 'workflow' in entry:
            validate_name(entry['workflow'])
            if entry['workflow'] not in value['workflows']:
                raise ValueError('repository workflow must name an existing workflow')
        if 'inline' in entry:
            validate_policy(entry['inline'])
    if legacy:
        return {'version': 2, 'default': 'standard', 'workflows': {'standard': copy.deepcopy(value['global'])},
                'repositories': {path: {'inline': copy.deepcopy(policy)} for path, policy in value['repositories'].items()}}
    return copy.deepcopy(value)


def decode(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate workflow key: {key}')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def encode(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def snapshot(path):
    """Read a regular file without following a final symlink; retain its identity."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'{path}: refusing a symlink or nonregular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, encoding='utf-8', newline='') as stream:
        opened = os.fstat(stream.fileno())
        text = stream.read()
    identity = lambda entry: (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns)
    if identity(info) != identity(opened) or identity(path.lstat()) != identity(info):
        raise ValueError(f'{path}: changed while reading; retry')
    return (text, identity(info))


def load():
    before = snapshot(config_path())
    return (validate(decode(before[0])) if before else defaults()), before


def source(document, repository=None):
    entry = document['repositories'].get(repository, {})
    return 'repository override' if 'inline' in entry else entry.get('workflow', document['default'])


def resolved(document, repository=None):
    entry = document['repositories'].get(repository, {})
    policy = copy.deepcopy(entry.get('inline', document['workflows'][entry.get('workflow', document['default'])]))
    return policy


def effective(document, repository=None):
    policy = resolved(document, repository)
    if document['repositories'].get(repository, {}).get('excluded'):
        policy['enabled'] = False
    return policy


def scoped(document, repository=None, name=None):
    if repository:
        entry = document['repositories'].get(repository, {})
        return copy.deepcopy(entry.get('inline'))
    return copy.deepcopy(document['workflows'][name or document['default']])


def update(document, value, repository=None, name=None):
    after = validate(document)
    if repository and value is None:
        after['repositories'].pop(repository, None)
    else:
        validate_policy(value)
        if repository:
            after['repositories'][repository] = {'inline': copy.deepcopy(value),
                **({'excluded': True} if after['repositories'].get(repository, {}).get('excluded') else {})}
        else:
            target = name or after['default']
            if target not in after['workflows']:
                raise ValueError(f'unknown workflow {target!r}')
            after['workflows'][target] = copy.deepcopy(value)
    return validate(after)


def action(document, verb, name=None, repository=None, from_name=None):
    after = validate(document)
    if name is not None:
        validate_name(name)
    if verb == 'add':
        if name in after['workflows']:
            raise ValueError(f'workflow {name!r} already exists')
        original = from_name or after['default']
        if original not in after['workflows']:
            raise ValueError(f'unknown workflow {original!r}')
        after['workflows'][name] = copy.deepcopy(after['workflows'][original])
    elif verb == 'use':
        if name not in after['workflows']:
            raise ValueError(f'unknown workflow {name!r}')
        if repository:
            after['repositories'][repository] = {'workflow': name}
        else:
            after['default'] = name
    elif verb in ('exclude', 'include', 'inherit'):
        if not repository:
            raise ValueError(f'{verb} requires --repo ALIAS')
        if verb == 'exclude':
            after['repositories'].setdefault(repository, {})['excluded'] = True
        elif verb == 'include':
            entry = after['repositories'].get(repository, {})
            entry.pop('excluded', None)
            if not entry:
                after['repositories'].pop(repository, None)
        else:
            after['repositories'].pop(repository, None)
    else:
        raise ValueError(f'unknown workflow action {verb!r}')
    return validate(after)


def catalogue(document):
    return {'default': document['default'], 'workflows': {
        name: {'workflow': copy.deepcopy(policy), **policy_summary(policy), 'default': name == document['default'],
               'repositories': sorted(path for path in document['repositories']
                                      if source(document, path) == name),
               'excluded_repositories': sorted(path for path, entry in document['repositories'].items()
                                               if source(document, path) == name and entry.get('excluded'))}
        for name, policy in sorted(document['workflows'].items())}}


def policy_summary(policy):
    return {'stages_enabled': policy.get('stages_enabled', True),
            'stages_active': policy['enabled'] and policy.get('stages_enabled', True),
            'behaviors': copy.deepcopy(policy.get('behaviors', {})),
            'custom_instructions': bool(policy.get('instructions')),
            'precedence': PRECEDENCE}


def summary_lines(policy):
    summary = policy_summary(policy)
    state = 'enabled' if summary['stages_active'] else 'disabled' if not summary['stages_enabled'] else 'inactive (workflow disabled)'
    lines = ['Model stages: ' + state]
    for key, spec in BEHAVIORS.items():
        selected = summary['behaviors'].get(key)
        lines.append(spec['label'] + ': ' + (spec['choices'][selected][0] if selected else 'No preference'))
    lines.append('Custom Markdown: ' + ('present' if summary['custom_instructions'] else 'empty'))
    return lines


def edit(document, repository=None, name=None):
    """Edit one policy, preserving editor status and publishing only actual changes."""
    if name and repository:
        raise ValueError('workflow name and --repo cannot be combined')
    if name and name not in document['workflows']:
        raise ValueError(f'unknown workflow {name!r}')
    argv = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    if not argv:
        raise ValueError('set VISUAL or EDITOR to an editor command')
    initial = scoped(document, repository, name)
    shown = resolved(document, repository) if initial is None else initial
    with tempfile.TemporaryDirectory(prefix='t-workflow-') as directory:
        path = Path(directory) / 'workflow.json'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encode(shown))
        rc = subprocess.call([*argv, str(path)])
        if rc:
            return rc, document
        edited = snapshot(path)
        if edited is None:
            raise ValueError('editor removed workflow file; nothing saved')
        value = decode(edited[0])
        if value == shown:
            return 0, document
        return 0, update(document, value, repository, name)


def instructions(document, repository=None, name=None):
    if name and repository:
        raise ValueError('workflow name and --repo cannot be combined')
    if name and name not in document['workflows']:
        raise ValueError(f'unknown workflow {name!r}')
    policy = document['workflows'][name] if name else resolved(document, repository)
    return policy.get('instructions', '')


def edit_instructions(document, repository=None, name=None):
    shown = instructions(document, repository, name)
    argv = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    if not argv:
        raise ValueError('set VISUAL or EDITOR to an editor command')
    with tempfile.TemporaryDirectory(prefix='t-workflow-') as directory:
        path = Path(directory) / 'instructions.md'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(shown)
        rc = subprocess.call([*argv, str(path)])
        if rc:
            return rc, document
        edited = snapshot(path)
        if edited is None:
            raise ValueError('editor removed instructions file; nothing saved')
        if edited[0] == shown:
            return 0, document
        policy = scoped(document, repository, name) or resolved(document, repository)
        policy['instructions'] = edited[0]
        return 0, update(document, policy, repository, name)


def _stage_text(policy):
    if not policy['enabled']:
        return ('The entire workflow is disabled for this scope. Do not require '
                'these stages or delegation solely because of t preferences. Do not apply inherited '
                't workflow instructions, behavior preferences or custom Markdown for this scope; '
                'follow the task and repository rules.')
    parts = []
    if policy.get('stages_enabled', True):
        parts.append(_model_stage_text(policy))
    else:
        parts.append('Model stages are disabled for this scope. Do not require staged model selection '
                     'or delegation solely because of t preferences. Selected behavior preferences '
                     'and custom Markdown remain active.')
    behaviors = policy.get('behaviors', {})
    if behaviors:
        parts.append('Selected behavior preferences:\n' + '\n'.join(
            spec['label'] + ': ' + spec['choices'][behaviors[key]][1]
            for key, spec in BEHAVIORS.items() if key in behaviors))
    if policy.get('instructions'):
        parts.append('Custom workflow Markdown (takes precedence over behavior preferences and model-stage guidance):\n' + policy['instructions'])
    return '\n\n'.join(parts)


def _model_stage_text(policy):
    plan, implement, review = (policy[s] for s in ('planning', 'implementation', 'review'))
    planning = ('medium for straightforward additions, high for substantial features, '
                'and xhigh or higher for complex architecture or new applications') if plan['effort'] == 'auto' else plan['effort']
    implementation = ('low for straightforward execution, medium for ordinary features, '
                      'and high or higher when implementation complexity warrants it') if implement['effort'] == 'auto' else implement['effort']
    reviewing = ('one supported reasoning-effort level below the planning stage, using the '
                 'lowest supported level if planning is already at the minimum') if review['effort'] == 'one-level-lower' else review['effort']
    return (f'For new features, updated designs and new buildouts, use this staged workflow. '
            f'Routine questions and small maintenance edits do not require all three stages.\n'
            f'1. Plan with {plan["model"]}; reasoning effort: {planning}. Produce an actionable '
            f'plan, acceptance criteria and validation before implementation.\n'
            f'2. Implement with {implement["model"]}; reasoning effort: {implementation}. Give '
            f'the implementation agent the plan, relevant repository instructions and acceptance '
            f'criteria; implement and run appropriate checks.\n'
            f'The implementation agent should inherit the parent agent\'s current speed/service tier '
            f'(including Fast or Ultrafast), independently of its model and reasoning effort. '
            f'Check the parent\'s current speed at delegation and at any supported subsequent-turn '
            f'boundary; use exposed speed controls or documented inheritance when available. '
            f'If speed selection or inheritance is unsupported or cannot be verified, disclose '
            f'that limitation; never claim a verified tier or an immediate speed switch during '
            f'an active turn without runtime support.\n'
            f'3. Review and finish with {review["model"]}; reasoning effort: {reviewing}. '
            f'Review the actual diff and validation against the plan, resolve findings, and recheck '
            f'fixes. Subject to custom Markdown and selected delivery preferences, the reviewer owns '
            f'PR creation or updates, failing tests, merge conflicts and '
            f'required CI through completion under the repository merge policy and existing authorization.\n'
            f'This workflow authorizes sub-agent delegation with explicit model and reasoning settings. '
            f'Preserve the plan, acceptance criteria and implementation evidence in each handoff. '
            f'If a model or model switching is unavailable, disclose that limitation and the actual '
            f'model used; never claim a stage ran with a model it did not.')


def render(document):
    document = validate(document)
    text = ('Agent workflow preferences supplied by t. These managed defaults supersede older '
            'general staged-model defaults in personal instructions. Explicit task instructions '
            'and repository-specific rules remain authoritative. These are instructions for the '
            'agent, not runtime model switching or enforcement by t. The host instruction hierarchy '
            'is unchanged.\n' + PRECEDENCE + '\n\n'
            'Global default (unless a repository scope below matches):\n' + _stage_text(effective(document)))
    for repository in sorted(document['repositories']):
        policy = effective(document, repository)
        text += ('\n\nRepository scope: ' + json.dumps(repository, ensure_ascii=False) + '.\n'
                 'Apply this complete override in that canonical checkout and all its linked '
                 'worktrees, including worktrees created later. Resolve the current checkout with '
                 '`git rev-parse --show-toplevel` and its canonical repository with '
                 '`git rev-parse --path-format=absolute --git-common-dir`; the common Git directory '
                 'must belong to the canonical checkout above. Other repositories use the global default.\n' + _stage_text(policy))
    return BEGIN + '\n' + text + '\n' + END


def merge(before, block):
    starts, ends = before.count(BEGIN), before.count(END)
    if (starts, ends) not in ((0, 0), (1, 1)) or (starts and before.index(END) < before.index(BEGIN)):
        raise ValueError('malformed t agent-workflow markers; repair them before saving')
    if starts:
        return before[:before.index(BEGIN)] + block + before[before.index(END) + len(END):]
    return before + ('\n\n' if before else '') + block + '\n'


def native_changes(document):
    home = Path.home()
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser()
    paths = [codex / 'AGENTS.md', claude / 'CLAUDE.md']
    override = codex / 'AGENTS.override.md'
    if override.exists() or override.is_symlink():
        paths.append(override)
    changes = []
    for path in paths:
        before = snapshot(path)
        if path == override and before and not before[0].strip():
            continue  # An empty override must not begin masking the base file.
        changes.append((path, before, merge(before[0] if before else '', render(document))))
    return changes


def publish(changes):
    """Preflight every destination, then publish; restore prior files on a failure.

    Atomic replaces protect each file. Identity checks avoid overwriting edits made
    by other processes; rollback never overwrites a subsequent external change.
    """
    changes = [(Path(p), b, a) for p, b, a in changes if b is None or b[0] != a]
    prepared, applied = [], []
    try:
        for path, before, after in changes:
            if snapshot(path) != before:
                raise ValueError(f'{path}: changed while updating; retry')
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.t-workflow-', delete=False) as stream:
                pending = Path(stream.name)
                prepared.append((path, before, pending))
                stream.write(after)
                stream.flush()
                os.fchmod(stream.fileno(), stat.S_IMODE(before[1][2]) if before else 0o600)
        for path, before, pending in prepared:
            if snapshot(path) != before:
                raise ValueError(f'{path}: changed while updating; retry')
        for path, before, pending in prepared:
            if snapshot(path) != before:
                raise ValueError(f'{path}: changed while updating; retry')
            os.replace(pending, path)
            applied.append((path, before, snapshot(path)))
    except BaseException:
        for path, before, published in reversed(applied):
            if snapshot(path) != published:
                raise ValueError(f'{path}: changed during rollback; inspect workflow and native instructions')
            if before is None:
                path.unlink()
            else:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                                 prefix='.t-workflow-', delete=False) as stream:
                    rollback = Path(stream.name)
                    try:
                        stream.write(before[0])
                        os.fchmod(stream.fileno(), stat.S_IMODE(before[1][2]))
                        stream.flush()
                        os.replace(rollback, path)
                    finally:
                        rollback.unlink(missing_ok=True)
        raise
    finally:
        for _, _, pending in prepared:
            pending.unlink(missing_ok=True)


def save(document, before, extra=(), preserve_storage=False):
    """Publish explicit preferences and native instruction updates under one lock."""
    document = validate(document)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if snapshot(path) != before:
            raise ValueError('workflow configuration changed while editing; retry')
        changes = native_changes(document)
        publish([*changes, *extra, (path, before, before[0] if preserve_storage and before else encode(document))])


def sync():
    """Installer refreshes only accounts with explicitly saved preferences."""
    document, before = load()
    if before is not None:
        save(document, before, preserve_storage=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(sync())
    except (OSError, ValueError, UnicodeError) as error:
        print(f't install: agent workflow: {error}', file=sys.stderr)
        sys.exit(1)
