"""Quick help is a bounded, complete reference; detailed help retains explanations."""

import argparse
import contextlib
import io
import json
from pathlib import Path

import pytest

from test_cli_contract import lint_module


CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'ui/command-contract.json').read_text())


def test_all_help_surfaces_follow_the_contract(t_mod):
    assert lint_module().check_help_style(t_mod, t_mod.build_parser(), CONTRACT) == []


@pytest.mark.parametrize('words', [['resume'], ['session', 'resume']])
@pytest.mark.parametrize('width', [80, 44, 32, 120])
def test_resume_quick_help_and_structured_details(t_mod, monkeypatch, capsys, words, width):
    monkeypatch.setattr(t_mod, 'Config', lambda: pytest.fail('help loaded config'))
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: pytest.fail('help updated'))
    monkeypatch.setattr(t_mod.shutil, 'get_terminal_size',
                        lambda fallback=(80, 24): t_mod.os.terminal_size((width, 24)))
    assert t_mod.main([*words, '-h']) == 0
    quick = capsys.readouterr().out
    assert t_mod.main([*words, '--help']) == 0
    assert capsys.readouterr().out == quick
    assert len(quick.splitlines()) <= 45 if width >= 80 else True
    assert max(map(len, quick.splitlines())) <= min(width, 80)
    assert 't help ' + ' '.join(words) in ' '.join(quick.split())
    assert 'browse local and remote repositories' in ' '.join(quick.split())
    assert 'cloned only after selection' in ' '.join(quick.split())
    for flag in ('--app', '--cli', '--fg', '--all', '--live', '--remote', '--days', '--host', '--help'):
        assert flag in quick
    assert t_mod.main(['help', *words]) == 0
    detail = capsys.readouterr().out
    for heading in ('PICKER CONTROLS', 'LIVE OWNERS AND DESKTOP', 'REMOTE HISTORY', 'REQUIREMENTS AND DISCOVERY'):
        assert heading in detail
    for retained in ('Alt-T', 'Alt-V', 'Ctrl-/', 'only a selected remote transcript', 'never resumed a second time',
                     'registered only after selection'):
        assert retained in ' '.join(detail.split())
    assert len(detail) > len(quick)


@pytest.mark.parametrize('path', [
    ['share', 'read'], ['system', 'advisories'], ['config', 'show'],
    ['config', 'workflow', 'list'], ['config', 'onboard'], ['profile', 'apply'], ['policy', 'apply'],
    ['agent', 'status'], ['hosts', 'delete'], ['hosts', 'rm'],
])
def test_nested_and_mode_help_share_layout_without_side_effects(t_mod, monkeypatch, capsys, path):
    monkeypatch.setattr(t_mod, 'Config', lambda: pytest.fail('help loaded config'))
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: pytest.fail('help updated'))
    assert t_mod.main([*path, '-h']) == 0
    quick = capsys.readouterr().out
    displayed = [*path[:-1], 'remove'] if path[:1] == ['hosts'] and path[-1] in ('delete', 'rm') else t_mod._workflow_argv(path)
    assert quick.startswith('usage: t ' + ' '.join(displayed))
    assert 'OPTIONS:' in quick and 'LEARN MORE' in quick
    assert t_mod.main([*path, '--help']) == 0
    assert capsys.readouterr().out == quick


@pytest.mark.parametrize('change,expected', [
    ('summary', 'summary exceeds'), ('option', 'argument host exceeds'),
    ('page', 'lines (limit'), ('missing_option', 'missing public option --host'),
    ('heading', 'unrecognized heading'), ('heading_case', 'headings must use uppercase'),
    ('examples', '4 examples'),
    ('indent', 'two spaces'), ('whitespace', 'trailing whitespace'),
    ('usage', 'start with usage'), ('pointer', 'details pointer'),
])
def test_help_lint_rejects_rendered_style_regressions(t_mod, change, expected):
    lint = lint_module()
    parser = t_mod._help_parser(t_mod.build_parser(), ['resume'])
    body = parser.format_help()
    if change == 'summary':
        parser.description = 'word ' * 61
    elif change == 'option':
        parser._option_string_actions['--host'].help = 'word ' * 25
    elif change == 'page':
        body += '\n' * 61
    elif change == 'missing_option':
        body = body.replace('--host', '--missing')
    elif change == 'heading':
        body += '\nSURPRISE\n'
    elif change == 'heading_case':
        body = body.replace('EXAMPLES', 'examples:')
    elif change == 'examples':
        body = body.replace('EXAMPLES\n', 'EXAMPLES\n  t resume api 3\n')
    elif change == 'indent':
        body += '\n one-space row\n'
    elif change == 'whitespace':
        body += '\n  trailing \n'
    elif change == 'usage':
        body = body.replace('usage:', 'invoke:', 1)
    else:
        body = body.replace('t help resume', 'documentation')
    errors = lint.check_help_page('resume', body, parser, CONTRACT['help_style'])
    assert any(expected in error for error in errors), errors


def test_help_lint_covers_new_native_parsers_and_restores_terminal_size(t_mod):
    lint = lint_module()
    parser = t_mod.build_parser()
    share = t_mod._help_parser(parser, ['share'])
    children = next(a for a in share._actions if isinstance(a, argparse._SubParsersAction))
    child = children.add_parser('future', help='A future public command', description='word ' * 61)
    child.add_argument('--new-option', help='a public option')
    surfaces = {path for path, _, _ in lint.help_surfaces(t_mod, parser)}
    assert ('share', 'future') in surfaces
    original = t_mod.shutil.get_terminal_size
    errors = lint.check_help_style(t_mod, parser, CONTRACT)
    assert any('t share future summary exceeds' in error for error in errors)
    assert any('t share future help must show its complete command path' in error for error in errors)
    assert t_mod.shutil.get_terminal_size is original
    assert lint.check_help_style(t_mod, parser, {}) == ['command inventory must define the help_style contract']


def test_help_lint_preserves_unbreakable_tokens_and_checks_width(t_mod):
    lint = lint_module()
    policy = CONTRACT['help_style']
    body = 'usage: t resume\n\n' + 'x' * 90 + '\n\nLEARN MORE\n  t help resume\n'
    assert not any('columns' in error for error in lint.check_help_page('resume', body, None, policy))
    body = body.replace('x' * 90, 'short ' * 18)
    assert any('80 columns' in error for error in lint.check_help_page('resume', body, None, policy))


def test_detailed_help_rendering_does_not_mutate_reused_parsers(t_mod):
    parser = t_mod._help_parser(t_mod.build_parser(), ['resume'])
    before = parser.format_help()
    with contextlib.redirect_stdout(io.StringIO()):
        t_mod._print_command_help(parser, ['resume'], ['resume'], detailed=True)
    assert parser.format_help() == before


@pytest.mark.parametrize('mutation', ['missing', 'wrong_color', 'no_bold', 'no_reset', 'body', 'layout'])
def test_help_color_lint_rejects_regressions(mutation):
    lint = lint_module()
    plain = 'usage: t demo\n\nOPTIONS:\n  --flag  Body\n'
    colored = '\033[1m\033[36musage:\033[0m t demo\n\n\033[1m\033[36mOPTIONS:\033[0m\n  --flag  Body\n'
    assert lint.check_help_color('demo', plain, colored, enabled=True) == []
    if mutation == 'missing':
        colored = plain
    elif mutation == 'wrong_color':
        colored = colored.replace('[36m', '[32m')
    elif mutation == 'no_bold':
        colored = colored.replace('\033[1m', '')
    elif mutation == 'no_reset':
        colored = colored.replace('\033[0m', '')
    elif mutation == 'body':
        colored = colored.replace('Body', '\033[36mBody\033[0m')
    else:
        colored = colored.replace('  --flag', '    --flag')
    assert lint.check_help_color('demo', plain, colored, enabled=True)
    if mutation != 'missing':
        assert lint.check_help_color('demo', plain, colored, enabled=False)


@pytest.mark.parametrize('tty,overrides', [
    (True, {}), (False, {}), (True, {'NO_COLOR': ''}), (True, {'NO_COLOR': '1'}),
    (True, {'TERM': 'dumb'}), (True, {'CLICOLOR': '0'}), (False, {'CLICOLOR': '1'}),
])
def test_help_output_honors_stdout_and_environment(t_mod, monkeypatch, tty, overrides):
    lint = lint_module()
    for key in ('NO_COLOR', 'CLICOLOR'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('TERM', 'xterm-256color')
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(t_mod, 'Config', lambda: pytest.fail('help loaded config'))
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: pytest.fail('help updated'))
    # stderr being a terminal never enables color for redirected stdout.
    monkeypatch.setattr(t_mod.sys, 'stderr', lint._HelpOutput(True))
    parser = t_mod.build_parser()
    for words in (['--help'], ['session', 'resume', '-h'], ['help', 'session', 'resume'],
                  ['help', 'agents'], ['help', 'aliases'], ['session', 'resume', '-h']):
        with contextlib.redirect_stdout(lint._HelpOutput(False)) as output:
            assert t_mod.main(words) == 0
        plain = output.getvalue()
        with contextlib.redirect_stdout(lint._HelpOutput(tty)) as output:
            assert t_mod.main(words) == 0
        assert lint.check_help_color(' '.join(words), plain, output.getvalue(),
                                     enabled=tty and not overrides) == []
    with contextlib.redirect_stdout(lint._HelpOutput(True)):
        detailed = {path: body for path, body, _ in lint.help_surfaces(t_mod, parser, detailed=True, tty=True)}
    assert ('help', 'agents') in detailed and ('help', 'aliases') in detailed
    if tty and not overrides:
        assert '\033[1m\033[36mPICKER CONTROLS\033[0m' in detailed[('session', 'resume')]


def test_help_color_lint_detects_detail_and_topic_omissions(t_mod, monkeypatch):
    lint = lint_module()
    wrap = t_mod._wrap_help
    def broken_wrap(body):
        rendered = wrap(body)
        return rendered.replace('\033[1m\033[36mAGENT SUPPORT\033[0m', 'AGENT SUPPORT').replace(
            '\033[1m\033[36mPICKER CONTROLS\033[0m', 'PICKER CONTROLS')
    monkeypatch.setattr(t_mod, '_wrap_help', broken_wrap)
    errors = lint.check_help_style(t_mod, t_mod.build_parser(), CONTRACT)
    assert any('t help agents help must use bold cyan' in error for error in errors)
    assert any('t session resume help must use bold cyan' in error for error in errors)


def test_help_color_lint_accepts_titles_but_not_other_controls():
    lint = lint_module()
    plain = 'USAGE\n  t help\n'
    rendered = '\x1b]0;t help\x07' + plain + '\x1b]0;\x07'
    assert lint.check_help_color('help', plain, rendered, enabled=False) == []
    assert lint.check_help_color('help', plain, '\x1b[2J' + rendered, enabled=False)


@pytest.mark.parametrize('width', [80, 44, 32])
def test_root_help_separates_work_and_configuration(t_mod, monkeypatch, width):
    monkeypatch.setattr(t_mod.shutil, 'get_terminal_size',
                        lambda fallback=(80, 24): t_mod.os.terminal_size((width, 24)))
    text = t_mod._root_help()
    before = text.split('CONFIGURATION', 1)[0]
    assert before.endswith('\n\n')
    assert 'agentcore' in before
    assert 'runners' in text.split('CONFIGURATION', 1)[1]
