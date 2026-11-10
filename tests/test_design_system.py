"""Repository evidence, independent source selection, and explicit drafting."""
import copy
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_workflow import workflow, Menu  # noqa: F401
from test_workflow_tool_ui import ToolMenu, config
from test_tui import make_ui

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def design():
    spec = importlib.util.spec_from_file_location('design_test', ROOT / 'libexec/t_design_system.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    return root.resolve()


def put(root, path, text=''):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def commit(root, message):
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', message], check=True)


def test_refresh_finds_merged_design_without_moving_stale_checkouts(design, repo, tmp_path, monkeypatch):
    put(repo, 'README.md', '# Application')
    commit(repo, 'initial')
    remote, registered, linked = (tmp_path / name for name in ('remote.git', 'registered', 'session'))
    subprocess.run(['git', 'clone', '-q', '--bare', str(repo), str(remote)], check=True)
    subprocess.run(['git', 'clone', '-q', str(remote), str(registered)], check=True)
    subprocess.run(['git', '-C', str(registered), 'worktree', 'add', '-qb', 'session', str(linked)], check=True)
    put(linked, 'README.md', 'Uncommitted session work')
    before = design.git(registered, 'show-ref')
    put(repo, 'DESIGN.md', '# Design system\n[Design guidance](ui/README.md)')
    put(repo, 'ui/README.md', '# Terminal guidance')
    commit(repo, 'merged design guidance')
    subprocess.run(['git', '-C', str(repo), 'push', '-q', str(remote), 'HEAD'], check=True)
    monkeypatch.chdir(linked)
    assert design.discover(design.workspace(registered))['candidates'] == []
    result = design.refresh(registered)
    assert design.automatic_source(result) == 'DESIGN.md'
    assert {item['path'] for item in result['candidates']} == {'DESIGN.md', 'ui/README.md'}
    assert all(item['location'] == 'Default branch · origin/HEAD' for item in result['candidates'])
    assert result['warnings'] == []
    assert design.git(registered, 'show-ref') == before
    assert not (registered / '.git/FETCH_HEAD').exists()
    assert not (linked / 'DESIGN.md').exists() and not (registered / 'DESIGN.md').exists()
    assert (linked / 'README.md').read_text() == 'Uncommitted session work'


def test_refresh_includes_canonical_but_keeps_active_source_first(design, repo, tmp_path, monkeypatch):
    put(repo, 'DESIGN.md', '# Design system')
    commit(repo, 'initial')
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-qb', 'session', str(linked)], check=True)
    put(repo, 'design-system.md', '# More guidance')
    monkeypatch.chdir(linked)
    result = design.refresh(repo)
    assert len(result['candidates']) == 2
    assert 'location' not in result['candidates'][0]
    assert result['candidates'][1]['location'] == 'Registered checkout'


@pytest.mark.parametrize('failure', ['offline', 'no-head', 'timeout'])
def test_refresh_remote_failure_keeps_local_evidence(design, repo, monkeypatch, failure):
    put(repo, 'DESIGN.md', '# Design system')
    original = design.git
    def git(root, *args):
        if args == ('remote',):
            return b'origin\n'
        if args[0] == 'ls-remote':
            if failure == 'offline':
                raise ValueError('offline')
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('git', 10)
            return b''
        return original(root, *args)
    monkeypatch.setattr(design, 'git', git)
    result = design.refresh(repo)
    assert design.automatic_source(result) == 'DESIGN.md'
    assert 'Default-branch scan unavailable' in result['warnings'][0]


def test_revision_scan_excludes_symlinks_dependencies_and_bounds_reads(design, repo, monkeypatch):
    put(repo, 'DESIGN.md', '# Design system')
    put(repo, 'large.md', 'x' * 100)
    put(repo, 'node_modules/DESIGN.md', '# Design system')
    put(repo, 'ui/tokens.json', '{}')
    put(repo, 'ui/components/Button.ts', '')
    (repo / 'design-system.md').symlink_to('DESIGN.md')
    commit(repo, 'fixture')
    monkeypatch.setattr(design, 'MAX_BYTES', 50)
    result = design.discover_revision(repo, 'HEAD')
    assert {item['path'] for item in result['candidates']} == {'DESIGN.md', 'ui'}
    assert any('too large' in warning for warning in result['warnings'])
    monkeypatch.setattr(design, 'MAX_FILES', 1)
    assert any('limited to 1 files' in warning for warning in design.discover_revision(repo, 'HEAD')['warnings'])


def test_refresh_menu_selects_merged_source_without_duplicate_rows(workflow, t_mod, monkeypatch, repo):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    cfg._workflow_document = workflow.select_design_system(cfg._workflow_document, str(repo), 'DESIGN.md')
    helper = vars(workflow).copy()
    design = workflow.design_system()
    refreshed = {'candidates': [{'path': 'DESIGN.md', 'evidence': ['Design-system entry point'],
                                'location': 'Default branch · origin/HEAD'}], 'warnings': []}
    helper['design_system'] = lambda: dict(design, refresh=lambda root: refreshed)
    ui = ToolMenu(['refresh', 'candidate:0'])
    t_mod._workflow_design_system_menu(ui, cfg, helper, 'web', str(repo))
    assert [label for _, label in ui.views[-1][1]].count('DESIGN.md') == 1
    assert 'origin/HEAD' in ui.views[-1][2]['values']['candidate:0']
    assert cfg._workflow_document['repositories'][str(repo)]['design_system'] == {'path': 'DESIGN.md'}
    assert not workflow.config_path().exists()


def test_refresh_menu_reports_failure_and_can_cancel(workflow, t_mod, monkeypatch, repo):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    helper = vars(workflow).copy()
    design = workflow.design_system()
    helper['design_system'] = lambda: dict(design, refresh=lambda root: (_ for _ in ()).throw(ValueError('offline')))
    ui = ToolMenu(['refresh', 'back'])
    t_mod._workflow_design_system_menu(ui, cfg, helper, 'web', str(repo))
    assert not cfg._workflow_document['repositories']


def test_discovery_evidence_and_exclusions(design, repo):
    put(repo, 'DESIGN.md', '# Design system\n[Terminal contract](ui/README.md)')
    put(repo, 'AGENTS.md', '[Design](DESIGN.md)\n[Design](https://example.com)\n[Design](../outside.md)')
    put(repo, 'ui/README.md', '# Command and terminal contract')
    put(repo, 'packages/ui/tokens.json', '{}')
    put(repo, 'packages/ui/components/Button.ts', 'export {}')
    put(repo, 'packages/ui/.storybook/main.ts', '')
    put(repo, 'src/components/App.ts', 'export {}')
    put(repo, 'node_modules/pkg/DESIGN.md', '# Design system')
    put(repo, 'generated/DESIGN.md', '# Design system')
    put(repo, '.gitignore', 'generated/\n')
    put(repo, 'outside-link.md', 'no')
    (repo / 'symlink').symlink_to(repo.parent, target_is_directory=True)
    result = design.discover(repo)
    by_path = {item['path']: item['evidence'] for item in result['candidates']}
    assert set(by_path) == {'DESIGN.md', 'ui/README.md', 'packages/ui'}
    assert 'Linked from AGENTS.md' in by_path['DESIGN.md']
    assert 'Storybook configuration' in by_path['packages/ui']
    assert result['candidates'][0]['path'] == 'DESIGN.md'
    assert result['warnings'] == []


@pytest.mark.parametrize('value', [None, {}, {'path': ''}, {'path': '../private'}, {'path': '/etc/passwd'},
    {'path': 'a/../b'}, {'path': './ui'}, {'path': 'ui//README.md'}, {'path': 'node_modules/ui'},
    {'path': 'a\\b'}, {'path': 'x\ny'}, {'path': 3}, {'path': 'x', 'name': 'x'}, {'path': 'a' * 1025}])
def test_invalid_source_schema(design, value):
    with pytest.raises(ValueError, match='design system'):
        design.validate_source(value)


def test_paths_and_linked_worktree(design, repo, tmp_path):
    put(repo, 'DESIGN.md', '# Design system')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'fixture'], check=True)
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-qb', 'test', str(linked)], check=True)
    assert design.workspace(repo, linked) == linked
    assert design.workspace(repo, tmp_path) == repo
    assert design.workspace(repo, repo) == repo
    other = tmp_path / 'other'
    other.mkdir()
    subprocess.run(['git', 'init', '-q', str(other)], check=True)
    assert design.workspace(repo, other) == repo
    (repo / 'link').symlink_to(linked, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        design.source_path(repo, 'link/DESIGN.md')
    with pytest.raises(ValueError, match='missing'):
        design.source_path(repo, 'missing.md')


def test_discovery_limits_and_empty(design, repo, monkeypatch):
    assert design.discover(repo)['candidates'] == []
    put(repo, 'a.md', '# Design system')
    put(repo, 'b.md', '# Design system')
    monkeypatch.setattr(design, 'MAX_FILES', 1)
    monkeypatch.setattr(design, 'MAX_READS', 1)
    result = design.discover(repo)
    assert len(result['warnings']) == 2 and len(result['candidates']) == 1
    monkeypatch.setattr(design, 'git', lambda *args: b'a.md\0gone.md\0ui\0')
    monkeypatch.setattr(design, 'MAX_FILES', 10)
    (repo / 'ui').mkdir()
    monkeypatch.setattr(Path, 'open', lambda *a, **k: (_ for _ in ()).throw(OSError('cannot read')))
    assert 'Cannot read a.md.' in design.discover(repo)['warnings']


def test_selected_source_survives_assignment_and_isolation(workflow, repo):
    w = workflow
    initial = w.defaults()
    document = w.select_design_system(initial, str(repo), 'DESIGN.md')
    assert initial['repositories'] == {}
    document = w.action(document, 'add', 'other')
    for action, name in [('use', 'other'), ('exclude', None), ('include', None), ('inherit', None)]:
        document = w.action(document, action, name, str(repo))
        assert document['repositories'][str(repo)]['design_system'] == {'path': 'DESIGN.md'}
    document = w.update(document, w.DEFAULT, str(repo))
    document = w.update(document, None, str(repo))
    assert document['repositories'][str(repo)] == {'design_system': {'path': 'DESIGN.md'}}
    full = w.with_tool_document(initial, 'cursor', document)
    assert w.tool_document(full, 'codex')['repositories'] == {}
    assert w.tool_document(full, 'cursor')['repositories'] == document['repositories']
    assert w.select_design_system(document, str(repo), None)['repositories'] == {}
    assigned = w.action(document, 'use', 'standard', str(repo))
    assert w.select_design_system(assigned, str(repo), None)['repositories'][str(repo)] == {'workflow': 'standard'}


def test_source_binding_does_not_count_as_explicit_assignment(workflow, repo):
    document = workflow.select_design_system(workflow.defaults(), str(repo), 'DESIGN.md')
    assert workflow.catalogue(document)['workflows']['standard']['assignment_count'] == 0
    assigned = workflow.action(document, 'use', 'standard', str(repo))
    assert workflow.catalogue(assigned)['workflows']['standard']['assignment_count'] == 1
    excluded = workflow.action(assigned, 'exclude', repository=str(repo))
    assert workflow.catalogue(excluded)['workflows']['standard']['assignment_count'] == 0


def test_discovery_never_runs_repository_fsmonitor(design, repo):
    put(repo, 'DESIGN.md', '# Design system')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    hook = put(repo, '.git/fsmonitor', '#!/bin/sh\nprintf ran > "$0.ran"\n')
    hook.chmod(0o755)
    subprocess.run(['git', '-C', str(repo), 'config', 'core.fsmonitor', str(hook)], check=True)
    assert design.discover(repo)['candidates'][0]['path'] == 'DESIGN.md'
    assert not Path(str(hook) + '.ran').exists()


@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
def test_guidance_independent_of_previews_and_stages(workflow, repo, tool):
    w = workflow
    document = w.tool_defaults(tool)
    policy = document['workflows']['standard']
    policy.update(stages_enabled=False, behaviors={'design_system': 'repo', 'frontend_enabled': 'disabled',
        'cli_enabled': 'disabled', 'cli_planning_preview': 'before_implementation', 'cli_planning_approval': 'required'})
    document = w.select_design_system(document, str(repo), 'ui/README.md')
    rendered = w.render(document, tool)
    assert 'Selected repository design-system source: "ui/README.md"' in rendered
    assert 'CLI planning preview:' not in rendered
    assert 'terminal interfaces' in rendered
    disabled = w.action(document, 'exclude', repository=str(repo))
    assert 'Selected repository design-system source:' not in w.render(disabled, tool)
    assert disabled['repositories'][str(repo)]['design_system'] == {'path': 'ui/README.md'}


def test_planning_preview_does_not_change_working_preview(workflow):
    policy = workflow.update_behavior(workflow.DEFAULT, 'cli_planning_preview', 'on_request')
    policy = workflow.update_behavior(policy, 'cli_planning_approval', 'required')
    policy = workflow.update_behavior(policy, 'cli_preview_timing', 'when_ready')
    text = workflow._stage_text(policy)
    assert 'only when explicitly requested' in text
    assert 'This choice alone does not request a preview' in text
    assert 'When the work is ready to try' in text
    assert workflow.update_behavior(policy, 'cli_planning_preview', '')['behaviors'].get('cli_planning_preview') is None


def test_workflow_editor_stages_design_and_planning_independently(workflow, t_mod):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={
        'cli_preview_timing': 'when_ready', 'cli_launch': 'on_request'})
    ui = ToolMenu(['local_preview', 'cli', 'cli_planning_preview', 'before_implementation',
                   'cli_planning_approval', 'required', 'back', 'back', 'back'])
    changed = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert changed['behaviors'] == dict(original['behaviors'],
        cli_planning_preview='before_implementation', cli_planning_approval='required')
    assert 'frontend_design' not in t_mod._WORKFLOW_PREVIEW_SUBMENUS['frontend'][1]
    assert 'design_system' not in t_mod._WORKFLOW_PREVIEW_SUBMENUS['frontend'][1]
    assert not workflow.config_path().exists()


def ui_config(workflow, t_mod, monkeypatch, repo):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    cfg.repos = {'web': str(repo)}
    cfg._workflow_tool = 'codex'
    return cfg


def test_assignment_guides_selection_and_review(workflow, t_mod, monkeypatch, repo):
    put(repo, 'DESIGN.md', '# Design system')
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    cfg._workflow_document['workflows']['standard']['behaviors'] = {'design_system': 'repo'}
    ui = ToolMenu(['workflow', 'name:standard', 'design_system', 'candidate:0', 'back'])
    t_mod._workflow_repository_settings(ui, cfg, vars(workflow), 'web', str(repo))
    entry = cfg._workflow_document['repositories'][str(repo)]
    assert entry == {'workflow': 'standard', 'design_system': {'path': 'DESIGN.md'}}
    assert any(title == 'web · Design system' for title, _, _ in ui.views)
    review = '\n'.join(t_mod._workflow_changed_review(vars(workflow), cfg._workflow_document, cfg._workflow_initial))
    assert 'Frontend · Design system: Use workflow default → DESIGN.md' in review
    assert not workflow.config_path().exists()


def test_menu_missing_manual_clear_refresh_and_defer(workflow, t_mod, monkeypatch, repo):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    cfg._workflow_document = workflow.select_design_system(cfg._workflow_document, str(repo), 'gone.md')
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['current'])
    t_mod._workflow_design_system_menu(ui, cfg, vars(workflow), 'web', str(repo))
    assert 'missing or unsafe' in ui.views[0][2]['values']['current']
    assert cfg._workflow_document == before
    path = put(repo, 'guide.txt', 'Design guidance')
    inputs = iter([None, '../outside', 'guide.txt'])
    monkeypatch.setattr(t_mod, '_config_input', lambda *args: next(inputs))
    t_mod._workflow_design_system_menu(Menu(['refresh', 'manual', 'manual', 'manual']), cfg, vars(workflow), 'web', str(repo))
    assert cfg._workflow_document['repositories'][str(repo)]['design_system']['path'] == path.name
    t_mod._workflow_design_system_menu(Menu(['']), cfg, vars(workflow), 'web', str(repo))
    assert str(repo) not in cfg._workflow_document['repositories']
    t_mod._workflow_design_system_menu(Menu(['back']), cfg, vars(workflow), 'web', str(repo))
    assert not workflow.config_path().exists()


def test_discovery_failure_and_draft_pending(workflow, t_mod, monkeypatch, repo):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    monkeypatch.setattr(workflow, 'design_system', lambda: {'workspace': lambda *a: repo,
        'automatic_source': lambda *a: None, 'discover': lambda *a: (_ for _ in ()).throw(ValueError('offline'))})
    cfg._workflow_activate = True
    t_mod._workflow_design_system_menu(Menu(['draft', 'back']), cfg, vars(workflow), 'web', str(repo))
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('choice,fail', [('back', False), ('start', False), ('start', True)])
def test_explicit_draft_launch_is_guarded(workflow, t_mod, monkeypatch, repo, choice, fail):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    calls = []
    def launch(*args, **kwargs):
        calls.append((args, kwargs))
        if fail:
            raise ValueError('owner changed')
        return {'open_command': 't session open web 9'}
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {
        'preview': lambda *args: {'dirty': True}, 'launch': launch})
    helper = dict(vars(workflow), design_system=lambda: {'draft_prompt': lambda: 'Draft only; stop before publishing.'})
    t_mod._workflow_design_draft(Menu([choice]), cfg, helper, 'web', repo)
    assert len(calls) == (choice == 'start')
    if calls:
        assert calls[0][0][2] == 'codex' and calls[0][1]['plan'] == {'dirty': True}


def test_draft_prompt_has_review_boundary(design):
    prompt = design.draft_prompt()
    assert 'DESIGN.md' in prompt and 'AGENTS.md' in prompt
    assert 'stop before committing, pushing, opening a PR' in prompt
    assert 'do not duplicate' in prompt


def test_disabled_frontend_keeps_design_visible(workflow, t_mod):
    behaviors = {'frontend_enabled': 'disabled', 'design_system': 'repo'}
    assert t_mod._workflow_section_summary(vars(workflow), 'frontend', behaviors) == 'Disabled'
    assert 'Design system: Automatically' in t_mod._workflow_section_summary(vars(workflow), 'design', behaviors)
    assert 'Design approach:' in '\n'.join(t_mod._workflow_behavior_lines(vars(workflow), 'design', behaviors))
    assert 'Design approach:' in workflow._stage_text(dict(workflow.DEFAULT, behaviors=behaviors))


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_discovery_real_renderer_narrow_and_cancel(workflow, t_mod, monkeypatch, repo, width, height):
    put(repo, 'DESIGN.md', '# Design system')
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    before = copy.deepcopy(cfg._workflow_document)
    ui = make_ui(t_mod, ['down', 'down', 'down', 'down', 'q'], width, height)
    t_mod._workflow_design_system_menu(ui, cfg, vars(workflow), 'web', str(repo))
    assert cfg._workflow_document == before
    assert ui.frames
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line, width) for line in frame) <= height
        assert all(t_mod._vis_len(line) <= width for line in frame)


def test_source_save_roundtrip_and_full_review(workflow, t_mod, repo):
    document = workflow.select_design_system(workflow.defaults(), str(repo), 'DESIGN.md')
    document['workflows']['standard']['behaviors'] = {'design_system': 'repo'}
    workflow.save(document, None, propagate=False)
    saved, _ = workflow.load()
    assert saved['repositories'][str(repo)]['design_system'] == {'path': 'DESIGN.md'}
    assert 'Frontend · Design system source: DESIGN.md' in '\n'.join(t_mod._workflow_scope_review(vars(workflow), document))


def test_repo_source_in_read_only_json(workflow, t_mod, monkeypatch, repo, capsys):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    document = workflow.select_design_system(cfg._workflow_document, str(repo), 'missing.md')
    monkeypatch.setattr(workflow, 'load', lambda: (document, ('saved', 0)))
    monkeypatch.setattr(workflow, 'propagation', lambda: {'status': lambda: {'runtime': 'test'}})
    args = t_mod.build_parser().parse_args(['config', 'workflow', 'list', '--repo', 'web', '--json'])
    args.workflow = True
    assert t_mod._cmd_workflow(cfg, args) == 0
    assert '"path": "missing.md"' in capsys.readouterr().out
    args.json = False
    assert t_mod._cmd_workflow(cfg, args) == 0
    assert 'Repository design-system source: missing.md' in capsys.readouterr().out


@pytest.mark.parametrize('paths,expected', [
    ([], None), (['DESIGN.md', 'ui/README.md'], 'DESIGN.md'),
    (['design-system.md', 'packages/ui'], 'design-system.md'),
    (['design-system.md', 'design_system.md'], None),
    (['packages/ui'], 'packages/ui'), (['ui/README.md', 'packages/ui'], None),
])
def test_automatic_discovery_resolves_only_unambiguous_sources(design, paths, expected):
    assert design.automatic_source({'candidates': [{'path': path} for path in paths]}) == expected


def test_defaults_are_live_complete_and_never_materialized(workflow, t_mod):
    before = copy.deepcopy(workflow.DEFAULT)
    resolved = workflow.resolved_behaviors({})
    assert resolved['design_system'] == 'auto'
    assert resolved['frontend_design'] == 'standalone_html'
    assert resolved['pr_open'] == resolved['pr_merge'] == 'on_request'
    assert resolved['cli_planning_preview'] == 'before_implementation'
    assert resolved['cli_planning_approval'] == 'required'
    for section in ('design', *t_mod._WORKFLOW_SECTIONS, *t_mod._WORKFLOW_PREVIEW_SUBMENUS):
        for key in t_mod._workflow_section_fields(section, {}):
            value = t_mod._workflow_field_value(vars(workflow), {}, key)
            assert value and 'Not set' not in value and 'None' not in value
    for key, default in workflow.BEHAVIOR_DEFAULTS.items():
        changed = workflow.update_behavior(before, key, default)
        assert workflow.update_behavior(changed, key, '') == before
    assert workflow.DEFAULT == before
    assert not workflow.config_path().exists()


def test_repository_off_override_roundtrip_and_agent_instructions(workflow, repo):
    w = workflow
    document = w.select_design_system(w.defaults(), str(repo), mode='off')
    assert w.validate(document) == document
    assert w.validate(__import__('json').loads(w.encode(document))) == document
    for action, name in [('use', 'standard'), ('inherit', None), ('exclude', None), ('include', None)]:
        document = w.action(document, action, name, str(repo))
        assert document['repositories'][str(repo)]['design_system'] == {'mode': 'off'}
    text = w.render(document).split('Repository scope:', 1)[1]
    assert 'Do not add design-system discovery' in text
    assert 'automatically discover repository-owned' not in text
    document['workflows']['standard']['behaviors'] = {'design_system': 'off'}
    selected = w.select_design_system(document, str(repo), 'DESIGN.md')
    text = w.render(selected).split('Repository scope:', 1)[1]
    assert 'Selected repository design-system source: "DESIGN.md"' in text
    assert 'This repository selection overrides the workflow discovery default' in text
    assert 'Do not add design-system discovery' not in text
    with pytest.raises(ValueError, match='mode'):
        w.select_design_system(document, str(repo), mode='unknown')


def test_workflow_design_menu_stages_defaults_sources_and_reset(workflow, t_mod, monkeypatch, repo):
    put(repo, 'DESIGN.md', '# Design system')
    put(repo, 'ui/README.md', '# Command and terminal contract')
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['frontend_design', 'approach', 'in_app', 'systems',
                   'repo:web', 'candidate:1', 'default', 'off', 'back', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), name='standard')
    doc = cfg._workflow_document
    assert doc['workflows']['standard']['behaviors'] == {'frontend_design': 'in_app', 'design_system': 'off'}
    assert doc['repositories'][str(repo)]['design_system'] == {'path': 'ui/README.md'}
    main = ui.views[0]
    assert list(dict(main[1])).index('frontend_design') < list(dict(main[1])).index('behaviors')
    systems = [view for view in ui.views if view[0] == 'Design systems']
    assert systems[0][2]['values']['repo:web'] == 'Auto · DESIGN.md'
    assert systems[-1][2]['values']['repo:web'] == 'ui/README.md'
    assert systems[0][2]['sections'] == {'default': 'Default', 'repo:web': 'Repositories', 'reset': 'Manage'}
    picker = next(view for view in ui.views if view[0] == 'web · Design system')
    assert [label for _, label in picker[1]].count('DESIGN.md') == 1
    assert 'Manage' in picker[2]['sections'].values()
    t_mod._workflow_policy_menu(ToolMenu(['frontend_design', 'systems', 'default', '', 'back',
        'approach', '', 'back', 'back']), cfg, vars(workflow), name='standard')
    assert cfg._workflow_document['workflows']['standard'] == before['workflows']['standard']
    assert cfg._workflow_document['repositories'][str(repo)]['design_system']['path'] == 'ui/README.md'
    assert not workflow.config_path().exists()


def test_design_menu_scopes_repos_and_keeps_workflow_changes_on_interrupt(workflow, t_mod, monkeypatch, repo):
    from test_workflow_tool_ui import InterruptMenu
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    cfg._workflow_document = workflow.action(cfg._workflow_document, 'add', 'other')
    cfg._workflow_document = workflow.action(cfg._workflow_document, 'use', 'other', str(repo))
    ui = ToolMenu(['frontend_design', 'systems', 'back', 'back', 'back'])
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), name='standard')
    assert dict(next(view for view in ui.views if view[0] == 'Design systems')[1]) == {
        'default': 'Find design systems', 'reset': 'Reset to defaults', 'back': 'Back'}
    assert cfg._workflow_document == before
    with pytest.raises(KeyboardInterrupt):
        t_mod._workflow_policy_menu(InterruptMenu(['frontend_design', 'systems', 'default', 'off', KeyboardInterrupt]),
                                    cfg, vars(workflow), name='other')
    assert cfg._workflow_document['workflows']['other']['behaviors']['design_system'] == 'off'


def test_source_picker_off_inheritance_and_scan_states(workflow, t_mod, monkeypatch, repo):
    cfg = ui_config(workflow, t_mod, monkeypatch, repo)
    helper = vars(workflow)
    empty = {'candidates': [], 'warnings': []}
    assert t_mod._workflow_design_source_label(helper, cfg._workflow_document, str(repo), empty) == 'Auto · No system found'
    assert t_mod._workflow_design_source_label(helper, cfg._workflow_document, str(repo),
        dict(empty, warnings=['offline'])) == 'Auto · Scan unavailable'
    ui = ToolMenu(['off'])
    t_mod._workflow_design_system_menu(ui, cfg, helper, 'web', str(repo))
    assert cfg._workflow_document['repositories'][str(repo)]['design_system'] == {'mode': 'off'}
    assert t_mod._workflow_design_source_label(helper, cfg._workflow_document, str(repo), empty) == 'Off'
    t_mod._workflow_design_system_menu(ToolMenu(['']), cfg, helper, 'web', str(repo))
    assert str(repo) not in cfg._workflow_document['repositories']
    assert t_mod._workflow_design_source_label(helper, cfg._workflow_document, str(repo), empty,
        dict(workflow.DEFAULT, behaviors={'design_system': 'off'})) == 'Off'
    put(repo, 'DESIGN.md', '# Design system')
    cfg._workflow_document = workflow.select_design_system(cfg._workflow_document, str(repo), 'DESIGN.md')
    ui = ToolMenu(['back'])
    t_mod._workflow_design_system_menu(ui, cfg, helper, 'web', str(repo))
    assert [label for _, label in ui.views[0][1]].count('DESIGN.md') == 1


def test_incomplete_discovery_requires_choice_except_root_entry(design):
    result = {'candidates': [{'path': 'packages/ui'}], 'warnings': ['Inspection limited']}
    assert design.automatic_source(result) is None
    result['candidates'].append({'path': 'DESIGN.md'})
    assert design.automatic_source(result) == 'DESIGN.md'


@pytest.mark.parametrize('approach', ['standalone_html', 'in_app'])
def test_design_approach_does_not_request_disabled_preview(workflow, approach):
    policy = dict(workflow.DEFAULT, behaviors={'frontend_enabled': 'disabled', 'frontend_design': approach})
    text = workflow._stage_text(policy)
    assert 'When a frontend design prototype is requested or due' in text
    assert 'preview by itself' in text
    assert 'For frontend changes — When to preview:' not in text
    assert 'Design system:' in text
