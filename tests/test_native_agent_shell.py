"""Native agent launch paths work before the user's shell PATH is refreshed."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")
pytestmark = pytest.mark.skipif(not ZSH, reason="zsh is required")


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@"\n')
    path.chmod(0o755)
    return path


def run_shell(tmp_path, code, path="/usr/bin:/bin"):
    home = tmp_path / "home with spaces and 'quotes'"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "ZDOTDIR": str(home), "PATH": path,
           "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_CACHE_HOME": str(home / ".cache"),
           "TMUX_TMPDIR": str(tmp_path / "tmux"), "T_RECOVERY_DISABLE": "1"}
    script = (f"source {shlex.quote(str(ROOT / 'zsh/agent.zsh'))}; "
              "typeset -gA DEV_MODEL DEV_EFFORT DEV_FAST; " + code)
    return subprocess.run([ZSH, "-f", "-c", script], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["new", "resume"])
@pytest.mark.parametrize("on_path", [False, True])
def test_native_agent_generated_commands_execute_with_quoted_arguments(tmp_path, agent, mode, on_path):
    home = tmp_path / "home with spaces and 'quotes'"
    native = executable(home / ".local/bin" / agent)
    preferred = executable(tmp_path / "preferred bin" / agent)
    path = f"{preferred.parent}:/usr/bin:/bin" if on_path else "/usr/bin:/bin"
    sid = "session; $(touch unexpected)"
    extra = "directory with spaces; $(touch unexpected)"
    arguments = [agent, sid] + (["--cd", extra] if mode == "resume" else [])
    result = run_shell(tmp_path, f"_dev_agent_check {agent} || exit; "
                       f'eval "$(_dev_agent_{mode}_cmd {shlex.join(arguments)})"; '
                       'print -r -- "PATH=$PATH"', path)
    assert result.returncode == 0, result.stderr
    expected = [str(preferred if on_path else native)]
    if mode == "new":
        expected += ["--session-id", sid] if agent == "claude" else ["--no-daemon"]
    else:
        expected += (["-r", sid] if agent == "claude" else ["resume", sid]) + ["--cd", extra]
        if agent == "codex":
            expected += ["--no-daemon"]
    assert result.stdout.splitlines() == expected + [f"PATH={path}"]
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_fallback_preserves_shell_wrappers(tmp_path, agent):
    executable(tmp_path / "home with spaces and 'quotes'" / ".local/bin" / agent)
    result = run_shell(tmp_path, f'{agent}() {{ print -r -- "wrapper:$*"; }}; '
                       f'_dev_agent_check {agent} || exit; '
                       f'eval "$(_dev_agent_resume_cmd {agent} sid)"')
    assert result.returncode == 0, result.stderr
    expected = "wrapper:-r sid" if agent == "claude" else "wrapper:resume sid --no-daemon"
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("state", ["missing", "not-executable", "directory", "broken-link"])
def test_native_agent_check_rejects_unusable_fallback(tmp_path, state):
    native = tmp_path / "home with spaces and 'quotes'" / ".local/bin/claude"
    native.parent.mkdir(parents=True)
    if state == "not-executable":
        native.write_text("#!/bin/sh\nexit 0\n")
        native.chmod(0o644)
    elif state == "directory":
        native.mkdir()
    elif state == "broken-link":
        native.symlink_to(native.parent / "missing-target")
    result = run_shell(tmp_path, "_dev_agent_check claude")
    assert result.returncode == 1
    assert "t agent install claude" in result.stderr
    assert not result.stdout


def test_native_fallback_does_not_resolve_arbitrary_paths(tmp_path):
    target = executable(tmp_path / "home with spaces and 'quotes'" / ".local/other")
    result = run_shell(tmp_path, "_dev_agent_executable ../other")
    assert result.returncode == 1
    assert not result.stdout
    assert os.access(target, os.X_OK)


def checkpoint(tmp_path):
    import json,runpy
    home=tmp_path / "home with spaces and 'quotes'";home.mkdir(exist_ok=True)
    w=runpy.run_path(str(ROOT/'libexec/t_workflow.py'))
    sync=runpy.run_path(str(ROOT/'libexec/t_workflow_sync.py'))
    doc=w['defaults']()
    doc['workflows']['standard']['stages_enabled'] = False  # Exercise account intent without a scoped stage-model override.
    config=home/'.config/t';config.mkdir(parents=True)
    (config/'workflow.json').write_text(json.dumps(doc))
    (config/'workflow-propagation.json').write_text(json.dumps({'revision':1,'workflow_hash':sync['digest'](doc),
        'context':{},'main_settings':{'model':'preferred-main','effort':'medium'},'settings':{},'attempts':0,'runtime':'pending'}))
    return home


@pytest.mark.parametrize('explicit',[False,True])
def test_guarded_resume_applies_checkpoint_with_explicit_override_precedence(tmp_path,explicit):
    home=checkpoint(tmp_path);native=executable(home/'.local/bin/codex')
    sid='33333333-3333-3333-3333-333333333333'
    extra=' --model deliberate -c model_reasoning_effort=high' if explicit else ''
    result=run_shell(tmp_path,f'eval "$(_dev_agent_resume_cmd codex {sid}{extra})"')
    assert result.returncode==0,result.stderr
    expected=([str(native),'resume',sid,'--model','deliberate','-c','model_reasoning_effort=high','--no-daemon'] if explicit else
              [str(native),'resume',sid,'--model','preferred-main','-c','model_reasoning_effort="medium"','--no-daemon'])
    assert result.stdout.splitlines()==expected
    assert 'service_tier' not in result.stdout and 'permissions' not in result.stdout


def test_live_attach_notice_does_not_launch_another_agent(tmp_path):
    home=checkpoint(tmp_path);executable(home/'.local/bin/codex')
    result=run_shell(tmp_path,'_dev_workflow_attach codex; print done')
    assert result.returncode==0 and result.stdout=='done\n'
    assert 'live attach keeps this owner' in result.stderr


@pytest.mark.parametrize('extra',[['-c','model=deliberate'],['--config=model=deliberate'],['-mdeliberate'],['-cmodel=deliberate']])
def test_guarded_resume_respects_all_explicit_model_forms(tmp_path,extra):
    home=checkpoint(tmp_path);executable(home/'.local/bin/codex')
    sid='44444444-4444-4444-4444-444444444444'
    result=run_shell(tmp_path,f'eval "$(_dev_agent_resume_cmd codex {sid} {shlex.join(extra)})"')
    assert result.returncode==0,result.stderr
    assert 'preferred-main' not in result.stdout and '--model\n' not in result.stdout
    assert result.stdout.splitlines()[3:5]==['-c','model_reasoning_effort="medium"']
    assert result.stdout.splitlines()[5:-1]==extra


def test_resume_selected_codex_home_checks_that_index_for_child(tmp_path):
    import sqlite3
    home=checkpoint(tmp_path);executable(home/'.local/bin/codex')
    selected=tmp_path/'selected-home';selected.mkdir()
    sid='55555555-5555-5555-5555-555555555555'
    with sqlite3.connect(selected/'state_5.sqlite') as db:
        db.execute('create table threads(id text,source text)');db.execute('insert into threads values(?,?)',(sid,'{"subAgent":{}}'))
    result=run_shell(tmp_path,f'CODEX_HOME={shlex.quote(str(selected))} _dev_agent_resume_cmd codex {sid}')
    assert result.returncode==0 and 'preferred-main' not in result.stdout
    assert 'model_reasoning_effort' not in result.stdout
    source=(ROOT/'zsh/resume.zsh').read_text()
    assert 'resume_command=$(CODEX_HOME="$codex_home" _dev_agent_resume_cmd codex "$sid" --cd "$dir") ||' in source
