"""Batch resume keeps the rich picker rows and attaches existing live owners."""

import pathlib
import re

from test_agent_seam import zsh  # noqa: F401 — sandboxed shell fixture


FZF_ALL = """#!/usr/bin/env python3
import os, sys
rows = sys.stdin.read().splitlines()
with open(os.environ['FZF_LOG'] + '.args', 'w') as args:
    args.write('\\n'.join(sys.argv[1:]))
with open(os.environ['FZF_LOG'], 'w') as log:
    for row in rows:
        log.write(row.split('\\t')[-1] + '\\n')
for row in rows:
    print(row)
"""


def test_resume_multi_opens_selected_live_slot_and_revives_dead_slot(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    wt3 = zsh.home / "code" / ".worktrees" / "api" / "3"
    wt4 = zsh.home / "code" / ".worktrees" / "api" / "4"
    wt3.mkdir(parents=True)
    wt4.mkdir(parents=True)
    proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt3))
    proj.mkdir(parents=True)
    (proj / "c1.jsonl").write_text('{"type":"user","message":{"content":"Fix login"}}\n')
    log = tmp_path / "fzf.log"
    live = (f"_dev_session_rows() {{ print -r -- '-\t{wt4}\tapi-4\tdetached\tactive\tLive work\tcodex'; }}; "
            "_dev_open_tab() { echo \"TAB $*\"; }; ")
    r = zsh(live + "_t_resume api --live; echo rc=$?", _tty=True, FZF_LOG=str(log),
            FAKE_SESSIONS="dev-api-4", FAKE_SESSION_PATH=str(wt4))
    assert "rc=0" in r.stdout, r.stdout
    assert "TAB t open api 4 --cli --local" in r.stdout, r.stdout
    assert "Resuming c1 in dev-api-3" in r.stdout, r.stdout
    assert "attach-session -t =dev-api-3:" in zsh.log.read_text()
    assert "claude -r c1" in zsh.log.read_text()
    assert "codex resume" not in zsh.log.read_text()  # the live row keeps its owner
    rows = log.read_text().splitlines()
    assert any("4" in row and "⬡ codex" in row and "● active" in row and "Live work" in row for row in rows)
    assert any("3" in row and "✱ claude" in row and "Fix login" in row for row in rows)
    args = (tmp_path / "fzf.log.args").read_text()
    assert "--with-nth=-1" in args and "--preview=printf \"%s\\n\" {11}" in args
    assert "--bind=ctrl-/:toggle-preview" in args and "Ctrl-/ details" in args


def test_resume_multi_fg_guard_prevents_any_revive(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    for n in (3, 5):
        wt = zsh.home / "code" / ".worktrees" / "api" / str(n)
        wt.mkdir(parents=True)
        proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt))
        proj.mkdir(parents=True)
        (proj / f"c{n}.jsonl").write_text(f'{{"type":"user","message":{{"content":"Task {n}"}}}}\n')
    r = zsh("_t_resume api --fg; echo rc=$?", _tty=True, FZF_LOG=str(tmp_path / "fzf.log"))
    assert "mark a single row" in r.stdout and "rc=1" in r.stdout, r.stdout
    assert "send-keys" not in (zsh.log.read_text() if zsh.log.exists() else "")


def test_resume_multi_all_live_attaches_first_and_tabs_second(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    wt4 = zsh.home / "code" / ".worktrees" / "api" / "4"
    wt5 = zsh.home / "code" / ".worktrees" / "api" / "5"
    wt4.mkdir(parents=True)
    wt5.mkdir(parents=True)
    tab = "\t"
    shell = (
        f"tmux() {{ if [[ $1 == list-sessions ]]; then print -rl dev-api-4 dev-api-5; "
        f"elif [[ $1 == display-message ]]; then [[ $* == *dev-api-4* ]] && print -r -- {wt4} || print -r -- {wt5}; "
        "else command tmux \"$@\"; fi; }; "
        "_dev_repo_slots() { print -rl 4 5; }; "
        f"_dev_session_rows() {{ print -r -- '-{tab}{wt4}{tab}api-4{tab}detached{tab}active{tab}First live{tab}codex'; "
        f"print -r -- '-{tab}{wt5}{tab}api-5{tab}detached{tab}active{tab}Second live{tab}claude'; }}; "
        "_dev_open_tab() { echo \"TAB $*\"; }; _t_dev() { echo \"ATTACH $*\"; }; "
    )
    r = zsh(shell + "_t_resume api --live; echo rc=$?", _tty=True,
            FZF_LOG=str(tmp_path / "fzf.log"))
    assert "rc=0" in r.stdout, r.stdout
    assert ("ATTACH api 4" in r.stdout and "TAB t open api 5 --cli --local" in r.stdout) or (
        "ATTACH api 5" in r.stdout and "TAB t open api 4 --cli --local" in r.stdout
    ), r.stdout
    assert "send-keys" not in (zsh.log.read_text() if zsh.log.exists() else "")


def test_resume_multi_host_opens_extra_landings_on_exact_host(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    for n in (3, 5):
        wt = zsh.home / "code" / ".worktrees" / "api" / str(n)
        wt.mkdir(parents=True)
        proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt))
        proj.mkdir(parents=True)
        (proj / f"c{n}.jsonl").write_text(f'{{"type":"user","message":{{"content":"Task {n}"}}}}\n')
    shell = (
        "REMOTE_HOSTS[mini]=mini.local; _dev_rows_all() { :; }; "
        "_t_resume_on_host() { echo dev-api-${3:t}; }; "
        "_dev_open_tab() { echo \"TAB $*\"; }; "
    )
    # ssh is stubbed: no remote host, dotfiles, or credentials are involved.
    ssh = tmp_path / "stubbin" / "ssh"
    ssh.write_text("#!/bin/sh\nprintf 'SSH %s\\n' \"$*\"\n")
    ssh.chmod(0o755)
    r = zsh(shell + "_t_resume api --host mini; echo rc=$?", _tty=True,
            FZF_LOG=str(tmp_path / "fzf.log"))
    assert "rc=0" in r.stdout, r.stdout
    assert "TAB t open api 5 --cli --host mini" in r.stdout or "TAB t open api 3 --cli --host mini" in r.stdout
    assert "SSH -t mini.local zsh -lic" in r.stdout, r.stdout


def test_resume_multi_remote_foreground_uses_its_owner_host(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    for n in (3, 5):
        wt = zsh.home / "code" / ".worktrees" / "api" / str(n)
        wt.mkdir(parents=True)
        proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt))
        proj.mkdir(parents=True)
        (proj / f"c{n}.jsonl").write_text(f'{{"type":"user","message":{{"content":"Task {n}"}}}}\n')
    wt3 = zsh.home / "code" / ".worktrees" / "api" / "3"
    tab = "\t"
    shell = (
        "REMOTE_HOSTS[mini]=mini.local; "
        f"_dev_rows_all() {{ print -r -- 'mini{tab}c3{tab}{wt3}{tab}api:fg{tab}active{tab}Live FG{tab}claude'; }}; "
        "_dev_open_tab() { echo \"TAB $*\"; }; "
    )
    r = zsh(shell + "_t_resume api --live; echo rc=$?", _tty=True,
            FZF_LOG=str(tmp_path / "fzf.log"))
    assert "rc=0" in r.stdout, r.stdout
    assert "TAB t open api:fg --cli --host mini" in r.stdout, r.stdout
    assert "claude -r c3" not in zsh.log.read_text()


def test_resume_multi_only_remote_foreground_attaches_on_exact_host(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    rows = []
    tab = "\t"
    for n in (3, 5):
        wt = zsh.home / "code" / ".worktrees" / "api" / str(n)
        wt.mkdir(parents=True)
        proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt))
        proj.mkdir(parents=True)
        (proj / f"c{n}.jsonl").write_text(f'{{"type":"user","message":{{"content":"Task {n}"}}}}\n')
        rows.append(f"print -r -- 'mini{tab}c{n}{tab}{wt}{tab}api:fg{n}{tab}active{tab}Live {n}{tab}claude';")
    shell = (
        "REMOTE_HOSTS[mini]=mini.local; _dev_rows_all() { " + " ".join(rows) + " }; "
        "_dev_open_tab() { echo \"TAB $*\"; }; "
        "_dev_remote_open() { echo \"REMOTE $*\"; }; "
    )
    r = zsh(shell + "_t_resume api --live; echo rc=$?", _tty=True,
            FZF_LOG=str(tmp_path / "fzf.log"))
    assert "rc=0" in r.stdout, r.stdout
    assert ("REMOTE mini api:fg3 --cli" in r.stdout and "TAB t open api:fg5 --cli --host mini" in r.stdout) or (
        "REMOTE mini api:fg5 --cli" in r.stdout and "TAB t open api:fg3 --cli --host mini" in r.stdout
    ), r.stdout
