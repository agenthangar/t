"""Cross-command behavior, checked at runtime rather than inferred from lint."""
import json
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / 'ui/command-contract.json').read_text())
READONLY = [pytest.param(mode['argv'], mode.get('terminal', True), id=f'{name}-{key}')
            for name, entry in CONTRACT['commands'].items()
            for key, mode in entry['modes'].items() if mode['read_only']]


@pytest.mark.parametrize('argv,terminal', READONLY)
def test_readonly_commands_never_offer_updates(t_mod, monkeypatch, argv, terminal):
    # Check the real dispatch preamble with an update available.
    # Handler behavior is covered by each inventory entry's concrete regressions.
    stdin, stdout = io.StringIO(), io.StringIO()
    stdin.isatty = stdout.isatty = lambda: terminal
    monkeypatch.setattr(t_mod.sys, 'stdin', stdin)
    monkeypatch.setattr(t_mod.sys, 'stdout', stdout)
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: True)
    monkeypatch.setattr(t_mod, '_t_update_installation', lambda: pytest.fail('read-only command checked updates'))
    monkeypatch.setattr(t_mod, 'Config', lambda: object())
    seen = []
    handler = lambda cfg, args: seen.append(args) or 0
    for name in t_mod.IMPLEMENTED:
        monkeypatch.setitem(t_mod.IMPLEMENTED, name, handler)
    monkeypatch.setattr(t_mod, 'cmd_cursor', handler)
    assert t_mod.main(argv) == 0
    assert len(seen) == (0 if argv == ['share'] and not terminal else 1)
    assert t_mod._command_readonly(argv, terminal=terminal)


@pytest.mark.parametrize('argv', [
    ['ls'], ['repos', 'ls'], ['repos', 'path', 'api'], ['config', '--show'],
    ['install', '--status'], ['login', '--status'], ['trust', '--status'],
    ['permissions'], ['instructions', '--show'], ['doctor'], ['cursor', '--ls'],
    ['hosts', '--json'], ['update', '--check'], [],
])
def test_readonly_aliases_share_the_prompt_contract(t_mod, monkeypatch, argv):
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: pytest.fail('read-only alias reached update gate'))
    assert t_mod._command_readonly(argv)
    assert t_mod._t_auto_update(argv) is None


@pytest.mark.parametrize('argv', [
    ['session', 'open', 'api'], ['repo', 'cd', 'api'], ['config', 'open'],
    ['profile', 'edit'], ['policy', 'apply'], ['hosts', 'default', 'mini'],
    ['hosts', 'default', '--clear'], ['cursor', 'send', 'chat'],
    ['hosts', 'run', 'mini', 'program', '--json'], ['session', 'open', '--', '--dry-run'],
])
def test_action_and_forwarded_arguments_are_not_readonly(t_mod, argv):
    assert not t_mod._command_readonly(argv)


def test_history_readonly_follows_terminal_context(t_mod, monkeypatch):
    import io

    argv = ['session', 'history', 'api', '1']
    tty_in, tty_out = io.StringIO(), io.StringIO()
    tty_in.isatty = tty_out.isatty = lambda: True
    with monkeypatch.context() as context:
        context.setattr(t_mod.sys, 'stdin', tty_in)
        context.setattr(t_mod.sys, 'stdout', tty_out)
        assert not t_mod._command_readonly(argv)
        assert t_mod._command_readonly([*argv, '--json'])
        context.setattr(t_mod.sys, 'stdin', io.StringIO())
        assert t_mod._command_readonly(argv)


def test_share_machine_receipt_is_mutating_but_never_offers_update(t_mod, monkeypatch):
    argv = ['share', '--to', 'codex:123', '--file', '-', '--json']
    assert not t_mod._command_readonly(argv, terminal=True)
    assert not t_mod._command_readonly(['share', '--file', 'read', '--to', 'codex:123'])
    monkeypatch.setattr(t_mod, '_t_update_interactive',
                        lambda: pytest.fail('share machine output reached update prompt'))
    monkeypatch.setattr(t_mod, 'Config', lambda: object())
    seen = []
    monkeypatch.setitem(t_mod.IMPLEMENTED, 'share',
                        lambda cfg, args: seen.append(args) or 0)
    assert t_mod.main(argv) == 0
    assert len(seen) == 1 and seen[0].json


@pytest.mark.parametrize('argv', [
    ['share', 'targets'], ['share', 'targets', '--json'],
    ['share', 'inbox'], ['share', 'read', '123'], ['share', '--json', 'targets'],
])
def test_share_readonly_verbs_and_bare_help(t_mod, argv):
    assert t_mod._command_readonly(argv)


def test_bare_share_is_readonly_help_only_without_terminal(t_mod):
    assert t_mod._command_readonly(['share'], terminal=False)
    assert not t_mod._command_readonly(['share'], terminal=True)
    assert t_mod._command_readonly(['share', '--json'], terminal=True)


@pytest.mark.parametrize('width', [80, 44, 32])
def test_tables_keep_complete_values_and_stay_inside_terminal(t_mod, width):
    value = 'a-long-checkout-or-issue-title-' * 5
    lines = t_mod._table_lines(('REPO', 'CHECKOUT'), [('api', value), ('web', '/code/web')], width)
    assert all(len(line) <= width for line in lines)
    assert 'REPO' in '\n'.join(lines) and 'CHECKOUT' in '\n'.join(lines)
    assert value in ''.join(line.strip().replace('CHECKOUT: ', '') for line in lines)
    assert 'api' in '\n'.join(lines) and '/code/web' in '\n'.join(lines)


def test_tables_align_plain_columns_and_do_not_truncate_redirects(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: False)
    rows = [('api', '/very/' + 'long/' * 30), ('web', '/code/web')]
    lines = t_mod._table_lines(('REPO', 'CHECKOUT'), rows)
    assert lines[0].index('CHECKOUT') == lines[1].index('/very/') == lines[2].index('/code/')
    assert rows[0][1] in lines[1]
    assert '\x1b' not in '\n'.join(lines)
    assert t_mod._table_lines(('REPO', 'CHECKOUT'), []) == ['  REPO  CHECKOUT']


def test_session_table_keeps_status_and_identity_in_small_view(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, '_columns', lambda: 32)
    rows = [dict(host='mini', slot='api-17', state='app', context='active', agent='codex',
                 model='model-test', summary='#12 merged, still in progress')]
    lines = t_mod._session_table_lines(rows, remote=True)
    text = '\n'.join(lines)
    assert all(len(line) <= 32 for line in lines)
    assert all(value in text for value in ('● ✓ ↻', 'desktop', 'api-17', 'mini', 'model-test'))
    rows[0].update(state='detached', context='', summary='')
    assert '○' in '\n'.join(t_mod._session_table_lines(rows))


def test_table_width_counts_wide_and_combining_characters(t_mod):
    rows = [('漢字' * 20, 'e\u0301' * 40)]
    lines = t_mod._table_lines(('NAME', 'VALUE'), rows, 32)
    assert all(t_mod._display_width(line) <= 32 for line in lines)
    assert t_mod._display_width('漢e\u0301\u200d') == 3
    assert t_mod._wrap_columns('', 32) == []
    wide = t_mod._table_lines(('NAME', 'VALUE'), [('漢字', 'one'), ('test', 'two')], 80)
    assert t_mod._display_width(wide[1].split('one')[0]) == wide[2].index('two')


@pytest.mark.parametrize('tty_in,tty_out,confirmed,expected', [
    (True, True, False, 130), (True, True, 'empty', 0), (True, False, True, 2),
])
def test_login_aborts_without_running_or_prompting_off_tty(t_mod, monkeypatch, tty_in, tty_out,
                                                         confirmed, expected):
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: tty_in)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: tty_out)
    monkeypatch.setattr(t_mod, '_login_probe', lambda agent: dict(state='logged-out', bin='/fixture/' + agent))
    monkeypatch.setattr(t_mod, '_install_wizard', lambda *a, **kw: confirmed)
    monkeypatch.setattr(t_mod, '_login_run', lambda *a, **kw: pytest.fail('aborted login ran'))
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('redirected login prompted'))
    args = t_mod.build_parser().parse_args(['agent', 'login'])
    assert t_mod.cmd_login(None, args) == expected


@pytest.mark.parametrize('command,wizard', [('config', '_config_wizard'), ('new', '_new_wizard'),
                                           ('setup', '_setup_wizard'), ('install', '_install_wizard')])
def test_python_wizard_cancellation_does_not_apply(t_mod, monkeypatch, tmp_path, command, wizard):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config'))
    monkeypatch.setenv('T_LOCAL_RC', str(tmp_path / 'local'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, wizard, lambda *a, **kw: None if command in ('config', 'new') else False)
    for effect in ('_config_write', '_append_local', '_new_walk', '_install_walk'):
        monkeypatch.setattr(t_mod, effect, lambda *a, **kw: pytest.fail('cancel applied work'))
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda *a, **kw: pytest.fail('cancel spawned a shell'))
    if command == 'setup':
        monkeypatch.setattr(t_mod, '_scan_repos', lambda *a: [str(tmp_path / 'api')])
        monkeypatch.setattr(t_mod, '_parse_ssh_hosts', lambda *a: [])
        argv = ['setup', str(tmp_path)]
    elif command == 'install':
        monkeypatch.setattr(t_mod, '_install_probe', lambda: {
            a: dict(installed=False, logged_in=False, version='', bin='') for a in t_mod.INSTALL_AGENTS})
        argv = ['install']
    else:
        argv = [command]
    args = t_mod.build_parser().parse_args(argv)
    assert getattr(t_mod, 'cmd_' + command)(t_mod.Config(), args) == 130
    assert not (tmp_path / 'local').exists()


# Synthetic history fixtures use the same HOME/XDG sandbox as the history suite.
from test_history_data import history
from test_cli_contract import lint_module


def _history_display_fixture(tmp_path, *, owned):
    owner = dict(host='mini', slot='api-17', state='attached', context='active', agent='codex',
                 sid='exact-history-conversation', cwd=str(tmp_path / 'worktree'), model='model-test',
                 summary='Fix login #12 merged, still in progress')
    row = dict(repo='org/api', sid=owner['sid'], agent='codex', title='Retained working-on text',
               updated=1767225600, owners=[owner] if owned else [],
               associations=[dict(host='mini', slot='17', workspace=str(tmp_path / 'worktree'),
                                  branch='retained/branch-with-full-detail', commit='a' * 40,
                                  evidence='transcript', observed=1767225600, available=True)],
               sources=[dict(host='mini', path='/recorded/transcript-with-full-detail.jsonl', available=True)],
               prs=[dict(repo='org/api', number=12, url='https://github.com/org/api/pull/12', state='MERGED')],
               mentions=[dict(repo='org/api', number=99, url='https://github.com/org/api/pull/99')])
    return row, owner


def _history_contract_command(history, t_mod, tmp_path, monkeypatch, row, owners):
    cfg = SimpleNamespace(repos={'api': str(tmp_path / 'api')}, hosts={'mini': 'fixture.example'},
                          worktree_root=str(tmp_path / 'worktrees'), repo_of_dir=lambda cwd: ('api', '17'))
    store = history.Store()
    store.merge(row)
    command = history.HistoryCommand(cfg, {**vars(t_mod), '_t_source_root': lambda: str(ROOT)}, store)
    monkeypatch.setattr(command, 'identity', lambda alias: 'org/api')
    monkeypatch.setattr(command, 'discover', lambda alias: None)
    monkeypatch.setattr(command, 'owners', lambda *a: owners)
    monkeypatch.setattr(command, 'branch_prs', lambda *a, **k: None)
    monkeypatch.setattr(command, 'pr_states', lambda *a, **k: None)
    for effect in ('pick', 'choose_slot', 'resume', 'refresh'):
        monkeypatch.setattr(command, effect, lambda *a, **k: pytest.fail('display contract started interactive or network work'))
    return command, store


def _table_contract_cells(text, labels):
    """Read aligned or labeled output, so swapped/missing rendered fields fail."""
    lines = [line for line in text.splitlines() if line.startswith('  ')]
    header = next((line for line in lines if all(label in line for label in labels)), None)
    if header:
        start = lines.index(header) + 1
        body = lines[start]
        positions = [header.index(label) for label in labels] + [len(body)]
        return {label: body[positions[i]:positions[i + 1]].strip() for i, label in enumerate(labels)}
    cells, current = {}, None
    for line in lines:
        stripped = line.strip()
        label = next((label for label in labels if stripped.startswith(label + ':')), None)
        if label:
            current = label
            cells[current] = stripped[len(label) + 1:].strip()
        elif current:
            cells[current] += stripped
    return cells


@pytest.mark.parametrize('width', [None, 80, 44, 32])
def test_session_and_history_display_follow_shared_schema(history, t_mod, tmp_path, monkeypatch, capsys, width):
    """The contract must describe real ls and history output, including wrapped cells."""
    common = CONTRACT['display_schemas']['session_information']
    schema = CONTRACT['display_schemas']['history_information']
    row, owner = _history_display_fixture(tmp_path, owned=True)
    command, store = _history_contract_command(history, t_mod, tmp_path, monkeypatch, row, [owner])
    enriched, = command.records('api', '17', [owner])
    projection = history.history_display(enriched, alias='api')
    assert lint_module().check_display_projection(CONTRACT, 'history_information', projection) == []
    assert projection['state'] == owner['state'] and projection['context'] == owner['context']
    assert projection['model'] == owner['model'] and projection['slot'] == owner['slot']
    assert projection['interface'] == 'cli' and projection['host'] == owner['host']
    # A missing renderer fact is an executable violation, even if its schema exists.
    incomplete = dict(projection)
    incomplete.pop('model')
    assert any('model' in error for error in lint_module().check_display_projection(
        CONTRACT, 'history_information', incomplete))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: width is not None)
    if width is not None:
        monkeypatch.setattr(t_mod, '_columns', lambda: width)
    live = '\n'.join(t_mod._session_table_lines([owner], remote=True))
    try:
        args = t_mod.build_parser().parse_args(['session', 'history', 'api', '17'])
        assert command.execute(args) == 0
        retained = capsys.readouterr().out
    finally:
        store.close()
    labels = [column['label'] for column in common['columns']]
    assert schema['layouts']['table']['columns'][:len(labels)] == labels
    for label in schema['layouts']['table']['columns']:
        assert label in retained
    for label in labels:
        assert label in live and label in retained
    live_cells = _table_contract_cells(live, labels)
    retained_cells = _table_contract_cells(retained, schema['layouts']['table']['columns'])
    for label, value in (('UI', 'cli'), ('HOST', owner['host']), ('SESSION', owner['slot']),
                         ('MODEL', owner['model']), ('WORKING ON', owner['summary'])):
        assert ''.join(value.split()) in ''.join(live_cells[label].split())
        assert ''.join(value.split()) in ''.join(retained_cells[label].split())
    assert row['associations'][0]['branch'] in retained_cells['BRANCH']
    assert '#12[MERGED]' in retained_cells['PRS']
    assert retained_cells['ID'] == row['sid']
    import re
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', retained_cells['DATE'])
    for mark in common['status_marks'].values():
        if mark != common['status_marks']['detached']:
            assert mark in live and mark in retained
    if width is not None:
        assert all(t_mod._display_width(line) <= width for line in retained.splitlines() if line.startswith('  '))


def test_history_display_contract_preserves_unknowns_preview_and_json(history, t_mod, tmp_path, monkeypatch, capsys):
    schema = CONTRACT['display_schemas']['history_information']
    common = CONTRACT['display_schemas']['session_information']
    row, _ = _history_display_fixture(tmp_path, owned=False)
    projection = history.history_display(row, alias='api')
    assert projection['model'] == common['fields']['model']['unknown']
    assert projection['interface'] == common['fields']['interface']['unknown']
    assert projection['context'] == common['fields']['context']['unknown']
    assert '✓' not in projection['status'] and '●' not in projection['status']
    preview = history.details(row, alias='api')
    for label in ('Working on:', 'Status:', 'Context:', 'UI:', 'Model:', 'Host:', 'Session:', 'Date:', 'ID:', 'Branch:', 'PRs:'):
        assert label in preview
    for value in (row['repo'], row['sid'], row['title'], row['associations'][0]['workspace'],
                  row['associations'][0]['branch'], row['associations'][0]['commit'],
                  row['prs'][0]['url'], row['mentions'][0]['url'], row['sources'][0]['path']):
        assert value in preview
    command, store = _history_contract_command(history, t_mod, tmp_path, monkeypatch, row, [])
    try:
        args = t_mod.build_parser().parse_args(['session', 'history', 'api', '17', '--json'])
        assert command.execute(args) == 0
        output = capsys.readouterr().out
    finally:
        store.close()
    payload = json.loads(output)
    assert lint_module().check_history_json(CONTRACT, payload) == []
    legacy = json.loads(output)
    for field in schema['json']['additive_fields']:
        legacy['conversations'][0].pop(field, None)
    assert lint_module().check_history_json(CONTRACT, legacy) == []
    mistyped = json.loads(output)
    mistyped['page'] = True
    assert any('page' in error for error in lint_module().check_history_json(CONTRACT, mistyped))
    malformed = json.loads(output)
    malformed['conversations'][0]['display'].pop('summary')
    assert any('summary' in error for error in lint_module().check_history_json(CONTRACT, malformed))
    assert set(schema['json']['envelope_required']) <= set(payload)
    assert payload['version'] == schema['json']['version']
    record, = payload['conversations']
    assert set(schema['json']['conversation_required']) <= set(record)
    assert record['sid'] == row['sid'] and record['associations'][0]['commit'] == 'a' * 40
    assert lint_module().check_display_projection(CONTRACT, 'history_information', record['display']) == []
    assert record['display']['model'] == '-' and record['display']['interface'] == 'unknown'
    assert '\x1b' not in output and 'Enter ' not in output
