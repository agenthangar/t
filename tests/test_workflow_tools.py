"""Tool catalogues remain isolated without rewriting legacy workflow storage."""
import copy

import pytest

from test_workflow import workflow  # noqa: F401 -- sandbox account paths


def test_tool_views_preserve_legacy_document_without_materializing_defaults(workflow):
    document = workflow.defaults()
    document['workflows']['standard']['instructions'] = 'Codex-only instructions'
    document['workflows']['standard']['behaviors'] = {'pr_open': 'automatic'}
    before = copy.deepcopy(document)
    encoded = workflow.encode(document)
    for tool in workflow.TOOLS:
        view = workflow.tool_document(document, tool)
        assert set(view) == {'version', 'default', 'workflows', 'repositories'}
        assert view.tool == tool
        assert workflow.with_tool_document(document, tool, view) == document
        if tool != 'codex':
            policy = workflow.effective(view)
            assert not policy['stages_enabled']
            assert all(policy[stage]['model'] == '' for stage in workflow.MODEL_STAGES)
            assert not policy.get('behaviors')
            assert 'Codex-only instructions' not in workflow.render(document, tool)
            assert 'gpt-' not in workflow.render(document, tool)
    assert document == before and workflow.encode(document) == encoded
    assert 'tools' not in document and not workflow.config_path().exists()


def test_tool_catalogue_edits_and_assignments_are_independent(workflow, tmp_path):
    original = workflow.defaults()
    repository = str(tmp_path.resolve() / 'repo')
    claude = workflow.action(workflow.tool_document(original, 'claude'), 'add', 'careful')
    policy = workflow.scoped(claude, name='careful')
    policy['session_defaults'] = {'claude': {'model': 'sonnet', 'effort': 'high'}}
    claude = workflow.update(claude, policy, name='careful')
    claude = workflow.action(claude, 'use', 'careful', repository)
    after = workflow.with_tool_document(original, 'claude', claude)
    assert workflow.tool_document(after, 'codex') == original
    assert workflow.tool_document(after, 'cursor') == workflow.tool_document(original, 'cursor')
    assert workflow.source(workflow.tool_document(after, 'claude'), repository) == 'careful'
    assert workflow.main_session_settings(workflow.effective(claude, repository), 'claude') == {'model': 'sonnet', 'effort': 'high'}
    assert 'tools' not in original
    codex = workflow.tool_document(after, 'codex')
    codex['workflows']['standard']['planning']['model'] = 'changed-codex'
    merged = workflow.with_tool_document(after, 'codex', codex)
    assert merged['tools'] == after['tools']
    assert workflow.validate(merged) == merged


@pytest.mark.parametrize('tools', [None, [], {'codex': {}}, {'unknown': {}},
    {'claude': {}}, {'claude': {'default': 'missing', 'workflows': {}, 'repositories': {}}}])
def test_invalid_tool_catalogue_schema_rejected(workflow, tools):
    document = workflow.defaults()
    document['tools'] = tools
    with pytest.raises(ValueError):
        workflow.validate(document)


def test_tool_catalogue_rejects_nesting_and_unknown_tools(workflow):
    document = workflow.defaults()
    scoped = workflow.tool_document(document, 'claude')
    scoped['tools'] = {}
    with pytest.raises(ValueError, match='nested tools'):
        workflow.with_tool_document(document, 'claude', scoped)
    with pytest.raises(ValueError, match='workflow tool'):
        workflow.tool_document(document, 'other')


def test_tool_handoffs_preserve_legacy_snapshot_guards(workflow, tmp_path):
    document = workflow.WorkflowDocument(workflow.defaults())
    document.legacy_snapshots = {tmp_path / 'AGENTS.md': ('original', (1, 2, 3))}
    view = workflow.tool_document(document, 'claude')
    assert view.legacy_snapshots == document.legacy_snapshots
    changed = workflow.action(view, 'add', 'second')
    assert changed.tool == 'claude'
    assert changed.legacy_snapshots == document.legacy_snapshots
    merged = workflow.with_tool_document(document, 'claude', changed)
    assert merged.legacy_snapshots == document.legacy_snapshots
    assert not hasattr(merged, 'tool')
    assert 'legacy_snapshots' not in workflow.encode(merged)


def test_empty_stage_models_inherit_selected_tool_session_defaults(workflow):
    policy = workflow.effective(workflow.tool_document(workflow.defaults(), 'claude'))
    policy = workflow.select_model_stages(policy, ['planning', 'implementation', 'review'])
    policy['session_defaults'] = {'claude': {'model': 'claude-model', 'effort': 'low'},
                                  'codex': {'model': 'private-codex-model'}}
    assert workflow.validate_policy(policy) == policy
    assert workflow.main_session_settings(policy, 'claude') == {'model': 'claude-model', 'effort': 'medium'}
    assert workflow.stage_settings(policy, 'planning', 'claude') == {'model': 'claude-model', 'effort': 'auto'}
    text = workflow._stage_text(policy, 'claude')
    assert 'claude-model' in text and 'private-codex-model' not in text
    policy['session_defaults'].pop('claude')
    assert workflow.main_session_settings(policy, 'claude') == {'effort': 'medium'}
    assert workflow.stage_settings(policy, 'planning', 'claude')['model'] == ''
    assert 'using the current session model' in workflow._stage_text(policy, 'claude')
    assert 'current-native-model' not in workflow._stage_text(policy, 'claude')


@pytest.mark.parametrize('model', ['composer-2[thinking=true]', 'claude-opus-4.6(high)',
                                  'custom/model[context=large,reasoning=high]'])
def test_cursor_parameterized_models_do_not_emit_separate_effort(workflow, model):
    policy = workflow.effective(workflow.tool_document(workflow.defaults(), 'cursor'))
    policy['session_defaults'] = {'cursor': {'model': model}}
    assert workflow.validate_policy(policy) == policy
    assert workflow.main_session_settings(policy, 'cursor') == {'model': model}
    selected = workflow.select_model_stages(policy, ['implementation'])
    selected['implementation']['model'] = model
    assert workflow.validate_policy(selected) == selected
    assert workflow.main_session_settings(selected, 'cursor') == {'model': model}
    policy['session_defaults']['cursor']['effort'] = 'high'
    with pytest.raises(ValueError, match='Cursor reasoning'):
        workflow.validate_policy(policy)


def test_native_publications_render_each_tool_without_cross_tool_models(workflow):
    document = workflow.defaults()
    document['workflows']['standard']['implementation']['model'] = 'codex-exclusive'
    claude = workflow.tool_document(document, 'claude')
    policy = workflow.select_model_stages(workflow.effective(claude), ['implementation'])
    policy['implementation']['model'] = 'claude-exclusive'
    claude = workflow.update(claude, policy)
    document = workflow.with_tool_document(document, 'claude', claude)
    changes = workflow.native_changes(document)
    codex_text, claude_text = changes[0][2], changes[1][2]
    assert 'codex-exclusive' in codex_text and 'claude-exclusive' not in codex_text
    assert 'claude-exclusive' in claude_text and 'codex-exclusive' not in claude_text
    assert workflow.render(claude, 'claude') == workflow.render(document, 'claude')
    assert not any(path.exists() for path in workflow.native_paths())


def test_saved_legacy_claude_preferences_migrate_read_only_then_freeze(workflow, tmp_path):
    document = workflow.defaults()
    document['workflows']['standard']['instructions'] = 'Shared delivery instructions.'
    document['workflows']['standard']['session_defaults'] = {'claude': {'model': 'saved-claude', 'effort': 'high'},
                                                            'codex': {'model': 'saved-codex'}}
    document['workflows']['standard']['permissions'] = {'claude': 'plan', 'codex': 'full'}
    document['workflows']['standard']['behaviors'] = {'pr_open': 'on_request'}
    document['workflows']['standard']['runners'] = copy.deepcopy(workflow.RUNNER_DEFAULT)
    document['repositories'][str(tmp_path.resolve() / 'repo')] = {'workflow': 'standard', 'excluded': True}
    path = workflow.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(workflow.encode(document))
    raw = path.read_bytes()
    loaded, before = workflow.load()
    claude = workflow.tool_document(loaded, 'claude')
    policy = workflow.effective(claude)
    assert policy['instructions'] == 'Shared delivery instructions.'
    assert policy['session_defaults'] == {'claude': {'model': 'saved-claude', 'effort': 'high'}}
    assert policy['permissions'] == {'claude': 'plan'}
    assert policy['behaviors'] == {'pr_open': 'on_request'}
    assert policy['runners'] == workflow.RUNNER_DEFAULT
    assert claude['repositories'] == document['repositories']
    assert not policy['stages_enabled']
    assert all(policy[stage]['model'] == '' for stage in workflow.MODEL_STAGES)
    assert path.read_bytes() == raw
    codex = workflow.tool_document(loaded, 'codex')
    codex['workflows']['standard']['instructions'] = 'Changed Codex only.'
    changed = workflow.with_tool_document(loaded, 'codex', codex)
    workflow.save(changed, before, propagate=False)
    saved, _ = workflow.load()
    assert workflow.tool_document(saved, 'claude') == claude
    assert saved['workflows']['standard']['instructions'] == 'Changed Codex only.'


def test_save_without_migrated_view_freezes_original_legacy_claude_policy(workflow):
    original = workflow.defaults()
    original['workflows']['standard']['instructions'] = 'Original shared text.'
    path = workflow.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(workflow.encode(original))
    before = workflow.snapshot(path)
    changed = copy.deepcopy(original)
    changed['workflows']['standard']['instructions'] = 'New Codex text.'
    workflow.save(changed, before, propagate=False)
    saved, _ = workflow.load()
    assert workflow.effective(workflow.tool_document(saved, 'claude'))['instructions'] == 'Original shared text.'


def test_tool_runner_guidance_keeps_selected_catalogue(workflow):
    policy = workflow.effective(workflow.tool_document(workflow.defaults(), 'claude'))
    policy['runners'] = copy.deepcopy(workflow.RUNNER_DEFAULT)
    policy['runners']['targets'] = {'linux': {'provider': 'ssh', 'host': 'worker', 'os': 'linux', 'arch': 'x64', 'capabilities': []}}
    text = workflow._stage_text(policy, 'claude')
    assert '`t runners list --tool claude`' in text
    assert '`t runners runs start --tool claude -- COMMAND ...`' in text
    assert '`t runners runs status ID`' in text
    assert 'Read/edit prose: t workflow instructions show --tool claude / edit --tool claude' in workflow.summary_lines(policy, 'claude')
