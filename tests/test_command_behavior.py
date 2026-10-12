"""Cross-command behavior, checked at runtime rather than inferred from lint."""
import json
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
    # Check the real dispatch preamble in a terminal with an update available.
    # Handler behavior is covered by each inventory entry's concrete regressions.
    monkeypatch.setattr(t_mod, '_t_update_interactive', lambda: True)
    monkeypatch.setattr(t_mod, '_t_update_installation', lambda: pytest.fail('read-only command checked updates'))
    monkeypatch.setattr(t_mod, 'Config', lambda: object())
    seen = []
    handler = lambda cfg, args: seen.append(args) or 0
    for name in t_mod.IMPLEMENTED:
        monkeypatch.setitem(t_mod.IMPLEMENTED, name, handler)
    monkeypatch.setattr(t_mod, 'cmd_cursor', handler)
    assert t_mod.main(argv) == 0
    assert len(seen) == 1
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
    ['share'], ['share', 'targets'], ['share', 'targets', '--json'],
    ['share', 'inbox'], ['share', 'read', '123'], ['share', '--json', 'targets'],
])
def test_share_readonly_verbs_and_bare_help(t_mod, argv):
    assert t_mod._command_readonly(argv)


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
