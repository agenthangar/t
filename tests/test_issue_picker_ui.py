"""Issue browsing uses the shared fzf multi-select interaction."""

import os
from pathlib import Path
import subprocess


PICKER = Path(__file__).resolve().parents[1] / "ui" / "fzf.sh"

FZF_STUB = """#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys

if '--help' in sys.argv:
    if not os.environ.get('FZF_LEGACY'):
        print('--id-nth')
    sys.exit(0)
log = Path(os.environ['PICKER_LOG'])
log.write_text('\\n'.join(sys.argv[1:]))
(log.parent / 'initial-rows').write_text(sys.stdin.read())
(log.parent / 'all-rows').write_text(Path(os.environ['T_PICKER_ALL']).read_text())
(log.parent / 'default-header').write_text(Path(os.environ['T_PICKER_HEADER_DEFAULT']).read_text())
(log.parent / 'all-header').write_text(Path(os.environ['T_PICKER_HEADER_ALL']).read_text())
actions = []
for key in filter(None, os.environ.get('PICKER_ACTIONS', '').split(',')):
    action = subprocess.check_output(['sh', os.environ['T_PICKER_TOGGLE'], key], text=True)
    actions.append(action)
    rows = Path(os.environ['T_PICKER_CURRENT']).read_text().splitlines()
if actions:
    (log.parent / 'toggle-action').write_text('\\n'.join(actions))
    (log.parent / 'current-header').write_text(Path(os.environ['T_PICKER_CURRENT_HEADER']).read_text())
else:
    rows = (log.parent / 'initial-rows').read_text().splitlines()
for row in rows:
    if row.split('\\t')[1] in os.environ.get('PICKER_IDS', '').split(','):
        print(row)
if os.environ.get('PICKER_CANCEL'):
    sys.exit(130)
"""


def picker(tmp_path, *, ids="42", actions=(), cancel=False, legacy=False, all_label="All issues"):
    stubbin = tmp_path / "stubbin"
    stubbin.mkdir()
    stub = stubbin / "fzf"
    stub.write_text(FZF_STUB)
    stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        PATH=str(stubbin) + os.pathsep + env.get("PATH", ""),
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CACHE_HOME=str(home / ".cache"),
        TMPDIR=str(tmp_path),
        T_PICKER_ALL_LABEL=all_label,
        PICKER_LOG=str(tmp_path / "fzf-args"),
        PICKER_IDS=ids,
        PICKER_ACTIONS=",".join(actions),
        PICKER_CANCEL="1" if cancel else "",
        FZF_LEGACY="1" if legacy else "",
    )
    # Issue rows keep stable hidden keys before the full detail column.
    default = "agenthangar/t\t42\topen\t#42 Fix login · no owner"
    all_rows = default + "\nagenthangar/t\t43\tclosed\t#43 Improve docs · closed"
    cmd = [
        "sh", "-c",
        '. "$1"; _t_fzf_filtered "$2" "$3" start "Open issues" "$4" ""',
        "sh", str(PICKER), default, all_rows, "Space/Tab mark issues to start",
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def test_issue_picker_uses_shared_multiselect_and_preserves_full_rows(tmp_path):
    result = picker(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "agenthangar/t\t42\topen\t#42 Fix login · no owner\n"
    args = (tmp_path / "fzf-args").read_text()
    assert "--multi" in args
    assert "--id-nth=1,2,3" in args
    assert "--with-nth=-1" in args
    assert "enter:accept,esc:abort,ctrl-c:abort,space:toggle+down,tab:toggle+down" in args
    assert "ctrl-/:toggle-preview" in args
    assert 'ctrl-a:execute-silent(sh "$T_PICKER_TOGGLE" all)+reload-sync(cat "$T_PICKER_CURRENT")' in args
    assert 'ctrl-f:execute-silent(sh "$T_PICKER_TOGGLE" default)+reload-sync(cat "$T_PICKER_CURRENT")' in args
    assert 'transform-header(cat "$T_PICKER_CURRENT_HEADER")' in args
    assert "--preview-window=hidden:wrap:down:40%" in args
    assert "Enter start · Esc · Space/Tab mark ✓ · Ctrl-/ details" in (tmp_path / "default-header").read_text()
    assert "Open issues · Ctrl-A show all · Ctrl-F defaults · 1 hidden" in (tmp_path / "default-header").read_text()
    assert "All issues · Ctrl-A show all · Ctrl-F defaults" in (tmp_path / "all-header").read_text()
    assert list(tmp_path.glob("t-picker.*")) == []


def test_issue_picker_ctrl_a_exposes_filtered_issues(tmp_path):
    result = picker(tmp_path, ids="42,43", actions=("all",))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "agenthangar/t\t42\topen\t#42 Fix login · no owner",
        "agenthangar/t\t43\tclosed\t#43 Improve docs · closed",
    ]
    assert "All issues · Ctrl-A show all · Ctrl-F defaults" in (tmp_path / "current-header").read_text()


def test_issue_picker_ctrl_f_restores_filter_and_clears_old_fzf_marks(tmp_path):
    result = picker(tmp_path, ids="42,43", actions=("all", "default"), legacy=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["agenthangar/t\t42\topen\t#42 Fix login · no owner"]
    assert "Open issues · Ctrl-A show all" in (tmp_path / "current-header").read_text()
    args = (tmp_path / "fzf-args").read_text()
    assert 'ctrl-a:deselect-all+execute-silent(sh "$T_PICKER_TOGGLE" all)' in args
    assert 'ctrl-f:deselect-all+execute-silent(sh "$T_PICKER_TOGGLE" default)' in args


def test_issue_picker_escape_cancels_without_leaking_private_files(tmp_path):
    result = picker(tmp_path, cancel=True, ids="")
    assert result.returncode == 130
    assert not result.stdout
    assert list(tmp_path.glob("t-picker.*")) == []


def test_named_picker_profiles_own_cardinality_controls_and_theme(tmp_path):
    stubbin = tmp_path / "stubbin"
    stubbin.mkdir()
    stub = stubbin / "fzf"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PICKER_ARGS\"\ncat\n")
    stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    env.update(PATH=f"{stubbin}{os.pathsep}{env.get('PATH', '')}", PICKER_ARGS=str(tmp_path / "args"))
    for profile, arguments, expected in (
        ("_t_fzf_single", "open 'Cursor chats'", "--no-multi"),
        ("_t_fzf_multi", "'open tabs' 'Remote sessions'", "--multi"),
    ):
        result = subprocess.run(
            ["sh", "-c", f'. "$1"; printf "one\\ntwo\\n" | {profile} {arguments} --prompt="pick > "',
             "sh", str(PICKER)], env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "one\ntwo\n"
        args = (tmp_path / "args").read_text()
        assert expected in args and "--margin=1,2" in args and "--pointer=▸" in args
        assert " · Esc" in args and "--prompt=pick > " in args
        forbidden = subprocess.run(
            ["sh", "-c", f'. "$1"; {profile} {arguments} --bind=space:accept', "sh", str(PICKER)],
            env=env, capture_output=True, text=True,
        )
        assert forbidden.returncode == 2
        assert "unsupported content option" in forbidden.stderr


def test_named_picker_empty_selection_returns_130(tmp_path):
    stubbin = tmp_path / "stubbin"
    stubbin.mkdir()
    stub = stubbin / "fzf"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    env["PATH"] = f"{stubbin}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        ["sh", "-c", '. "$1"; _t_fzf_single open ""', "sh", str(PICKER)],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 130
