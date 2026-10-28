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
    for flag in ('--app', '--cli', '--fg', '--all', '--live', '--remote', '--days', '--host', '--help'):
        assert flag in quick
    assert t_mod.main(['help', *words]) == 0
    detail = capsys.readouterr().out
    for heading in ('PICKER CONTROLS', 'LIVE OWNERS AND DESKTOP', 'REMOTE HISTORY', 'REQUIREMENTS AND DISCOVERY'):
        assert heading in detail
    for retained in ('Alt-T', 'Alt-V', 'Ctrl-/', 'only a selected remote transcript', 'never resumed a second time'):
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
    displayed = [*path[:-1], 'remove'] if path[:1] == ['hosts'] and path[-1] in ('delete', 'rm') else path
    assert quick.startswith('usage: t ' + ' '.join(displayed))
    assert 'OPTIONS:' in quick and 'LEARN MORE' in quick
    assert t_mod.main([*path, '--help']) == 0
    assert capsys.readouterr().out == quick


@pytest.mark.parametrize('change,expected', [
    ('summary', 'summary exceeds'), ('option', 'argument host exceeds'),
    ('page', 'lines (limit'), ('missing_option', 'missing public option --host'),
    ('heading', 'unrecognized heading'), ('examples', '4 examples'),
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
