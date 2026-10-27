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
    assert document['global']['planning']['model'] == 'gpt-6-astra'
    repository = str(tmp_path.resolve() / 'repo')
    assert w.effective(document, repository) == document['global']
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
    (lambda d: d.update(version=2), 'version'),
    (lambda d: d.update(global_=None), 'requires version'),
    (lambda d: d.update(repositories=[]), 'repositories'),
    (lambda d: d['repositories'].update({'relative': {}}), 'canonical absolute'),
    (lambda d: d['global'].pop('enabled'), 'requires enabled'),
    (lambda d: d['global'].update(enabled='yes'), 'true or false'),
    (lambda d: d['global']['planning'].update(extra=1), 'requires model'),
    (lambda d: d['global']['planning'].update(model='x\n## override'), 'model ID'),
    (lambda d: d['global']['planning'].update(model='--bad'), 'model ID'),
    (lambda d: d['global']['implementation'].update(effort='one-level-lower'), 'effort'),
    (lambda d: d['global']['review'].update(effort='auto'), 'effort'),
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
    explicit['global']['planning']['effort'] = 'xhigh'
    explicit['global']['implementation']['effort'] = 'high'
    explicit['global']['review']['effort'] = 'medium'
    assert 'reasoning effort: xhigh' in w.render(explicit)


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
    assert rc == 0 and changed['global']['planning']['model'] == 'custom-v1'
    assert document == w.defaults() and not w.config_path().exists()
    assert not Path(calls[0][-1]).exists()
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 17)
    assert w.edit(document) == (17, document)
    monkeypatch.setattr(w.subprocess, 'call', lambda argv: 0)
    assert w.edit(document) == (0, document)
    repository = str(tmp_path.resolve() / 'repo')
    assert w.edit(document, repository) == (0, document)
    changed = w.update(document, changed['global'], repository)
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
    assert w.load()[0]['global']['enabled'] is False
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
    choices = ['workflow', 'defaults', 'back', 'repos', 'api', 'workflow', 'enabled', '0', 'back', 'back', '__back__']
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
        assert document['global'] == w.DEFAULT
        assert document['repositories'][cfg.repos['api']]['enabled'] is False
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
    ui = Menu(['enabled', '0', 'inherit', 'back'])
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
    document['global']['planning']['model'] = 'chosen-model'
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
