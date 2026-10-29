"""Runner policy compatibility, staged configuration and public CLI behavior."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_config import Menu
from test_workflow import workflow


def runners():
    return {'default_targets': ['linux', 'mini'], 'timeout_seconds': 900,
            'queue_timeout_seconds': 300, 'artifacts': ['test-results'], 'targets': {
        'linux': {'provider': 'github', 'repository': 'owner/private-control', 'os': 'linux',
                  'arch': 'x64', 'label': 'ubuntu-24.04', 'capabilities': ['tests']},
        'mini': {'provider': 'ssh', 'host': 'mini', 'os': 'macos', 'arch': 'arm64',
                 'capabilities': ['tests']}}}


def test_runners_coexist_with_behavior_preferences_and_disabled_model_stages(workflow):
    w = workflow
    policy = copy.deepcopy(w.DEFAULT)
    policy.update(runners=runners(), stages_enabled=False,
                  behaviors={'delivery': 'review'}, instructions='Keep this custom guidance.')
    document = w.update(w.defaults(), policy)
    assert w.validate(document) == document
    text = w._stage_text(w.resolved(document))
    assert 'Test runners are configured' in text
    assert 'Model stages are disabled' in text
    assert 'Leave changes uncommitted' in text
    assert 'Keep this custom guidance.' in text
    assert '1. Plan with' not in text
    excluded = w.action(document, 'exclude', repository='/example/repository')
    assert 'Test runners are configured' not in w._stage_text(w.effective(excluded, '/example/repository'))


def test_runner_policy_compatibility_and_resolution(workflow, tmp_path):
    w = workflow
    before = w.defaults()
    assert 'runners' not in w.resolved(before)
    assert w.runners(before) == w.RUNNER_DEFAULT
    assert not w.config_path().exists()
    policy = w.resolved(before)
    policy['runners'] = runners()
    changed = w.update(before, policy)
    assert w.validate(changed) == changed
    assert w.runners_summary(policy) == '2 targets · defaults: linux, mini'
    assert w.runners_summary(w.DEFAULT) == 'unconfigured'
    copied = w.action(changed, 'add', 'cloud')
    assert copied['workflows']['cloud']['runners'] == runners()
    repository = str(tmp_path.resolve() / 'repo')
    assigned = w.action(copied, 'use', 'cloud', repository)
    assert w.runners(assigned, repository) == runners()
    excluded = w.action(assigned, 'exclude', repository=repository)
    assert w.runners(excluded, repository) == runners()
    assert not w.effective(excluded, repository)['enabled']
    assert 'Test runners are configured' not in w._stage_text(w.effective(excluded, repository))
    assert 'runs start -- COMMAND' in w._stage_text(policy)
    assert 'Test runners are configured' not in w._stage_text(w.DEFAULT)
    assert w.action(excluded, 'inherit', repository=repository)['repositories'] == {}
    with pytest.raises(ValueError, match='cannot be combined'):
        w.runners(changed, repository, 'standard')
    with pytest.raises(ValueError, match='unknown workflow'):
        w.runners(changed, name='missing')
    # Legacy and v2 reads preserve absent runner fields and never write migration.
    legacy = {'version': 1, 'global': copy.deepcopy(w.DEFAULT), 'repositories': {}}
    w.config_path().parent.mkdir(parents=True)
    w.config_path().write_text(w.encode(legacy))
    text = w.config_path().read_text()
    loaded, _ = w.load()
    assert w.runners(loaded) == w.RUNNER_DEFAULT
    assert w.config_path().read_text() == text


@pytest.mark.parametrize('field,value', [
    ('timeout_seconds', True), ('timeout_seconds', 0), ('timeout_seconds', 21601),
    ('queue_timeout_seconds', 3601), ('targets', []),
    ('targets', {f't{i}': {} for i in range(17)}),
    ('default_targets', ['missing']), ('default_targets', ['linux', 'linux']),
    ('default_targets', [None]), ('default_targets', 'linux'),
    ('artifacts', 'test-results'), ('artifacts', ['x'] * 33), ('artifacts', ['x', 'x']),
    ('artifacts', ['../secret']), ('artifacts', ['/tmp/file']), ('artifacts', ['a\\b']),
    ('artifacts', ['a//b']), ('artifacts', ['.git/config']), ('artifacts', ['a\nfile']),
])
def test_runner_config_rejects_invalid_sections(workflow, field, value):
    config = runners()
    config[field] = value
    with pytest.raises(ValueError):
        workflow.validate_runners(config)


@pytest.mark.parametrize('field,value', [
    ('provider', 'other'), ('provider', {}), ('os', 'windows'), ('arch', 'riscv'),
    ('repository', 'bad'), ('repository', []), ('label', []), ('label', 'macos-15'),
    ('capabilities', ['tests', 'tests']), ('capabilities', ['space bad']),
    ('capabilities', 'tests'), ('capabilities', ['x'] * 33),
])
def test_runner_config_rejects_invalid_targets(workflow, field, value):
    config = runners()
    config['targets']['linux'][field] = value
    with pytest.raises(ValueError):
        workflow.validate_runners(config)


def test_runner_config_strict_structure(workflow):
    for value in (None, {}, {'unknown': 'field'}):
        with pytest.raises(ValueError):
            workflow.validate_runners(value)
    for target in ([], {'provider': 'ssh'}, {'provider': 'ssh', 'host': '-option', 'os': 'linux', 'arch': 'x64', 'capabilities': []}):
        value = runners()
        value['targets']['mini'] = target
        with pytest.raises(ValueError):
            workflow.validate_runners(value)
    with pytest.raises(ValueError):
        workflow.validate_runner_path('x' * 1025)


def test_runner_editor_preserves_cancel_and_scope(workflow, tmp_path, monkeypatch):
    w = workflow
    doc = w.defaults()
    monkeypatch.setenv('EDITOR', 'test-editor --wait')
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 19)
    assert w.edit_runners(doc) == (19, doc)
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 0)
    assert w.edit_runners(doc) == (0, doc)
    def editor(argv):
        path = Path(argv[-1])
        assert path.stat().st_mode & 0o777 == 0o600
        path.write_text(w.encode(runners()))
        return 0
    monkeypatch.setattr(w.subprocess, 'call', editor)
    repository = str(tmp_path.resolve() / 'repo')
    rc, after = w.edit_runners(doc, repository)
    assert rc == 0 and w.runners(after, repository) == runners()
    assert w.runners(doc) == w.RUNNER_DEFAULT
    def remove(argv):
        Path(argv[-1]).unlink()
        return 0
    monkeypatch.setattr(w.subprocess, 'call', remove)
    with pytest.raises(ValueError, match='removed runners'):
        w.edit_runners(doc)
    monkeypatch.setenv('EDITOR', '   ')
    with pytest.raises(ValueError, match='set VISUAL or EDITOR'):
        w.edit_runners(doc)


def test_runner_menu_stages_until_save(workflow, t_mod, monkeypatch, tmp_path):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: w.__dict__)
    cfg = SimpleNamespace(repos={})
    t_mod._workflow_session(cfg)
    original = copy.deepcopy(cfg._workflow_document)
    policy = w.resolved(original)
    policy['runners'] = runners()
    after = w.update(original, policy)
    monkeypatch.setitem(w.__dict__, 'edit_runners', lambda *a: (0, after))
    t_mod._workflow_policy_menu(Menu(['runners', 'show', 'edit', 'back', 'back']), cfg, w.__dict__, name='standard')
    assert cfg._workflow_document == after and t_mod._workflow_pending(cfg)
    assert not w.config_path().exists()
    assert '2 targets' in '\n'.join(t_mod._workflow_review(after))
    repo = str(tmp_path.resolve() / 'repo')
    inline = w.update(after, policy, repo)
    assert 'Test runners: 2 targets' in '\n'.join(t_mod._workflow_review(inline))
    for result in ((17, original),):
        monkeypatch.setitem(w.__dict__, 'edit_runners', lambda *a: result)
        t_mod._workflow_runners_menu(Menu(['edit', 'back']), cfg, w.__dict__)
    def invalid(*args):
        raise ValueError('bad runner')
    monkeypatch.setitem(w.__dict__, 'edit_runners', invalid)
    t_mod._workflow_runners_menu(Menu(['edit', 'back']), cfg, w.__dict__)
    t_mod._workflow_runners_menu(Menu([None]), cfg, w.__dict__)


def test_runner_cli_contract(t_mod):
    parser = t_mod.build_parser()
    forms = [('runners list', []), ('runners list', ['--repo', 'api', '--json']),
             ('runners list', ['standard']), ('runners edit', []),
             ('runners setup', ['--github-repo', 'owner/private-control', '--create']),
             ('runs start', ['--target', 'linux', '--json', '--', 'python3', '-m', 'pytest']),
             ('runs list', ['--json']), ('runs status', ['11111111-1111-4111-8111-111111111111', '--json']),
             ('runs logs', ['11111111-1111-4111-8111-111111111111']),
             ('runs artifacts', ['11111111-1111-4111-8111-111111111111', '--output', 'out']),
             ('runs cancel', ['11111111-1111-4111-8111-111111111111']),
             ('runs cleanup', ['11111111-1111-4111-8111-111111111111'])]
    for words, flags in forms:
        argv = ['config', 'workflow', *words.split(), *flags]
        args = parser.parse_args(argv)
        assert args.verb == 'config'
        readonly = words in ('runners list', 'runs list', 'runs status', 'runs logs')
        assert t_mod._command_readonly(argv, terminal=True) is readonly
    for group in ('config workflow runners', 'config workflow runs'):
        assert 'USAGE' in t_mod._group_help(group)
        assert t_mod.main(group.split()) == 0
    assert t_mod._command_readonly(['config', 'workflow', 'runs'])
    assert t_mod._command_readonly(['config', 'workflow', 'runners'])
    assert t_mod._canonical_argv(['config', 'workflow', 'runners', 'ls'])[-1] == 'list'
    assert t_mod._canonical_argv(['config', 'workflow', 'runs', 'ls'])[-1] == 'list'


def test_runner_config_commands_are_readonly_and_explicit(workflow, t_mod, monkeypatch, tmp_path, capsys):
    w = workflow
    repository = str(tmp_path.resolve() / 'repo')
    cfg = SimpleNamespace(repos={'api': repository}, hosts={})
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod, '_workflow', lambda: w.__dict__)
    monkeypatch.setattr(t_mod, '_t_offer_update', lambda: pytest.fail('readonly prompted'))
    assert t_mod.main(['config', 'workflow', 'runners', 'list']) == 0
    assert 'No test runners configured' in capsys.readouterr().out
    policy = w.resolved(w.defaults())
    policy['runners'] = runners()
    document = w.update(w.defaults(), policy)
    monkeypatch.setitem(w.__dict__, 'load', lambda: (document, None))
    for flags in ([], ['--repo', 'api'], ['standard']):
        assert t_mod.main(['config', 'workflow', 'runners', 'list', *flags]) == 0
        assert 'linux' in capsys.readouterr().out
        assert t_mod.main(['config', 'workflow', 'runners', 'list', *flags, '--json']) == 0
        assert json.loads(capsys.readouterr().out)['runners'] == runners()
    assert not w.config_path().exists()
    assert t_mod.main(['config', 'workflow', 'runners', 'list', '--repo', 'missing']) == 1
    assert 'unknown registered' in capsys.readouterr().err
    assert t_mod.main(['config', 'workflow', 'runners', 'list', 'standard', '--repo', 'api']) == 1
    capsys.readouterr()
    assert t_mod.main(['config', 'workflow', 'runners', 'edit']) == 1
    assert 'terminal' in capsys.readouterr().err
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setitem(w.__dict__, 'edit_runners', lambda *a: (17, document))
    assert t_mod.main(['config', 'workflow', 'runners', 'edit']) == 17
    monkeypatch.setitem(w.__dict__, 'edit_runners', lambda *a: (0, document))
    assert t_mod.main(['config', 'workflow', 'runners', 'edit']) == 0
    assert 'unchanged' in capsys.readouterr().out
    after = w.action(document, 'add', 'new')
    monkeypatch.setitem(w.__dict__, 'edit_runners', lambda *a: (0, after))
    saved = []
    monkeypatch.setitem(w.__dict__, 'save', lambda *a: saved.append(a))
    assert t_mod.main(['config', 'workflow', 'runners', 'edit']) == 0
    assert saved[0][0] == after
    fake = {'setup_github': lambda repo, create=False: {'repository': repo, 'created': create}}
    monkeypatch.setattr(t_mod, '_runners', lambda: fake)
    assert t_mod.main(['config', 'workflow', 'runners', 'setup', '--github-repo', 'owner/private-control']) == 0
    def interrupt(*a, **kw):
        raise KeyboardInterrupt
    fake['setup_github'] = interrupt
    assert t_mod.main(['config', 'workflow', 'runners', 'setup', '--github-repo', 'owner/private-control']) == 130


def test_runner_run_commands_and_target_scope(workflow, t_mod, monkeypatch, tmp_path, capsys):
    w = workflow
    canonical = str(tmp_path.resolve() / 'source')
    checkout = str(tmp_path.resolve() / 'worktree')
    cfg = SimpleNamespace(repos={'api': canonical, 'other': str(tmp_path.resolve() / 'other')}, hosts={'mini': 'remote-user@remote'})
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod, '_workflow', lambda: w.__dict__)
    monkeypatch.setattr(t_mod, '_t_git', lambda *a: checkout)
    monkeypatch.setattr(t_mod, '_t_canonical_root', lambda *a: canonical)
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda *a: None)
    policy = w.resolved(w.defaults())
    policy['runners'] = runners()
    document = w.update(w.defaults(), policy)
    monkeypatch.setitem(w.__dict__, 'load', lambda: (document, None))
    calls = []
    def start(root, config, command, **kwargs):
        calls.append((root, config, command, kwargs))
        return {'id': 'fixture', 'exit_code': 7, 'status': 'failed'}
    fake = {'select_targets': t_mod._runners()['select_targets'], 'start': start, 'list_jobs': lambda: [], 'status': lambda job: {'id': job},
            'logs': lambda job, target=None: {'one': 'hello\n', 'two': 'world\n'},
            'artifacts': lambda *a, **kw: {'one': '/result'},
            'cancel': lambda job: {'id': job, 'status': 'cancelled'},
            'cleanup': lambda job: {'id': job, 'cleaned': True}}
    monkeypatch.setattr(t_mod, '_runners', lambda: fake)
    command = ['config', 'workflow', 'runs']
    assert t_mod.main([*command, 'start', '--repo', 'api', '--target', 'mini', '--include', 'new.py', '--artifact', 'report', '--timeout', '12', '--queue-timeout', '9', '--json', '--', 'python3', '-c', 'print("quoted ; $x")']) == 7
    assert calls[0][0] == checkout
    assert calls[0][1]['targets']['mini']['host'] == 'remote-user@remote'
    assert calls[0][2] == ['python3', '-c', 'print("quoted ; $x")']
    assert calls[0][3]['timeout'] == 12 and calls[0][3]['include'] == ['new.py']
    assert json.loads(capsys.readouterr().out)['exit_code'] == 7
    assert t_mod.main([*command, 'start', '--', 'true']) == 7
    assert 'Run: fixture' in capsys.readouterr().out
    assert t_mod.main([*command, 'start', '--repo', 'other', '--', 'true']) == 1
    assert 'must match' in capsys.readouterr().err
    cfg.hosts = {}
    assert t_mod.main([*command, 'start', '--target', 'linux', '--json', '--', 'true']) == 7
    assert json.loads(capsys.readouterr().out)['exit_code'] == 7
    assert list(calls[-1][1]['targets']) == ['linux']
    assert t_mod.main([*command, 'start', '--target', 'linux', '--target', 'mini', '--os', 'linux', '--json', '--', 'true']) == 7
    assert json.loads(capsys.readouterr().out)['exit_code'] == 7
    assert calls[-1][3]['targets'] == ['linux']
    assert t_mod.main([*command, 'start', '--target', 'linux', '--target', 'mini', '--os', 'linux', '--json', '--', 'true']) == 7
    assert json.loads(capsys.readouterr().out)['exit_code'] == 7
    assert calls[-1][3]['targets'] == ['linux']
    assert t_mod.main([*command, 'start', '--', 'true']) == 1
    assert 'not registered' in capsys.readouterr().err
    cfg.hosts = {'mini': 'remote'}
    assert t_mod.main([*command, 'start']) == 1
    assert 'requires -- COMMAND' in capsys.readouterr().err
    document = w.action(document, 'exclude', repository=canonical)
    assert t_mod.main([*command, 'start', '--', 'true']) == 1
    assert 'disabled or excluded' in capsys.readouterr().err
    monkeypatch.setattr(t_mod, '_t_canonical_root', lambda *a: None)
    assert t_mod.main([*command, 'start', '--', 'true']) == 1
    assert 'registered repository' in capsys.readouterr().err
    assert t_mod.main([*command, 'list']) == 0
    assert 'No test runs' in capsys.readouterr().out
    assert t_mod.main([*command, 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == []
    fake['list_jobs'] = lambda: [{'id': 'fixture', 'status': 'succeeded', 'targets': {'linux': {}}}]
    assert t_mod.main([*command, 'list']) == 0
    assert 'fixture' in capsys.readouterr().out
    assert t_mod.main([*command, 'status', 'fixture']) == 0
    assert 'Run: fixture' in capsys.readouterr().out
    assert t_mod.main([*command, 'status', 'fixture', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['id'] == 'fixture'
    assert t_mod.main([*command, 'logs', 'fixture']) == 0
    assert '== one ==\nhello\n' in capsys.readouterr().out
    for verb in ('artifacts', 'cancel', 'cleanup'):
        assert t_mod.main([*command, verb, 'fixture']) == 0
        assert json.loads(capsys.readouterr().out)
    def invalid(*a, **kw):
        raise ValueError('missing job')
    fake['status'] = invalid
    assert t_mod.main([*command, 'status', 'fixture']) == 1
    assert 'missing job' in capsys.readouterr().err
    def interrupt(*a, **kw):
        raise KeyboardInterrupt
    fake['status'] = interrupt
    assert t_mod.main([*command, 'status', 'fixture']) == 130


def test_runner_receipts_label_outcomes_and_cleanup(t_mod):
    receipt = {'id': 'fixture', 'status': 'failed', 'exit_code': 7,
               'error': 'snapshot error', 'resources': [{'cleanup_error': 'release unavailable'}],
               'targets': {'linux': {'provider': 'github', 'status': 'failed', 'exit_code': 7,
                            'error': 'transport error', 'cleanup_error': 'cleanup error',
                            'result': {'artifact_error': 'missing report'}},
                           'mini': {'type': 'ssh', 'status': 'error', 'exit_code': None}}}
    text = '\n'.join(t_mod._runner_receipt_lines(receipt))
    assert all(item in text for item in ('Run: fixture', 'Status: failed', 'Exit code: 7',
        'transport error', 'cleanup error', 'missing report', 'release unavailable', 'snapshot error'))
    assert t_mod._runner_receipt_lines({'id': 'fixture'}) == ['Run: fixture', 'Status: unknown']


def test_runner_json_start_skips_update_prompt_but_child_json_is_literal(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: pytest.fail('machine mode prompted'))
    assert t_mod._t_auto_update(['config', 'workflow', 'runs', 'start', '--json', '--', 'true']) is None
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: True)
    monkeypatch.setattr(t_mod, '_t_update_installation', lambda: None)
    assert t_mod._t_auto_update(['config', 'workflow', 'runs', 'start', '--', 'program', '--json']) is None
