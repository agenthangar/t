"""Batch resume keeps the rich picker rows and attaches existing live owners."""

import pathlib
import re

from test_agent_seam import zsh  # noqa: F401 — sandboxed shell fixture


FZF_ALL = """#!/usr/bin/env python3
import os, sys, subprocess
from pathlib import Path
if '--help' in sys.argv:
    print('--id-nth')
    sys.exit()
rows = sys.stdin.read().splitlines()
Path(os.environ['FZF_LOG'] + '.initial').write_text('\\n'.join(rows))
for index, action in enumerate(filter(None, os.environ.get('PICK_ACTIONS', '').split(','))):
    subprocess.run(['sh', os.environ['T_PICKER_TOGGLE'], action], check=True)
    rows = Path(os.environ['T_PICKER_CURRENT']).read_text().splitlines()
    Path(os.environ['FZF_LOG'] + f'.step{index}').write_text('\\n'.join(rows))
    Path(os.environ['FZF_LOG'] + f'.header{index}').write_text(Path(os.environ['T_PICKER_CURRENT_HEADER']).read_text())
with open(os.environ['FZF_LOG'] + '.args', 'w') as args:
    args.write('\\n'.join(sys.argv[1:]))
with open(os.environ['FZF_LOG'], 'w') as log:
    for row in rows:
        log.write(row.split('\\t')[-1] + '\\n')
for row in rows:
    print(row)
if os.environ.get('PICK_CANCEL'):
    sys.exit(1)
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
    assert "--with-nth=-1" in (tmp_path / "fzf.log.args").read_text()


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


def test_resume_default_filters_live_rows_without_dropping_their_details(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    wt3 = zsh.home / "code" / ".worktrees" / "api" / "3"
    wt4 = zsh.home / "code" / ".worktrees" / "api" / "4"
    wt3.mkdir(parents=True)
    wt4.mkdir(parents=True)
    proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt3))
    proj.mkdir(parents=True)
    (proj / "c3.jsonl").write_text('{"type":"user","message":{"content":"Dead work"}}\n')
    tab = "\t"
    files = tmp_path / "picker"
    shell = (
        f"_dev_session_rows() {{ print -r -- '-{tab}{wt4}{tab}api-4{tab}detached{tab}active{tab}Live work{tab}codex'; }}; "
        f"_t_fzf_filtered() {{ print -r -- \"$1\" > {files}.filtered; "
        f"print -r -- \"$2\" > {files}.all; print -r -- \"$6\" > {files}.show; return 1; }}; "
    )
    env = dict(FAKE_SESSIONS="dev-api-4", FAKE_SESSION_PATH=str(wt4))
    r = zsh(shell + "_t_resume api; echo rc=$?", _tty=True, **env)
    assert "rc=1" in r.stdout, r.stdout
    filtered = pathlib.Path(f"{files}.filtered").read_text()
    all_rows = pathlib.Path(f"{files}.all").read_text()
    assert "Dead work" in filtered and "Live work" not in filtered
    assert "Dead work" in all_rows and "Live work" in all_rows and "⬡ codex" in all_rows
    assert pathlib.Path(f"{files}.show").read_text().strip() == ""
    r = zsh(shell + "_t_resume api --live; echo rc=$?", _tty=True, **env)
    assert "rc=1" in r.stdout, r.stdout
    assert pathlib.Path(f"{files}.show").read_text().strip() == "1"
    restored = pathlib.Path(f"{files}.filtered").read_text()
    all_rows = pathlib.Path(f"{files}.all").read_text()
    assert "Dead work" in restored and "Live work" not in restored
    assert "Dead work" in all_rows and "Live work" in all_rows
    # Non-interactive use has no Ctrl-A; its sole inactive row keeps the old
    # automatic resume behavior even though a live row was excluded.
    zsh.log.write_text("")
    r = zsh(shell + "_t_resume api; echo rc=$?", **env)
    assert "rc=0" in r.stdout and "Resuming c3 in dev-api-3" in r.stdout, (r.stdout, r.stderr)
    assert "claude -r c3" in zsh.log.read_text()


def test_resume_live_only_still_opens_toggleable_picker(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    wt = zsh.home / "code" / ".worktrees" / "api" / "4"
    wt.mkdir(parents=True)
    tab = "\t"
    files = tmp_path / "picker"
    shell = (
        f"_dev_session_rows() {{ print -r -- '-{tab}{wt}{tab}api-4{tab}detached{tab}active{tab}Live work{tab}codex'; }}; "
        f"_t_fzf_filtered() {{ print -r -- \"$1\" > {files}.filtered; "
        f"print -r -- \"$2\" > {files}.all; return 1; }}; "
        "_t_dev() { echo UNEXPECTED-ATTACH; }; "
    )
    r = zsh(shell + "_t_resume api; echo rc=$?", _tty=True,
            FAKE_SESSIONS="dev-api-4", FAKE_SESSION_PATH=str(wt))
    assert "rc=1" in r.stdout and "UNEXPECTED-ATTACH" not in r.stdout, r.stdout
    assert pathlib.Path(f"{files}.filtered").read_text().strip() == ""
    assert "Live work" in pathlib.Path(f"{files}.all").read_text()


def test_resume_live_hotkey_hides_live_without_losing_saved_rows(zsh, tmp_path):
    stub = tmp_path / "stubbin" / "fzf"
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    wt3 = zsh.home / "code" / ".worktrees" / "api" / "3"
    wt4 = zsh.home / "code" / ".worktrees" / "api" / "4"
    wt3.mkdir(parents=True)
    wt4.mkdir(parents=True)
    proj = zsh.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt3))
    proj.mkdir(parents=True)
    (proj / "c3.jsonl").write_text('{"type":"user","message":{"content":"Dead work"}}\n')
    tab = "\t"
    shell = f"_dev_session_rows() {{ print -r -- '-{tab}{wt4}{tab}api-4{tab}detached{tab}active{tab}Live work{tab}codex'; }}; "
    log = tmp_path / "fzf.log"
    result = zsh(shell + "_t_resume api --live; echo rc=$?", _tty=True,
                 FAKE_SESSIONS="dev-api-4", FAKE_SESSION_PATH=str(wt4),
                 FZF_LOG=str(log), PICK_ACTIONS="first", PICK_CANCEL="1")
    assert "rc=1" in result.stdout, result.stdout
    initial = pathlib.Path(f"{log}.initial").read_text()
    filtered = pathlib.Path(f"{log}.step0").read_text()
    assert "Live work" in initial and "Dead work" in initial
    assert "Live work" not in filtered and "Dead work" in filtered
    assert "live: hidden" in pathlib.Path(f"{log}.header0").read_text()
    assert "send-keys" not in (zsh.log.read_text() if zsh.log.exists() else "")


def test_resume_last_turn_overrides_mtime_and_sort_hotkeys_keep_rows(zsh, tmp_path):
    import json
    import os
    stub = tmp_path / 'stubbin' / 'fzf'
    stub.write_text(FZF_ALL)
    stub.chmod(0o755)
    for slot, day, modified in ((2, 22, 1_800_000_000), (10, 20, 1_900_000_000)):
        wt = zsh.home / 'code' / '.worktrees' / 'api' / str(slot)
        wt.mkdir(parents=True)
        project = zsh.home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(wt))
        project.mkdir(parents=True)
        transcript = project / f'c{slot}.jsonl'
        transcript.write_text(json.dumps({'type': 'user', 'timestamp': f'2026-10-{day}T10:00:00Z',
                                         'message': {'content': f'Task {slot}'}}, separators=(',', ':')) + '\n')
        os.utime(transcript, (modified, modified))
    log = tmp_path / 'fzf.log'
    result = zsh('_t_resume api --days all; echo rc=$?', _tty=True, FZF_LOG=str(log),
                 PICK_ACTIONS='sort-session,sort-repo,sort-agent,sort-host,sort-turn,all,default', PICK_CANCEL='1')
    assert 'rc=1' in result.stdout, result.stdout
    assert pathlib.Path(f'{log}.initial').exists(), result.stdout
    initial = pathlib.Path(f'{log}.initial').read_text().splitlines()
    assert [row.split('\t')[1] for row in initial] == ['2', '10']
    for index in range(7):
        assert pathlib.Path(f'{log}.step{index}').read_text().splitlines() == initial
    arguments = pathlib.Path(f'{log}.args').read_text()
    assert '--no-sort' in arguments
    for letter in ('t', 'r', 'g', 'o', 'v'):
        assert 'alt-' + letter + ':' in arguments
    assert 'Last:' in log.read_text()
    # Noninteractive explicit-slot fallback also picks the actual latest turn.
    # Move both transcripts under the same slot, retaining their opposed mtimes.
    project2 = zsh.home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(zsh.home / 'code/.worktrees/api/2'))
    project10 = zsh.home / '.claude/projects' / re.sub(r'[^A-Za-z0-9]', '-', str(zsh.home / 'code/.worktrees/api/10'))
    (project10 / 'c10.jsonl').rename(project2 / 'c10.jsonl')
    result = zsh('_t_resume api 2 --days all; echo rc=$?')
    assert 'Resuming c2 in dev-api-2' in result.stdout, result.stdout
