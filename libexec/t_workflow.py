"""Account-local staged agent preferences and managed native instructions.

Saved settings select main-session launch defaults; stage delegation remains guidance.
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
ISSUE_BEGIN = '<!-- t public-issues: begin -->'
ISSUE_END = '<!-- t public-issues: end -->'
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
        'before_implementation': ('Design preview first', 'For design and UI work, prepare and show a local design preview before substantial implementation. Wait for user feedback before continuing implementation. Report unavailable preview tools or verification limitations accurately.'),
        'on_request': ('Only when requested', 'Run a local preview only when explicitly requested, unless higher-priority instructions require it.'),
    }},
    'validation': {'label': 'Validation', 'choices': {
        'relevant': ('Relevant checks', 'Run the relevant documented checks for the change and report their results.'),
        'full': ('Full documented checks', 'Run the full documented validation suite and report its results and any unavailable checks.'),
    }},
    'delivery': {'label': 'Delivery', 'choices': {
        'review': ('Leave for review', 'Leave changes uncommitted for user review unless explicit task instructions or repository rules require otherwise.'),
        'pull_request': ('Open PR when ready; do not merge', 'When implementation and required checks are ready, commit, push and open a pull request without an extra confirmation. Do not merge or enable auto-merge from this preference.'),
        'draft_pull_request': ('Open a draft PR', 'When implementation and relevant checks are ready, commit, push and open a draft pull request. Leave it as a draft; do not mark it ready, merge or enable auto-merge from this preference.'),
        'ask_before_pr': ('Wait for approval before PR', 'Complete implementation and relevant checks, present a concrete reviewable result, then wait for explicit user approval before pushing or creating a pull request.'),
        'local_review': ('Install / preview locally first', 'Complete implementation and relevant checks, then use the documented local install or preview path appropriate to the project so the user can try the result. Respect existing authorization for installation and report unavailable tools accurately. Present how to try it and wait for explicit user feedback and approval before pushing or creating a pull request.'),
        'auto_merge': ('Auto-merge when authorized', 'Commit, push and open a pull request under repository merge policy and required checks. When authorized and supported, enable auto-merge and monitor it through completion; report any blocker accurately.'),
    }},
    'pr_open': {'label': 'Open pull request', 'choices': {
        'automatic': ('When ready', 'When the work is ready under the selected preview and validation preferences, commit, push and open a pull request without another confirmation. Honor any required preview approval and repository or task instructions.'),
        'on_request': ('When I ask', 'Prepare a concrete reviewable result, then wait for an explicit user request or approval before pushing or opening a pull request, subject to repository and task instructions.'),
    }},
    'pr_state': {'label': 'Pull request state', 'choices': {
        'ready': ('Ready for review', 'Create pull requests ready for review. Required CI still runs and all required checks and merge gates remain in effect.'),
        'draft': ('Draft', 'Open pull requests as drafts and wait for user approval before marking them ready for review.'),
    }},
    'pr_merge': {'label': 'Merge pull request', 'choices': {
        'automatic': ('After checks pass', 'Once a pull request is ready for review, enable automatic merging without another confirmation under repository policy and monitor required checks through completion. Do not promote drafts automatically. Never bypass required checks or branch protections.'),
        'on_request': ('When I ask', 'Wait for an explicit user request or approval before merging or enabling automatic merging, subject to repository and task instructions.'),
    }},
    'local_preview': {'label': 'Local preview', 'choices': {
        'when_ready': ('When ready', 'When the work is ready to try, prepare its supported local preview: a development install or command for a CLI, or a local HTML or application browser preview for a frontend. Explain how the user can access it and report unavailable preview tools accurately.'),
        'before_pr': ('Before PR, with approval', 'Prepare the supported local preview: a development install or command for a CLI, or a local HTML or application browser preview for a frontend. Explain how to try it, then wait for explicit user feedback and approval before pushing or opening a pull request. Honor repository and task instructions.'),
        'on_request': ('Only when requested', 'Prepare a local preview only when the user requests it, unless repository or task instructions require one.'),
    }},
    'cli_preview': {'label': 'Command-line preview', 'choices': {
        'install': ('Install locally', 'For command-line tool previews, use the supported development installation or relink mechanism and clearly identify the development version. Honor repository installation and isolation rules.'),
        'run': ('Run from the repo', 'For command-line tool previews, run the repository-supported development command directly and show the user how to invoke it.'),
    }},
    'frontend_design': {'label': 'Frontend design', 'choices': {
        'standalone_html': ('Separate HTML prototype', 'For frontend design, create an interactive standalone HTML prototype outside the application so the user can preview the design before app implementation.'),
        'in_app': ('In the app', 'For frontend design, implement and preview the design within the actual application using its existing components and development setup.'),
    }},
    'design_approval': {'label': 'Design approval', 'choices': {
        'required': ('Wait for approval', 'For design and UI work, prepare and show a design preview before substantial implementation, then wait for user feedback and approval before continuing. Report unavailable preview tools accurately.'),
        'continue': ('Continue building', 'After preparing the design, continue implementation without an additional design approval step unless repository or task instructions require one.'),
    }},
    'frontend_preview': {'label': 'Frontend preview', 'choices': {
        'fast_reload': ('Live reload', 'For frontend previews, use the supported local development server and fast reload when available so design changes can be inspected promptly. Report unsupported preview capabilities accurately.'),
        'rebuild': ('Rebuild', 'For frontend previews, rebuild using the supported preview command after relevant changes and show the updated result.'),
    }},
    'test_timing': {'label': 'Run tests', 'choices': {
        'during_work': ('Local: during work', 'Run relevant local checks as implementation progresses and report results. Keep required checks and repository validation rules intact.'),
        'before_preview': ('Local: before preview', 'Run relevant local checks before exposing the local preview and report results. Keep required checks and repository validation rules intact.'),
        'before_pr': ('Local: before PR', 'Run relevant local checks before pushing or opening a pull request and report results. Keep required checks and repository validation rules intact.'),
        'pr_ci': ('PR CI', 'Use the pull request\'s required CI for validation rather than routine local test, lint, coverage or compilation suites. Keep required checks and branch protections intact; explicit task instructions and repository rules remain authoritative.'),
    }},
}


def effective_behaviors(behaviors):
    """Resolve legacy choices without changing the saved workflow or raw JSON."""
    result = {}
    preview = {
        'ui_changes': {'local_preview': 'when_ready'},
        'before_implementation': {'design_approval': 'required'},
        'on_request': {'local_preview': 'on_request'},
    }
    delivery = {
        'pull_request': {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'on_request'},
        'draft_pull_request': {'pr_open': 'automatic', 'pr_state': 'draft', 'pr_merge': 'on_request'},
        'auto_merge': {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'automatic'},
        'review': {'pr_open': 'on_request', 'pr_merge': 'on_request'},
        'ask_before_pr': {'pr_open': 'on_request', 'pr_merge': 'on_request'},
        'local_review': {'pr_open': 'automatic', 'pr_merge': 'on_request', 'local_preview': 'before_pr'},
    }
    result.update(preview.get(behaviors.get('preview'), {}))
    result.update(delivery.get(behaviors.get('delivery'), {}))
    result.update({key: value for key, value in behaviors.items() if key not in ('preview', 'delivery')})
    if behaviors.get('delivery') == 'review' and result.get('pr_open') != 'automatic':
        result['delivery'] = 'review'  # Its leave-uncommitted rule is more specific than waiting for a PR.
    if 'test_timing' in result:
        result.pop('validation', None)
    return result


def update_behavior(policy, key, value):
    """Materialize effective choices only when the user edits a preference."""
    if key not in BEHAVIORS or (value is not None and not isinstance(value, str)) or (value and value not in BEHAVIORS[key]['choices']):
        raise ValueError('unknown workflow behavior choice')
    after = copy.deepcopy(policy)
    behaviors = effective_behaviors(after.get('behaviors', {}))
    if value:
        behaviors[key] = value
    else:
        behaviors.pop(key, None)
        if key == 'pr_open':
            behaviors.pop('delivery', None)
    if key == 'test_timing' and value:
        behaviors.pop('validation', None)
    behaviors = effective_behaviors(behaviors)
    if behaviors:
        after['behaviors'] = behaviors
    else:
        after.pop('behaviors', None)
    validate_policy(after)
    return after


PRECEDENCE = ('Explicit task instructions > repository rules > custom Markdown > behavior preferences > model-stage guidance. '
              'The selected repository workflow replaces the global default; exclusion suppresses all workflow instructions.')
DEFAULT = {
    'enabled': True,
    'planning': {'model': 'gpt-6-astra', 'effort': 'auto'},
    'implementation': {'model': 'gpt-6.1-sol', 'effort': 'auto'},
    'review': {'model': 'gpt-6-astra', 'effort': 'one-level-lower'},
}
RUNNER_DEFAULT = {'default_targets': [], 'timeout_seconds': 900,
                  'queue_timeout_seconds': 300, 'artifacts': [], 'targets': {}}
RUNNER_LABELS = {'ubuntu-24.04': ('linux', 'x64'), 'ubuntu-22.04': ('linux', 'x64'),
                 'macos-15': ('macos', 'arm64'), 'macos-15-intel': ('macos', 'x64'),
                 'macos-14': ('macos', 'arm64')}


def validate_runner_path(path):
    if (not isinstance(path, str) or not path or len(path) > 1024 or path.startswith('/') or
            '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path) or
            any(part in ('', '.', '..', '.git') for part in path.split('/'))):
        raise ValueError('runner artifact paths must be relative paths without traversal')
    return path


def validate_runners(value):
    if not isinstance(value, dict) or set(value) != set(RUNNER_DEFAULT):
        raise ValueError('runners requires default_targets, timeout_seconds, queue_timeout_seconds, artifacts and targets')
    for field, maximum in (('timeout_seconds', 21600), ('queue_timeout_seconds', 3600)):
        if type(value[field]) is not int or not 1 <= value[field] <= maximum:
            raise ValueError(f'runner {field} must be an integer between 1 and {maximum}')
    targets = value['targets']
    if not isinstance(targets, dict) or len(targets) > 16:
        raise ValueError('runner targets must be an object with at most 16 entries')
    for name, target in targets.items():
        validate_name(name)
        if not isinstance(target, dict):
            raise ValueError('runner target must be an object')
        provider = target.get('provider')
        required = {'provider', 'os', 'arch', 'capabilities'}
        required |= {'repository', 'label'} if provider == 'github' else {'host'} if provider == 'ssh' else set()
        if provider not in ('github', 'ssh') or set(target) != required:
            raise ValueError('runner target requires provider github or ssh and its connection fields')
        if target['os'] not in ('linux', 'macos') or target['arch'] not in ('x64', 'arm64'):
            raise ValueError('runner OS/architecture must be linux or macos and x64 or arm64; Windows is not yet supported')
        if provider == 'github':
            repository = target['repository']
            if not isinstance(repository, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
                raise ValueError('GitHub runner repository must be OWNER/REPO')
            if not isinstance(target['label'], str) or RUNNER_LABELS.get(target['label']) != (target['os'], target['arch']):
                raise ValueError('GitHub runner label must match a supported OS/architecture')
        elif not isinstance(target['host'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}', target['host']):
            raise ValueError('SSH runner host must be a host alias or user@host without options')
        capabilities = target['capabilities']
        if not isinstance(capabilities, list) or len(capabilities) > 32:
            raise ValueError('runner capabilities must be a list with at most 32 entries')
        for capability in capabilities:
            validate_name(capability)
        if len(set(capabilities)) != len(capabilities):
            raise ValueError('runner capabilities must be unique')
    names = value['default_targets']
    if not isinstance(names, list) or any(not isinstance(name, str) or name not in targets for name in names) or len(set(names)) != len(names):
        raise ValueError('runner default_targets must name unique configured targets')
    paths = value['artifacts']
    if not isinstance(paths, list) or len(paths) > 32:
        raise ValueError('runner artifacts must be a list with at most 32 paths')
    for path in paths:
        validate_runner_path(path)
    if len(set(paths)) != len(paths):
        raise ValueError('runner artifact paths must be unique')
    return value


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser() / 't/workflow.json'


def issue_policy():
    return Path(__file__).with_name('t_issue_policy.txt').read_text(encoding='utf-8').strip()


def _seed_instructions(document, texts):
    policies = [*document['workflows'].values(),
                *(entry['inline'] for entry in document['repositories'].values() if 'inline' in entry)]
    for policy in policies:
        for text in texts:
            existing = policy.get('instructions', '')
            if text and text not in existing:
                policy['instructions'] = existing + ('\n\n' if existing else '') + text
    return document


def defaults():
    return _seed_instructions({'version': 3, 'default': 'standard',
                              'workflows': {'standard': copy.deepcopy(DEFAULT)}, 'repositories': {}}, [issue_policy()])


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - {'instructions', 'stages_enabled', 'behaviors', 'runners', 'session_defaults'} != set(DEFAULT):
        raise ValueError('workflow requires enabled, planning, implementation and review')
    instructions = value.get('instructions', '')
    if not isinstance(instructions, str) or any(marker in instructions for marker in (BEGIN, END, ISSUE_BEGIN, ISSUE_END)):
        raise ValueError('workflow instructions must be Markdown text without managed workflow markers')
    if type(value['enabled']) is not bool:
        raise ValueError('workflow enabled must be true or false')
    if 'runners' in value:
        validate_runners(value['runners'])
    if 'stages_enabled' in value and type(value['stages_enabled']) is not bool:
        raise ValueError('workflow stages_enabled must be true or false')
    session_defaults = value.get('session_defaults', {})
    if not isinstance(session_defaults, dict) or set(session_defaults) - {'codex', 'claude'}:
        raise ValueError('session_defaults supports codex and claude only')
    for settings in session_defaults.values():
        if not isinstance(settings, dict) or set(settings) - {'model', 'effort'}:
            raise ValueError('session_defaults requires model and/or effort')
        model, effort = settings.get('model', ''), settings.get('effort', '')
        if not isinstance(model, str) or (model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]*', model)):
            raise ValueError('session default model must be a model ID without spaces')
        if not isinstance(effort, str) or (effort and effort not in EFFORTS):
            raise ValueError('session default effort must be an explicit reasoning level')
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


def validate(value, *, migrate=True):
    """Validate and migrate in memory; read-only commands never rewrite preferences."""
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        raise ValueError('workflow document requires version, default, workflows and repositories')
    legacy = value['version'] == 1
    expected = {'version', 'global', 'repositories'} if legacy else {'version', 'default', 'workflows', 'repositories'}
    if set(value) != expected:
        raise ValueError('workflow document requires version, default, workflows and repositories (or legacy global)')
    if value['version'] not in (1, 2, 3):
        raise ValueError('unsupported workflow version; expected 1, 2 or 3')
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
        result = {'version': 3, 'default': 'standard', 'workflows': {'standard': copy.deepcopy(value['global'])},
                  'repositories': {path: {'inline': copy.deepcopy(policy)} for path, policy in value['repositories'].items()}}
    else:
        result = copy.deepcopy(value)
        result['version'] = 3
    return _seed_instructions(result, [issue_policy()]) if migrate and value['version'] < 3 else result


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


class WorkflowDocument(dict):
    """JSON data with nonserialized legacy-file snapshots for safe first edits."""


def load():
    before = snapshot(config_path())
    document = WorkflowDocument(validate(decode(before[0])) if before else defaults())
    document.legacy_snapshots = {}
    # Import customized legacy policy text before the installer removes its old
    # managed block. This read-only migration is persisted only by an explicit save.
    texts = []
    for path in native_paths():
        native = snapshot(path)
        if native:
            body = legacy_issue_body(native[0])
            if ISSUE_BEGIN in native[0]:
                document.legacy_snapshots[path] = native
            if body and body != issue_policy() and body not in texts:
                texts.append(body)
    return _seed_instructions(document, texts), before


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


def main_session_settings(policy, agent='codex'):
    """Scoped launch values; stage models belong only to Codex root sessions."""
    if not policy or not policy['enabled']:
        return {}
    if policy.get('stages_enabled', True):
        if agent != 'codex':
            return {}
        implementation = policy['implementation']
        return {'model': implementation['model'],
                'effort': 'medium' if implementation['effort'] == 'auto' else implementation['effort']}
    return {key: value for key, value in policy.get('session_defaults', {}).get(agent, {}).items() if value}


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
            'session_defaults': copy.deepcopy(policy.get('session_defaults', {})),
            'custom_instructions': bool(policy.get('instructions')),
            'precedence': PRECEDENCE}


def summary_lines(policy):
    summary = policy_summary(policy)
    behaviors = effective_behaviors(summary['behaviors'])
    state = 'enabled' if summary['stages_active'] else 'disabled' if not summary['stages_enabled'] else 'inactive (workflow disabled)'
    lines = ['Model stages: ' + state]
    if not summary['stages_enabled']:
        for agent, settings in summary['session_defaults'].items():
            lines.append(agent.title() + ' session default: ' + (settings.get('model') or 'agent default') +
                         '; reasoning: ' + (settings.get('effort') or 'default for this model'))
    if behaviors:
        for key, spec in BEHAVIORS.items():
            if key in behaviors:
                lines.append(spec['label'] + ': ' + spec['choices'][behaviors[key]][0])
    else:
        lines.append('PRs, previews & tests: no preferences set')
    lines.append('Custom Markdown: ' + ('present' if summary['custom_instructions'] else 'empty'))
    lines.append('Issue-before-code rule: ' + ('included in editable Markdown' if issue_policy() in policy.get('instructions', '') else 'customized or not included'))
    lines.append('Read/edit prose: t workflow instructions show / edit')
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


def runners(document, repository=None, name=None):
    """Inspect stored runner settings without changing scope or writing defaults."""
    if name and repository:
        raise ValueError('workflow name and --repo cannot be combined')
    if name and name not in document['workflows']:
        raise ValueError(f'unknown workflow {name!r}')
    policy = document['workflows'][name] if name else resolved(document, repository)
    return copy.deepcopy(policy.get('runners', RUNNER_DEFAULT))


def runners_summary(policy):
    value = policy.get('runners', RUNNER_DEFAULT)
    return (f"{len(value['targets'])} targets · defaults: " + ', '.join(value['default_targets'])
            if value['targets'] else 'unconfigured')


def edit_runners(document, repository=None, name=None):
    shown = runners(document, repository, name)
    argv = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    if not argv:
        raise ValueError('set VISUAL or EDITOR to an editor command')
    with tempfile.TemporaryDirectory(prefix='t-runners-') as directory:
        path = Path(directory) / 'runners.json'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encode(shown))
        rc = subprocess.call([*argv, str(path)])
        if rc:
            return rc, document
        edited = snapshot(path)
        if edited is None:
            raise ValueError('editor removed runners file; nothing saved')
        value = decode(edited[0])
        validate_runners(value)
        if value == shown:
            return 0, document
        policy = scoped(document, repository, name) or resolved(document, repository)
        policy['runners'] = value
        return 0, update(document, policy, repository, name)


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
        for agent, settings in policy.get('session_defaults', {}).items():
            chosen = ', '.join(key + ': ' + value for key, value in settings.items() if value)
            if chosen:
                parts.append(f'{agent.title()} session launch defaults: {chosen}. '
                             't applies these defaults to new CLI sessions and supported resumes in this scope. '
                             'They do not change an active turn or require switching models during a task.')
    behaviors = effective_behaviors(policy.get('behaviors', {}))
    if behaviors:
        parts.append('Selected behavior preferences:\n' + '\n'.join(
            spec['label'] + ': ' + spec['choices'][behaviors[key]][1]
            for key, spec in BEHAVIORS.items() if key in behaviors))
    if policy.get('runners', {}).get('targets'):
        parts.append('Test runners are configured. Use `t runners list` '
                     '(with `--repo ALIAS` for repository settings) to inspect targets. '
                     'Use `t runners runs start -- COMMAND ...` to test one immutable snapshot '
                     'of current tracked worktree edits without committing or pushing source; include intended '
                     'untracked inputs with `--include PATH`. Select targets with `--target NAME`, '
                     '`--os linux|macos` or `--capability NAME`. Inspect durable results with '
                     '`t runners runs status ID` and `t runners runs logs ID`. '
                     'Native desktop tests remain opt-in.')
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
    return (f'Keep the main conversation on {implement["model"]} for routine discussion, coding and '
            f'follow-ups; default reasoning effort: '
            f'{main_session_settings(policy)["effort"]}. '
            f'For new features, updated designs and new buildouts, use these bounded stages. '
            f'Routine questions and small maintenance edits do not require all three stages.\n'
            f'1. Plan with a bounded {plan["model"]} sub-agent; reasoning effort: {planning}. '
            f'Choose a concrete supported effort before delegation. Produce an actionable '
            f'plan, acceptance criteria and validation before implementation.\n'
            f'2. Implement in the main conversation with {implement["model"]}; reasoning effort: '
            f'{implementation}. Use the plan, relevant repository instructions and acceptance '
            f'criteria; implement and run appropriate checks.\n'
            f'3. Review with a bounded {review["model"]} sub-agent; reasoning effort: {reviewing}. '
            f'Choose a concrete supported effort before delegation. Review the actual diff and '
            f'validation against the plan and return findings. The main conversation owns fixes, '
            f'rechecks, PR updates, merge conflicts and required CI through completion under '
            f'the repository merge policy, selected delivery preferences and existing authorization.\n'
            f'This workflow authorizes sub-agent delegation with explicit model and concrete '
            f'reasoning settings. Do not blindly inherit the parent model. When model overrides '
            f'require fresh context, use it with a concise handoff containing the plan, relevant '
            f'repository instructions, acceptance criteria and implementation evidence. '
            f'Each sub-agent should inherit the parent agent\'s current speed/service tier '
            f'(including Fast or Ultrafast), independently of its model and reasoning effort. '
            f'Check the parent\'s current speed at delegation and at any supported subsequent-turn boundary; '
            f'use exposed speed controls or documented inheritance when available. '
            f'If speed selection or inheritance is unsupported or cannot be verified, disclose '
            f'that limitation; never claim a verified tier or an immediate speed switch during '
            f'an active turn without runtime support. '
            f'If a model or model switching is unavailable, disclose that limitation and the actual '
            f'model used; never claim a stage ran with a model it did not. '
            f'These instructions do not enforce model selection or switch an active conversation.')


def render(document):
    document = validate(document)
    text = ('Agent workflow preferences supplied by t. These managed defaults supersede older '
            'general staged-model defaults in personal instructions. Explicit task instructions '
            'and repository-specific rules remain authoritative. These are instructions for the '
            'agent; saved staged workflows set Codex main launch/resume defaults, while stage delegation '
            'remains guidance and does not switch active conversations. The host instruction hierarchy is unchanged.\n' + PRECEDENCE + '\n\n'
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


def legacy_issue_body(text):
    starts, ends = text.count(ISSUE_BEGIN), text.count(ISSUE_END)
    if (starts, ends) not in ((0, 0), (1, 1)) or (starts and text.index(ISSUE_END) < text.index(ISSUE_BEGIN)):
        raise ValueError('malformed t public-issues markers; repair them before saving')
    return text[text.index(ISSUE_BEGIN) + len(ISSUE_BEGIN):text.index(ISSUE_END)].strip() if starts else ''


def merge(before, block, legacy_instructions="", replace_legacy=False):
    body = legacy_issue_body(before)
    if ISSUE_BEGIN in before:
        # A customized block not imported by this caller must never be lost.
        preserved = body if not replace_legacy and body and body != issue_policy() and body not in block and body not in legacy_instructions else ''
        before = before[:before.index(ISSUE_BEGIN)] + preserved + before[before.index(ISSUE_END) + len(ISSUE_END):]
    starts, ends = before.count(BEGIN), before.count(END)
    if (starts, ends) not in ((0, 0), (1, 1)) or (starts and before.index(END) < before.index(BEGIN)):
        raise ValueError('malformed t agent-workflow markers; repair them before saving')
    if starts:
        return before[:before.index(BEGIN)] + block + before[before.index(END) + len(END):]
    return before + ('\n\n' if before else '') + block + '\n'


def native_paths():
    home = Path.home()
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser()
    paths = [codex / 'AGENTS.md', claude / 'CLAUDE.md']
    override = codex / 'AGENTS.override.md'
    if override.exists() or override.is_symlink():
        paths.append(override)
    return paths


def native_changes(document):
    paths = native_paths()
    override = paths[0].with_name('AGENTS.override.md')
    policies = [*document['workflows'].values(),
                *(entry['inline'] for entry in document['repositories'].values() if 'inline' in entry)]
    legacy_instructions = '\n'.join(policy.get('instructions', '') for policy in policies)
    changes = []
    legacy_snapshots = getattr(document, 'legacy_snapshots', {})
    for path in paths:
        before = snapshot(path)
        if path in legacy_snapshots and before != legacy_snapshots[path]:
            raise ValueError(f'{path}: native instructions changed while editing; retry')
        if path == override and before and not before[0].strip():
            continue  # An empty override must not begin masking the base file.
        changes.append((path, before, merge(before[0] if before else '', render(document), legacy_instructions, path in legacy_snapshots)))
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


def propagation():
    return __import__('runpy').run_path(str(Path(__file__).with_name('t_workflow_sync.py')))


def save(document, before, extra=(), preserve_storage=False, propagation_context=None, propagate=True, main_settings=None, refresh_source=False):
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
        publication = [*changes, *extra, (path, before, before[0] if preserve_storage and before else encode(document))]
        reconcile = propagate and not preserve_storage and (before is None or decode(before[0]) != document or bool(main_settings) or refresh_source)
        helper = propagation() if reconcile else None
        if helper:
            # Intent and preferences publish together; journal failures roll back
            # the native files too. Runtime work begins only after both locks exit.
            with helper['locked']():
                previous = validate(decode(before[0]), migrate=False) if before else defaults()
                state = helper['desired'](previous, document, settings=main_settings, context=propagation_context)
                journal = helper['state_path']()
                publish([*publication, (journal, snapshot(journal), encode(state))])
        else:
            publish(publication)
    if helper:
        try:
            helper['retry']()
        except (OSError, ValueError):
            print('t workflow: preferences saved; propagation pending; automatic retry on session start', file=sys.stderr)


def sync():
    """Install editable defaults once, migrate old policies, and honor later edits."""
    document, before = load()
    unchanged = before is not None and decode(before[0]) == document
    save(document, before, preserve_storage=unchanged)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(sync())
    except (OSError, ValueError, UnicodeError) as error:
        print(f't install: agent workflow: {error}', file=sys.stderr)
        sys.exit(1)
