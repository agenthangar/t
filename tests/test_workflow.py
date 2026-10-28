"""Account workflow persistence, safe editor/native delivery, and config staging."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from test_config import Menu

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('workflow_test', ROOT / 'libexec/t_workflow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, path in [('HOME', tmp_path / 'home'), ('XDG_CONFIG_HOME', tmp_path / 'xdg'),
                      ('CODEX_HOME', tmp_path / 'codex'), ('CLAUDE_CONFIG_DIR', tmp_path / 'claude')]:
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.delenv('EDITOR', raising=False)
    return module


def native_paths():
    return [Path(os.environ['CODEX_HOME']) / 'AGENTS.md', Path(os.environ['CLAUDE_CONFIG_DIR']) / 'CLAUDE.md']


def test_defaults_inheritance_and_explicit_activation(workflow, tmp_path):
    w = workflow
    document, before = w.load()
    assert before is None and not w.config_path().exists()
    assert document['workflows']['standard']['planning']['model'] == 'gpt-6-astra'
    repository = str(tmp_path.resolve() / 'repo')
    assert w.effective(document, repository) == document['workflows']['standard']
    assert w.scoped(document, repository) is None
    override = w.effective(document, repository)
    override['planning']['model'] = 'private/model:version'
    changed = w.update(document, override, repository)
    assert w.effective(changed, repository) == override
    assert w.effective(changed)['planning']['model'] == 'gpt-6-astra'
    assert w.update(changed, None, repository) == document
    w.save(changed, before)
    saved, saved_before = w.load()
    assert saved == changed
    assert stat.S_IMODE(w.config_path().stat().st_mode) == 0o600
    assert all(p.exists() for p in native_paths())
    before_stats = [p.stat() for p in [w.config_path(), *native_paths()]]
    w.save(saved, saved_before)
    assert [p.stat() for p in [w.config_path(), *native_paths()]] == before_stats
    with pytest.raises(ValueError, match='changed while editing'):
        w.save(saved, before)
    assert 'private/model:version' in native_paths()[0].read_text()


@pytest.mark.parametrize('mutate,match', [
    (lambda d: d.update(extra=1), 'requires version'),
    (lambda d: d.update(version=True), 'version'),
    (lambda d: d.update(version=3), 'version'),
    (lambda d: d.update(global_=None), 'requires version'),
    (lambda d: d.update(repositories=[]), 'repositories'),
    (lambda d: d['repositories'].update({'relative': {}}), 'canonical absolute'),
    (lambda d: d['workflows']['standard'].pop('enabled'), 'requires enabled'),
    (lambda d: d['workflows']['standard'].update(enabled='yes'), 'true or false'),
    (lambda d: d['workflows']['standard']['planning'].update(extra=1), 'requires model'),
    (lambda d: d['workflows']['standard']['planning'].update(model='x\n## override'), 'model ID'),
    (lambda d: d['workflows']['standard']['planning'].update(model='--bad'), 'model ID'),
    (lambda d: d['workflows']['standard']['implementation'].update(effort='one-level-lower'), 'effort'),
    (lambda d: d['workflows']['standard']['review'].update(effort='auto'), 'effort'),
])
def test_strict_schema(workflow, mutate, match):
    document = workflow.defaults()
    mutate(document)
    with pytest.raises(ValueError, match=match):
        workflow.validate(document)


def test_native_preservation_overrides_and_disabled_scope(workflow, tmp_path):
    w = workflow
    codex, claude = native_paths()
    original = '# Personal prose\nUse old general models.\n<!-- t public-issues: begin -->\nissue policy\n<!-- t public-issues: end -->\n'
    codex.write_text(original)
    codex.chmod(0o640)
    claude.write_text('Claude preferences\n')
    override = codex.with_name('AGENTS.override.md')
    override.write_text('Important override prose\n')
    document = w.defaults()
    disabled = copy.deepcopy(w.DEFAULT)
    disabled['enabled'] = False
    document = w.update(document, disabled, str(tmp_path.resolve() / 'repo'))
    w.save(document, None)
    for path in [codex, claude, override]:
        text = path.read_text()
        assert text.count(w.BEGIN) == 1 and text.count(w.END) == 1
        assert 'supersede older' in text and 'repository-specific rules remain authoritative' in text
        assert 'linked worktrees' in text and 'created later' in text and 'disabled for this scope' in text
        assert 'one supported reasoning-effort level below' in text
        assert 'acceptance criteria' in text and 'CI through completion' in text
        assert 'never claim a stage' in text and 'sub-agent delegation' in text
    assert codex.read_text().startswith(original)
    assert stat.S_IMODE(codex.stat().st_mode) == 0o640
    assert override.read_text().startswith('Important override prose\n')
    assert w.merge(codex.read_text(), w.render(document)) == codex.read_text()
    explicit = w.defaults()
    explicit['workflows']['standard']['planning']['effort'] = 'xhigh'
    explicit['workflows']['standard']['implementation']['effort'] = 'high'
    explicit['workflows']['standard']['review']['effort'] = 'medium'
    assert 'reasoning effort: xhigh' in w.render(explicit)


def test_speed_policy_scopes_and_native_sync(workflow, tmp_path):
    w = workflow
    document = w.defaults()
    enabled_repo = str(tmp_path.resolve() / 'enabled')
    disabled_repo = str(tmp_path.resolve() / 'disabled')
    document['repositories'][enabled_repo] = {'inline': copy.deepcopy(w.DEFAULT)}
    document['repositories'][disabled_repo] = {'inline': dict(w.DEFAULT, enabled=False)}
    before = copy.deepcopy(document)
    rendered = w.render(document)
    global_text, *scopes = rendered.split('\n\nRepository scope: ')
    enabled_text = next(text for text in scopes if enabled_repo in text)
    disabled_text = next(text for text in scopes if disabled_repo in text)
    for text in (global_text, enabled_text):
        assert "parent agent's current speed/service tier" in text
        assert 'Fast or Ultrafast' in text
        assert 'independently of its model and reasoning effort' in text
        assert 'supported subsequent-turn boundary' in text
        assert 'exposed speed controls or documented inheritance' in text
        assert 'unsupported or cannot be verified, disclose' in text
        assert 'never claim a verified tier or an immediate speed switch' in text
    assert 'disabled for this scope' in disabled_text
    assert 'speed/service tier' not in disabled_text
    assert document == before
    for path in native_paths():
        path.write_text('Personal instructions\n')
    w.save(document, None)
    saved_texts = [path.read_text() for path in native_paths()]
    for path in native_paths():
        assert path.read_text().startswith('Personal instructions\n')
        assert rendered in path.read_text()
        path.write_text('Personal instructions\n')
    assert w.sync() == 0
    assert [path.read_text() for path in native_paths()] == saved_texts
    assert w.load()[0] == before


@pytest.mark.parametrize('text', [' ', '\n\n'])
def test_empty_codex_override_remains_empty(workflow, text):
    override = Path(os.environ['CODEX_HOME']) / 'AGENTS.override.md'
    override.write_text(text)
    workflow.save(workflow.defaults(), None)
    assert override.read_text() == text
    assert workflow.BEGIN in native_paths()[0].read_text()


@pytest.mark.parametrize('broken', [
    '<!-- t agent-workflow: begin -->', '<!-- t agent-workflow: end -->',
    '<!-- t agent-workflow: end --><!-- t agent-workflow: begin -->',
    '<!-- t agent-workflow: begin --><!-- t agent-workflow: begin --><!-- t agent-workflow: end -->',
])
def test_native_malformed_preflight_writes_no_targets(workflow, broken):
    codex, claude = native_paths()
    codex.write_text('keep')
    claude.write_text(broken)
    with pytest.raises(ValueError, match='malformed'):
        workflow.save(workflow.defaults(), None)
    assert codex.read_text() == 'keep' and claude.read_text() == broken
    assert not workflow.config_path().exists()


def test_symlinks_nonregular_and_changed_reads_are_refused(workflow, tmp_path, monkeypatch):
    w = workflow
    source = tmp_path / 'source'
    source.write_text('source')
    codex, _ = native_paths()
    codex.symlink_to(source)
    with pytest.raises(ValueError, match='symlink'):
        w.save(w.defaults(), None)
    assert source.read_text() == 'source' and not w.config_path().exists()
    with pytest.raises(ValueError, match='nonregular'):
        w.snapshot(tmp_path)
    original = w.os.fstat
    monkeypatch.setattr(w.os, 'fstat', lambda fd: source.with_name('not-here').stat())
    with pytest.raises(FileNotFoundError):
        w.snapshot(source)
    monkeypatch.setattr(w.os, 'fstat', original)
    other = tmp_path / 'other'
    other.write_text('different')
    monkeypatch.setattr(w.os, 'fstat', lambda fd: other.stat())
    with pytest.raises(ValueError, match='changed while reading'):
        w.snapshot(source)


def test_concurrent_edits_and_failed_publish_rollback(workflow, tmp_path, monkeypatch):
    w = workflow
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('old first')
    second.write_text('old second')
    first.chmod(0o640)
    before = w.snapshot(first)
    first.write_text('external')
    with pytest.raises(ValueError, match='changed while updating'):
        w.publish([(first, before, 'new')])
    assert first.read_text() == 'external'
    first.write_text('old first')
    replace = w.os.replace
    failed = False
    def fail_second(source, target):
        nonlocal failed
        if Path(target) == second and not failed:
            failed = True
            raise OSError('disk failure')
        return replace(source, target)
    monkeypatch.setattr(w.os, 'replace', fail_second)
    with pytest.raises(OSError, match='disk failure'):
        w.publish([(first, w.snapshot(first), 'new first'), (second, w.snapshot(second), 'new second')])
    assert first.read_text() == 'old first' and second.read_text() == 'old second'
    assert stat.S_IMODE(first.stat().st_mode) == 0o640
    assert not list(tmp_path.glob('.t-workflow-*'))
    first.unlink()
    failed = False
    with pytest.raises(OSError):
        w.publish([(first, None, 'new'), (second, w.snapshot(second), 'new')])
    assert not first.exists()


def test_preparation_and_publish_concurrent_checks(workflow, tmp_path, monkeypatch):
    w = workflow
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('old')
    second.write_text('old')
    original = w.snapshot
    count = 0
    def changed(path):
        nonlocal count
        count += 1
        if count == 3:
            first.write_text('external')
        return original(path)
    monkeypatch.setattr(w, 'snapshot', changed)
    with pytest.raises(ValueError, match='changed while updating'):
        w.publish([(first, original(first), 'new'), (second, original(second), 'new')])
    assert first.read_text() == 'external' and second.read_text() == 'old'
    assert not list(tmp_path.glob('.t-workflow-*'))


def test_rollback_refuses_to_overwrite_external_edit(workflow, tmp_path, monkeypatch):
    w = workflow
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('old')
    second.write_text('old')
    replace = w.os.replace
    def failure(source, target):
        if Path(target) == second:
            first.write_text('external')
            raise OSError('fail')
        replace(source, target)
    monkeypatch.setattr(w.os, 'replace', failure)
    with pytest.raises(ValueError, match='changed during rollback'):
        w.publish([(first, w.snapshot(first), 'new'), (second, w.snapshot(second), 'new')])
    assert first.read_text() == 'external'


def test_editor_private_argv_status_validation_and_inheritance(workflow, tmp_path, monkeypatch):
    w = workflow
    document = w.defaults()
    calls = []
    monkeypatch.setenv('VISUAL', '"my editor" --wait')
    monkeypatch.setenv('EDITOR', 'ignored')
    def editor(argv):
        calls.append(argv)
        assert argv[:2] == ['my editor', '--wait']
        path = Path(argv[-1])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        value = json.loads(path.read_text())
        value['planning']['model'] = 'custom-v1'
        path.write_text(w.encode(value))
        return 0
    monkeypatch.setattr(w.subprocess, 'call', editor)
    rc, changed = w.edit(document)
    assert rc == 0 and changed['workflows']['standard']['planning']['model'] == 'custom-v1'
    assert document == w.defaults() and not w.config_path().exists()
    assert not Path(calls[0][-1]).exists()
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 17)
    assert w.edit(document) == (17, document)
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 0)
    assert w.edit(document) == (0, document)
    repository = str(tmp_path.resolve() / 'repo')
    assert w.edit(document, repository) == (0, document)
    changed = w.update(document, changed['workflows']['standard'], repository)
    def reset(argv):
        Path(argv[-1]).write_text('null')
        return 0
    monkeypatch.setattr(w.subprocess, 'call', reset)
    assert w.edit(changed, repository) == (0, document)
    with pytest.raises(ValueError, match='requires enabled'):
        w.edit(document)
    def invalid(argv):
        Path(argv[-1]).write_text('{bad json')
        return 0
    monkeypatch.setattr(w.subprocess, 'call', invalid)
    with pytest.raises(ValueError):
        w.edit(document)
    def removed(argv):
        Path(argv[-1]).unlink()
        return 0
    monkeypatch.setattr(w.subprocess, 'call', removed)
    with pytest.raises(ValueError, match='removed workflow'):
        w.edit(document)
    monkeypatch.setenv('VISUAL', ' ')
    with pytest.raises(ValueError, match='set VISUAL'):
        w.edit(document)


def args(**kwargs):
    return SimpleNamespace(show=False, edit=False, workflow=True, workflow_repo=None, **kwargs)


def test_cli_workflow_show_edit_and_validation(workflow, t_mod, monkeypatch, tmp_path, capsys):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    repository = str(tmp_path.resolve() / 'repo')
    cfg = SimpleNamespace(repos={'api': repository})
    show = SimpleNamespace(show=True, edit=False, workflow=True, workflow_repo='api')
    assert t_mod.cmd_config(cfg, show) == 0
    assert 'recommended defaults (not saved)' in capsys.readouterr().out
    assert not w.config_path().exists()
    assert t_mod._command_readonly(['config', '--show', '--workflow', '--repo', 'api'])
    edit = SimpleNamespace(show=False, edit=True, workflow=True, workflow_repo=None)
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 9)
    assert t_mod.cmd_config(cfg, edit) == 9
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 0)
    assert t_mod.cmd_config(cfg, edit) == 0 and not w.config_path().exists()
    def editor(argv):
        value = copy.deepcopy(w.DEFAULT)
        value['enabled'] = False
        Path(argv[-1]).write_text(w.encode(value))
        return 0
    monkeypatch.setattr(w.subprocess, 'call', editor)
    assert t_mod.cmd_config(cfg, edit) == 0
    assert w.load()[0]['workflows']['standard']['enabled'] is False
    edit.workflow_repo = 'api'
    def override(argv):
        value = json.loads(Path(argv[-1]).read_text())
        value['enabled'] = True
        Path(argv[-1]).write_text(w.encode(value))
        return 0
    monkeypatch.setattr(w.subprocess, 'call', override)
    assert t_mod.cmd_config(cfg, edit) == 0
    assert t_mod.cmd_config(cfg, show) == 0
    assert 'repository override' in capsys.readouterr().out
    edit.workflow_repo = 'missing'
    assert t_mod.cmd_config(cfg, edit) == 1
    edit.workflow_repo = None
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    assert t_mod.cmd_config(cfg, edit) == 1
    assert t_mod.cmd_config(cfg, args()) == 1
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=True, edit=False, workflow=False, workflow_repo='api')) == 1


@pytest.mark.parametrize('save', [False, True])
def test_menu_workflow_save_and_cancel(workflow, t_mod, tmp_path, monkeypatch, capsys, save):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    local = tmp_path / 'local.zsh'
    local.write_text('# shell prose\n')
    monkeypatch.setenv('T_LOCAL_RC', str(local))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'absent'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda snippet: '')
    cfg = t_mod.Config()
    cfg.repos = {'api': str(tmp_path.resolve() / 'repo')}
    choices = ['workflow', 'defaults', 'back', 'repos', 'api', 'workflow', 'exclude', 'back', 'back', '__back__']
    choices += ['save'] if save else ['cancel', 'discard']
    ui = Menu(choices)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == (0 if save else 130)
    assert ui.restored
    assert local.read_text() == '# shell prose\n'
    output = capsys.readouterr().out
    if save:
        assert 'saved agent workflow and native instructions' in output
        assert 'T_LOCAL_RC' not in output and 'source ' not in output
    assert w.config_path().exists() is save
    if save:
        document, _ = w.load()
        assert document['workflows']['standard'] == w.DEFAULT
        assert document['repositories'][cfg.repos['api']]['excluded'] is True
        assert all(path.exists() for path in native_paths())
    else:
        assert not any(path.exists() for path in native_paths())


def test_menu_workflow_editor_inherit_and_profile(workflow, t_mod, tmp_path, monkeypatch):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    cfg = SimpleNamespace(repos={'api': str(tmp_path.resolve() / 'repo')})
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 0)
    ui = Menu(['edit', 'back'])
    t_mod._config_workflow(ui, cfg, 'api')
    assert not t_mod._workflow_pending(cfg)
    ui = Menu(['exclude', 'inherit', 'back'])
    t_mod._config_workflow(ui, cfg, 'api')
    assert not cfg._workflow_document['repositories']
    calls = []
    monkeypatch.setattr(t_mod, '_instructions_init', lambda: calls.append('init'))
    monkeypatch.setattr(t_mod, '_instructions_edit', lambda: calls.append('edit') or 0)
    ui = Menu(['profile', 'back'])
    t_mod._config_workflow(ui, cfg)
    assert calls == ['init', 'edit']


def test_installer_sync_absent_and_configured(workflow):
    w = workflow
    assert w.sync() == 0 and not w.config_path().exists()
    assert not any(path.exists() for path in native_paths())
    w.config_path().parent.mkdir(parents=True)
    document = w.defaults()
    document['workflows']['standard']['planning']['model'] = 'chosen-model'
    w.config_path().write_text(w.encode(document))
    assert w.sync() == 0
    assert all('chosen-model' in path.read_text() for path in native_paths())
    w.config_path().write_text('{invalid')
    with pytest.raises(ValueError):
        w.sync()


def test_duplicate_json_keys_are_rejected(workflow):
    with pytest.raises(ValueError, match='duplicate workflow key'):
        workflow.decode('{"enabled": true, "enabled": false}')


def test_transaction_rolls_back_on_interrupt(workflow, tmp_path, monkeypatch):
    w = workflow
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('before')
    replace = w.os.replace
    def interrupt(source, target):
        if Path(target) == second:
            raise KeyboardInterrupt
        replace(source, target)
    monkeypatch.setattr(w.os, 'replace', interrupt)
    with pytest.raises(KeyboardInterrupt):
        w.publish([(first, w.snapshot(first), 'after'), (second, None, 'after')])
    assert first.read_text() == 'before' and not second.exists()


@pytest.mark.parametrize('malformed', [False, True])
def test_combined_config_save_is_one_transaction(workflow, t_mod, tmp_path, monkeypatch, capsys, malformed):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'absent'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    target = tmp_path / 'private-local.zsh'
    target.write_text('# keep shell prose\n')
    target.chmod(0o640)
    local = tmp_path / 'local-link.zsh'
    local.symlink_to(target)
    monkeypatch.setenv('T_LOCAL_RC', str(local))
    monkeypatch.setenv('T_SETUP_SHIM', '1')
    cfg = t_mod.Config()
    selected = 'codex' if cfg.agent_default == 'claude' else 'claude'
    codex, claude = native_paths()
    codex.write_text('keep native prose\n')
    if malformed:
        claude.write_text(w.END)
    ui = Menu(['workflow', 'defaults', 'back', 'tool', selected, 'save'])
    review = []
    ui.page = lambda title, lines, *a, **kw: review.extend(lines) or 'enter'
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    sync = []
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda snippet: sync.append(snippet) or '')
    assert t_mod.cmd_config(cfg, SimpleNamespace(show=False)) == (1 if malformed else 0)
    assert any('DEV_AGENT_DEFAULT' in line for line in review)
    assert any('Agent workflow and native' in line for line in review)
    assert local.is_symlink() and stat.S_IMODE(target.stat().st_mode) == 0o640
    if malformed:
        assert target.read_text() == '# keep shell prose\n'
        assert codex.read_text() == 'keep native prose\n' and claude.read_text() == w.END
        assert not w.config_path().exists() and not sync
        assert 'malformed' in capsys.readouterr().err
    else:
        assert f'DEV_AGENT_DEFAULT={selected}' in target.read_text()
        assert target.read_text().startswith('# keep shell prose\n')
        assert codex.read_text().startswith('keep native prose\n')
        assert w.config_path().exists() and sync == ['_t_sync_config']
        output = capsys.readouterr().out
        assert 'saved agent workflow and native instructions' in output and 'T_LOCAL_RC' in output


def test_nested_workflow_commands(workflow, t_mod, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = SimpleNamespace(repos={'api': str(tmp_path / 'api')})
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    # Exercise the real update classifier: list must skip the prompt.
    monkeypatch.setattr(t_mod, '_t_offer_update', lambda: pytest.fail('list prompted for update'))
    assert t_mod.main(['config', 'workflow', 'list']) == 0
    output = capsys.readouterr().out
    assert 'Scope: global' in output and 'recommended defaults (not saved)' in output
    assert 'Enabled: yes' in output and all(word in output for word in ('STAGE', 'MODEL', 'EFFORT', 'planning', 'implementation', 'review'))
    assert not workflow.config_path().exists()
    for flags in ([], ['--repo', 'api']):
        assert t_mod.main(['config', 'workflow', 'list', *flags, '--json']) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['workflow'] == workflow.DEFAULT
        assert result['scope'] == (str(tmp_path / 'api') if flags else 'global')
        assert t_mod.main(['config', 'show', '--workflow', *flags]) == 0
        assert json.loads(capsys.readouterr().out) == result
    document = workflow.defaults()
    document['workflows']['standard']['enabled'] = False
    document['repositories'][str(tmp_path / 'api')] = {'inline': dict(workflow.DEFAULT)}
    workflow.save(document, None)
    assert t_mod.main(['config', 'workflow', 'list']) == 0
    assert 'Enabled: no' in capsys.readouterr().out
    assert t_mod.main(['config', 'workflow', 'list', '--repo', 'api']) == 0
    output = capsys.readouterr().out
    assert 'Source: repository override' in output and 'Enabled: yes' in output
    assert t_mod.main(['config', 'workflow', 'list', '--repo', 'missing']) == 1
    assert 'unknown registered repository' in capsys.readouterr().err
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: None)
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    seen = []
    monkeypatch.setattr(workflow, 'edit', lambda doc, repo: (seen.append(repo) or 19, doc))
    assert t_mod.main(['config', 'workflow', 'edit', '--repo', 'api']) == 19
    assert seen == [str(tmp_path / 'api')]
    assert not t_mod._command_readonly(['config', 'workflow', 'edit'])
    assert t_mod._command_readonly(['config', 'workflow', 'list', '--repo', 'api'])


def test_legacy_migration_is_read_only_and_sync_preserves_storage(workflow, tmp_path):
    w = workflow
    repo = str(tmp_path / 'api')
    policy = copy.deepcopy(w.DEFAULT)
    policy['implementation']['model'] = 'legacy-model'
    legacy = {'version': 1, 'global': policy, 'repositories': {repo: dict(policy, enabled=False)}}
    w.config_path().parent.mkdir(parents=True)
    w.config_path().write_text(json.dumps(legacy, separators=(',', ':')))
    before = w.snapshot(w.config_path())
    doc, _ = w.load()
    assert doc['version'] == 2 and doc['default'] == 'standard'
    assert w.effective(doc) == policy
    assert w.effective(doc, repo) == legacy['repositories'][repo]
    assert w.snapshot(w.config_path()) == before
    w.sync()
    assert w.snapshot(w.config_path()) == before
    w.save(w.action(doc, 'add', 'copy'), before)
    assert json.loads(w.config_path().read_text())['version'] == 2


def test_named_consumers_inline_exclusion_and_inheritance(workflow, tmp_path):
    w = workflow
    repo, other = (str(tmp_path / name) for name in ('api', 'other'))
    doc = w.action(w.defaults(), 'add', 'careful')
    assert doc['default'] == 'standard'
    doc = w.action(doc, 'use', 'careful', repo)
    inline = w.effective(doc, repo)
    doc = w.update(doc, inline, other)
    policy = copy.deepcopy(inline)
    policy['planning']['model'] = 'new-model'
    policy['instructions'] = '# Notes\n\n- Exact prose.\n'
    doc = w.update(doc, policy, name='careful')
    assert w.effective(doc, repo) == policy
    assert w.effective(doc, other) == inline
    doc = w.action(doc, 'exclude', repository=repo)
    assert doc['repositories'][repo] == {'workflow': 'careful', 'excluded': True}
    assert not w.effective(doc, repo)['enabled']
    assert w.resolved(doc, repo) == policy
    rendered = w.render(doc).split('Repository scope: ')[1]
    assert 'Do not apply inherited' in rendered and 'Exact prose' not in rendered
    doc = w.action(doc, 'use', 'careful', repo)
    assert w.effective(doc, repo)['enabled']
    doc = w.action(doc, 'exclude', repository=other)
    doc = w.update(doc, dict(inline, instructions='local notes'), other)
    assert doc['repositories'][other]['excluded']
    assert not w.effective(doc, other)['enabled']
    assert w.action(doc, 'inherit', repository=other)['repositories'].get(other) is None
    assert w.catalogue(doc)['workflows']['careful']['repositories'] == [repo]
    doc = w.action(doc, 'use', 'careful')
    third = str(tmp_path / 'third')
    doc = w.action(doc, 'exclude', repository=third)
    assert w.source(doc, third) == 'careful'
    assert third in w.catalogue(doc)['workflows']['careful']['excluded_repositories']


@pytest.mark.parametrize('mutate,match', [
    (lambda d: d.update(default='missing'), 'existing'),
    (lambda d: d.update(workflows={}), 'nonempty'),
    (lambda d: d['workflows'].update({'bad name': copy.deepcopy(DEFAULT_POLICY)}), 'workflow name'),
    (lambda d: d['repositories'].update({'/api': {'workflow': 'missing'}}), 'existing'),
    (lambda d: d['repositories'].update({'/api': {'workflow': 'standard', 'inline': {}}}), 'one workflow'),
    (lambda d: d['repositories'].update({'/api': {}}), 'one workflow'),
    (lambda d: d['repositories'].update({'/api': {'excluded': False}}), 'excluded'),
    (lambda d: d['workflows']['standard'].update(instructions=123), 'Markdown'),
    (lambda d: d['workflows']['standard'].update(instructions='<!-- t agent-workflow: end -->'), 'markers'),
])
def test_named_schema_rejects_invalid_sources(workflow, mutate, match):
    doc = workflow.defaults()
    mutate(doc)
    with pytest.raises(ValueError, match=match):
        workflow.validate(doc)


DEFAULT_POLICY = {'enabled': True, 'planning': {'model': 'test', 'effort': 'auto'},
                  'implementation': {'model': 'test', 'effort': 'auto'},
                  'review': {'model': 'test', 'effort': 'one-level-lower'}}


def test_named_workflow_commands(workflow, t_mod, monkeypatch, tmp_path, capsys):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    cfg = SimpleNamespace(repos={'api': str(tmp_path / 'api')})
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: None)
    def run(*words):
        return t_mod.main(['config', 'workflow', *words])
    assert run('add', 'quick') == 0
    assert w.load()[0]['default'] == 'standard'
    assert run('use', 'quick', '--repo', 'api') == 0
    assert run('exclude', '--repo', 'api') == 0
    assert run('list', '--repo', 'api', '--json') == 0
    output = capsys.readouterr().out
    assert '"excluded": true' in output and '"workflow_name": "quick"' in output
    assert run('list', '--all') == 0
    assert 'REPOSITORY ASSIGNMENTS' in capsys.readouterr().out
    assert run('list', '--all', '--json') == 0
    catalogue = json.loads(capsys.readouterr().out)
    assert catalogue['workflows']['quick']['repositories'] == [cfg.repos['api']]
    before = w.snapshot(w.config_path())
    for words in [('add', 'quick'), ('add', 'bad name'), ('use', 'missing'),
                  ('add', 'new', '--from', 'missing'), ('list', '--all', '--repo', 'api'),
                  ('edit', 'quick', '--repo', 'api')]:
        assert run(*words) == 1
        assert w.snapshot(w.config_path()) == before
    for words in [('exclude',), ('inherit',), ('use',), ('add',)]:
        with pytest.raises(SystemExit):
            run(*words)
    assert run('use', 'quick', '--repo', 'api') == 0
    assert not w.load()[0]['repositories'][cfg.repos['api']].get('excluded')
    assert run('use', 'quick') == 0
    assert run('inherit', '--repo', 'api') == 0
    assert not w.load()[0]['repositories']
    before = w.snapshot(w.config_path())
    assert run('use', 'quick') == 0
    assert w.snapshot(w.config_path()) == before
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    def editor(argv):
        path = Path(argv[-1])
        policy = json.loads(path.read_text())
        policy['planning']['model'] = 'new-model'
        path.write_text(w.encode(policy))
        return 0
    monkeypatch.setattr(w.subprocess, 'call', editor)
    assert run('edit', 'quick') == 0
    assert w.load()[0]['workflows']['quick']['planning']['model'] == 'new-model'


def test_instructions_commands(workflow, t_mod, monkeypatch, tmp_path, capsys):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    cfg = SimpleNamespace(repos={'api': str(tmp_path / 'api')})
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: None)
    monkeypatch.setattr(t_mod, '_config_live_models', lambda _: pytest.fail('unexpected model lookup'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    prose = '# Human instructions\n\n- Keep this spacing.  \n\nParagraph without final newline'
    def editor(argv):
        path = Path(argv[-1])
        assert path.suffix == '.md' and stat.S_IMODE(path.stat().st_mode) == 0o600
        path.write_text(prose)
        return 0
    monkeypatch.setattr(w.subprocess, 'call', editor)
    assert t_mod.main(['config', 'workflow', 'instructions', 'edit']) == 0
    capsys.readouterr()
    assert t_mod.main(['config', 'workflow', 'instructions', 'show']) == 0
    assert capsys.readouterr().out == prose
    assert prose in native_paths()[0].read_text()
    assert t_mod.main(['config', 'workflow', 'exclude', '--repo', 'api']) == 0
    capsys.readouterr()
    assert t_mod.main(['config', 'workflow', 'instructions', 'show', '--repo', 'api']) == 0
    assert capsys.readouterr().out == prose
    before = w.snapshot(w.config_path())
    assert t_mod.main(['config', 'workflow', 'instructions', 'edit', '--repo', 'api']) == 0
    assert w.snapshot(w.config_path()) == before
    assert t_mod.main(['config', 'workflow', 'instructions', 'show', 'standard', '--repo', 'api']) == 1
    assert t_mod.main(['config', 'workflow', 'instructions', 'show', 'missing']) == 1
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: Path(argv[-1]).write_text('local') and 0)
    assert t_mod.main(['config', 'workflow', 'instructions', 'edit', '--repo', 'api']) == 0
    doc = w.load()[0]
    assert doc['repositories'][cfg.repos['api']]['excluded']
    assert doc['repositories'][cfg.repos['api']]['inline']['enabled']
    assert doc['repositories'][cfg.repos['api']]['inline']['instructions'] == 'local'
    assert doc['workflows']['standard']['instructions'] == prose
    assert 'local' not in w.render(doc).split('Repository scope: ')[1]
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 17)
    assert w.edit_instructions(doc) == (17, doc)
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: Path(argv[-1]).unlink() or 0)
    with pytest.raises(ValueError, match='removed instructions'):
        w.edit_instructions(doc)
    monkeypatch.setenv('VISUAL', ' ')
    with pytest.raises(ValueError, match='set VISUAL'):
        w.edit_instructions(doc)


def test_workflow_direct_action_validation(workflow):
    w = workflow
    for name in ['', '-bad', 'bad name', 'a' * 65, None]:
        with pytest.raises(ValueError, match='workflow name'):
            w.validate_name(name)
    for verb in ['exclude', 'inherit', 'unknown']:
        with pytest.raises(ValueError):
            w.action(w.defaults(), verb)
    for fn in [w.edit, w.instructions]:
        with pytest.raises(ValueError, match='cannot be combined'):
            fn(w.defaults(), '/repo', 'standard')
        with pytest.raises(ValueError, match='unknown workflow'):
            fn(w.defaults(), name='missing')
    with pytest.raises(ValueError, match='unknown workflow'):
        w.update(w.defaults(), w.DEFAULT, name='missing')


def test_workflow_stage_controls_preserve_launch_settings(workflow, t_mod, monkeypatch, tmp_path):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    cfg = SimpleNamespace(repos={'api': str(tmp_path / 'api')}, models={'codex': 'launch'}, efforts={'codex': 'high'})
    discoveries = []
    def discover(agent):
        discoveries.append(agent)
        if len(discoveries) == 1:
            raise OSError('offline')
        return [{'model': 'catalog-model', 'supportedReasoningEfforts': []}]
    monkeypatch.setattr(t_mod, '_config_live_models', discover)
    ui = Menu(['customize', 'planning', 'model', '__custom__', 'planning', 'effort', 'ultra',
               'implementation', 'model', '__refresh__', 'catalog-model', 'review', 'effort', 'low',
               'enabled', '0', 'back', 'back'], inputs=['custom-model'])
    t_mod._config_workflow(ui, cfg)
    policy = cfg._workflow_document['workflows']['standard']
    assert policy['planning'] == {'model': 'custom-model', 'effort': 'ultra'}
    assert policy['implementation'] == {'model': 'catalog-model', 'effort': 'auto'}
    assert policy['review']['effort'] == 'low' and not policy['enabled']
    assert cfg.models == {'codex': 'launch'} and cfg.efforts == {'codex': 'high'}
    assert discoveries == ['codex', 'codex']
    assert t_mod._workflow_pending(cfg) and not w.config_path().exists()
    ui = Menu(['choose', 'name:standard', 'customize', 'planning', 'model', 'custom-model',
               'planning', 'effort', 'ultra', 'back', 'back'])
    t_mod._config_workflow(ui, cfg, 'api')
    # Explicit assignment is an override; unchanged stage choices do not turn it into an inline copy.
    assert cfg._workflow_document['repositories'][cfg.repos['api']] == {'workflow': 'standard'}


def test_workflow_named_menu_and_instructions_staging(workflow, t_mod, monkeypatch, tmp_path):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    cfg = SimpleNamespace(repos={'api': str(tmp_path / 'api')})
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: Path(argv[-1]).write_text('# Notes\n') and 0)
    ui = Menu(['add', 'instructions', 'back', 'manage', 'name:careful', 'back',
               'choose', 'name:careful', 'back'], inputs=['careful'])
    t_mod._config_workflow(ui, cfg)
    assert cfg._workflow_document['default'] == 'careful'
    assert cfg._workflow_document['workflows']['careful']['instructions'] == '# Notes\n'
    assert not w.config_path().exists()
    assert '# Notes' in '\n'.join(t_mod._workflow_review(cfg._workflow_document))
    ui = Menu(['customize', 'instructions', 'back', 'back'])
    t_mod._config_workflow(ui, cfg, 'api')
    assert not cfg._workflow_document['repositories']  # same text does not create an override
    ui = Menu(['add', 'choose', None, 'manage', 'back', 'back'], inputs=['bad name'])
    t_mod._config_workflow(ui, cfg)


@pytest.mark.parametrize('source', ['inline', 'named', 'inherited'])
def test_repository_menu_resumes_preserved_workflow(workflow, t_mod, monkeypatch, tmp_path, source):
    w = workflow
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(w))
    repository = str(tmp_path / 'api')
    cfg = SimpleNamespace(repos={'api': repository})
    t_mod._workflow_session(cfg)
    document = cfg._workflow_document
    if source == 'inline':
        policy = dict(copy.deepcopy(w.DEFAULT), instructions='# Local notes', enabled=False)
        policy['planning']['model'] = 'local-model'
        document = w.update(document, policy, repository)
    elif source == 'named':
        document = w.action(document, 'use', 'standard', repository)
    cfg._workflow_document = w.action(document, 'exclude', repository=repository)
    t_mod._config_workflow(Menu(['exclude', 'back']), cfg, 'api')
    assert cfg._workflow_document == document
    assert not w.config_path().exists()
    if source == 'inline':
        assert 'Workflow: disabled' in '\n'.join(t_mod._workflow_review(document))


def test_instructions_preserve_crlf_and_absent_final_newline(workflow, monkeypatch):
    w = workflow
    body = b'# Exact\r\n\r\n- item  \r\nlast line'
    def editor(argv):
        Path(argv[-1]).write_bytes(body)
        return 0
    monkeypatch.setattr(w.subprocess, 'call', editor)
    rc, document = w.edit_instructions(w.defaults())
    assert rc == 0
    assert document['workflows']['standard']['instructions'].encode() == body
    w.save(document, None)
    assert w.instructions(w.load()[0]).encode() == body
    assert body in native_paths()[0].read_bytes()
    assert w.edit_instructions(document) == (0, document)
