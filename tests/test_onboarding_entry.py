"""The first-run entry never leaks into explicit commands or machine consumers."""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


def terminal(monkeypatch, t_mod, yes=True):
    stdin, stdout = io.StringIO(), io.StringIO()
    stdin.isatty = stdout.isatty = lambda: yes
    monkeypatch.setattr(t_mod.sys, 'stdin', stdin)
    monkeypatch.setattr(t_mod.sys, 'stdout', stdout)
    return stdout


def test_bare_nonterminal_stays_help_without_loading_config(t_mod, monkeypatch):
    output = terminal(monkeypatch, t_mod, False)
    monkeypatch.setattr(t_mod, 'Config', lambda: pytest.fail('loaded config'))
    monkeypatch.setattr(t_mod, '_onboarding', lambda: pytest.fail('started discovery'))
    assert t_mod.main([]) == 0
    assert output.getvalue() == t_mod._root_help()
    assert t_mod._command_readonly([], terminal=False)
    assert not t_mod._command_readonly([], terminal=True)


@pytest.mark.parametrize('fresh', [True, False])
def test_bare_terminal_routes_fresh_and_returning_accounts(t_mod, monkeypatch, fresh):
    terminal(monkeypatch, t_mod)
    cfg, calls = object(), []
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod, '_onboarding', lambda: {
        'needed': lambda c, api: fresh,
        'run': lambda c, api: calls.append(('setup', c)) or 130})
    monkeypatch.setattr(t_mod, '_onboarding_start', lambda c: calls.append(('start', c)) or 130)
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda _: pytest.fail('checked updates before setup'))
    assert t_mod.main([]) == 130
    assert calls == [('setup' if fresh else 'start', cfg)]


def test_config_onboard_repeats_discovery_without_new_top_level_command(t_mod, monkeypatch):
    terminal(monkeypatch, t_mod)
    calls = []
    monkeypatch.setattr(t_mod, 'Config', lambda: object())
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda _: None)
    monkeypatch.setattr(t_mod, '_onboarding', lambda: {'run': lambda c, a, **kw: calls.append(kw) or 0})
    assert t_mod.main(['config', 'onboard']) == 0
    assert calls == [{'force': True}]
    assert t_mod.main(['onboard']) == 2
    assert t_mod._canonical_error(['config', 'open', '--onboard'])


@pytest.mark.parametrize('argv', [['--help'], ['--version'], ['help', 'config', 'onboard'],
                                  ['config', 'show'], ['session', 'list'], ['issue', 'list', '--json'],
                                  ['agent', 'status'], ['hosts', 'list'], ['system', 'diagnose']])
def test_explicit_commands_never_offer_onboarding(t_mod, monkeypatch, argv):
    terminal(monkeypatch, t_mod)
    monkeypatch.setattr(t_mod, '_onboarding', lambda: pytest.fail('onboarding on explicit command'))
    monkeypatch.setattr(t_mod, 'Config', lambda: object())
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda _: None)
    for key in t_mod.IMPLEMENTED:
        monkeypatch.setitem(t_mod.IMPLEMENTED, key, lambda cfg, args: 0)
    if argv == ['--version']:
        with pytest.raises(SystemExit) as exc:
            t_mod.main(argv)
        assert exc.value.code == 0
    else:
        assert t_mod.main(argv) == 0


def test_onboarding_helper_is_packaged(t_mod):
    helper = t_mod._onboarding()
    assert {'run', 'needed'} <= helper.keys()


class Rail:
    choice = 'project'
    restored = 0
    def raw(self):
        pass
    def pick(self, label, rows, **kwargs):
        assert rows[-1][0] == '__discover__'
        return self.choice
    def restore(self):
        type(self).restored += 1


@pytest.mark.parametrize('agent', [None, 'claude', 'codex'])
def test_session_entry_reuses_guarded_shell_launcher(t_mod, monkeypatch, tmp_path, agent):
    cfg = SimpleNamespace(repos={'project': str(tmp_path)})
    monkeypatch.setattr(t_mod, '_RailUI', Rail)
    monkeypatch.setattr(Rail, 'choice', 'project')
    monkeypatch.setattr(Rail, 'restored', 0)
    calls = []
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda argv: calls.append(argv) or SimpleNamespace(returncode=7))
    assert t_mod._onboarding_start(cfg, agent) == 7
    assert calls[0][:2] == ['zsh', '-lic']
    assert 'source ' in calls[0][2] and 't.plugin.zsh' in calls[0][2]
    assert '_t_open project --local' in calls[0][2]
    assert ('--new --' + agent in calls[0][2]) if agent else '--new' not in calls[0][2]
    assert Rail.restored == 1


@pytest.mark.parametrize('choice,expected', [(None, 130), ('__discover__', 0)])
def test_session_entry_can_cancel_or_rediscover(t_mod, monkeypatch, choice, expected):
    monkeypatch.setattr(t_mod, '_RailUI', Rail)
    monkeypatch.setattr(Rail, 'choice', choice)
    calls = []
    monkeypatch.setattr(t_mod, '_onboarding', lambda: {'run': lambda c, a, **kw: calls.append(kw) or 0})
    assert t_mod._onboarding_start(SimpleNamespace(repos={})) == expected
    assert calls == ([{'force': True}] if choice else [])


@pytest.mark.parametrize('answer', ['1', 'q', '999', EOFError(), KeyboardInterrupt()])
def test_session_entry_plain_fallback(t_mod, monkeypatch, tmp_path, answer):
    def unavailable():
        raise OSError('not a terminal')
    monkeypatch.setattr(t_mod, '_RailUI', unavailable)
    def prompt(_):
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr('builtins.input', prompt)
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda _: SimpleNamespace(returncode=0))
    assert t_mod._onboarding_start(SimpleNamespace(repos={'project': str(tmp_path)})) == (0 if answer == '1' else 130)


def test_session_entry_restores_raw_failure_and_reports_launch_failure(t_mod, monkeypatch, tmp_path):
    class BrokenRail(Rail):
        def raw(self):
            raise OSError('raw failed')
    monkeypatch.setattr(t_mod, '_RailUI', BrokenRail)
    monkeypatch.setattr('builtins.input', lambda _: '1')
    def fail(_):
        raise OSError('missing zsh')
    monkeypatch.setattr(t_mod.subprocess, 'run', fail)
    assert t_mod._onboarding_start(SimpleNamespace(repos={'project': str(tmp_path)})) == 1
    assert BrokenRail.restored > 0
