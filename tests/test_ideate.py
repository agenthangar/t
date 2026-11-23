"""Exercise the public idea commands with isolated agent and GitHub fixtures."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from pty_helpers import run_terminal
from test_onboarding_pty import ROOT, sandbox


def git(env, *args, cwd=None):
    return subprocess.run(['git', *map(str, args)], cwd=cwd, env=env, check=True,
                          text=True, capture_output=True).stdout.strip()


def executable(path, program):
    path.write_text(f'#!{sys.executable}\n' + program)
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    home, env = sandbox(tmp_path)
    tools = tmp_path / 'bin'
    tools.mkdir()
    env['PATH'] = os.pathsep.join((str(tools), str(ROOT / 'bin'), env['PATH']))
    env['IDEA_FIXTURE_PATH'] = env['PATH']
    env['T_HOME'] = str(ROOT)
    env['GH_CONFIG_DIR'] = str(home / 'gh')
    (home / '.zshrc').write_text('export PATH="$IDEA_FIXTURE_PATH"\nsource "$T_HOME/t.plugin.zsh"\n')
    for name in ('GH_HOST', 'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN',
                 'GITHUB_ENTERPRISE_TOKEN', 'TMUX_PANE', 'T_SETUP_SHIM'):
        env.pop(name, None)
    env.update(GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
               GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid',
               IDEA_AGENT_LOG=str(tmp_path / 'agent.jsonl'))
    # A missing or misrouted remote must stay local in every test.
    for name in ('gh', 'ssh', 'codex'):
        executable(tools / name, 'raise SystemExit("unexpected external command")\n')
    executable(tools / 'claude', '''import json, os, pathlib, sys, termios, time
args = sys.argv[1:]
assert sys.stdin.isatty() and sys.stdout.isatty()
assert termios.tcgetattr(0)[3] & termios.ICANON
workspace = pathlib.Path.cwd()
assert ".local/state" in str(workspace)
assert not (workspace / ".git").exists()
assert not os.environ.get("GIT_DIR")
assert not os.environ.get("GIT_WORK_TREE")
assert not os.environ.get("CLAUDE_CODE_SESSION_ID")
with open(os.environ["IDEA_AGENT_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "cwd": str(workspace)}) + "\\n")
if os.environ.get("IDEA_FAIL_START"):
    print("Fixture agent startup failed before saving a conversation", file=sys.stderr)
    raise SystemExit(17)
if "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
    config = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR", pathlib.Path.home() / ".claude"))
    directory = config / "projects" / "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in str(workspace))
    directory.mkdir(parents=True, exist_ok=True)
    transcript = directory / (sid + ".jsonl")
    transcript.write_text(json.dumps({"type": "user", "sessionId": sid, "cwd": str(workspace),
                                     "message": {"role": "user", "content": "Private brainstorming"}}) + "\\n")
    (workspace / "publish/docs").mkdir(parents=True, exist_ok=True)
    (workspace / "publish/README.md").write_bytes(b"Reviewed bytes\\r\\n")
    (workspace / "publish/docs/routes.txt").write_text("Oak Loop\\n")
    (workspace / "private-notes.txt").write_text("Do not publish the brainstorm.\\n")
print("ISOLATED NATIVE AGENT", flush=True)
if os.environ.get("IDEA_RELEASE"):
    until = time.monotonic() + 20
    while not pathlib.Path(os.environ["IDEA_RELEASE"]).exists() and time.monotonic() < until:
        time.sleep(.02)
''')
    return home, env, tools


def invoke(env, *words, cwd=None):
    return subprocess.run([sys.executable, str(ROOT / 'bin/t'), *words], env=env, cwd=cwd,
                          stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)


def terminal(env, words, cwd, actions, size=(82, 25)):
    return run_terminal([sys.executable, str(ROOT / 'bin/t'), *words], env, cwd, size, actions)


def ideas_on_disk(home):
    root = home / '.local/state'
    return sorted((str(entry.relative_to(root)), entry.read_bytes()) for entry in root.rglob('*.json'))


def test_help_and_nonterminal_actions_have_no_side_effects(box, tmp_path):
    home, env, _ = box
    help_result = invoke(env, 'idea', '--help', cwd=tmp_path)
    assert help_result.returncode == 0, help_result.stderr
    assert all(word in help_result.stdout for word in ('list', 'show', 'start', 'resume', 'publish'))
    assert invoke(env, 'ideate', '--help', cwd=tmp_path).returncode == 0
    for args in (('ideate',), ('idea', 'start', 'Offline'), ('idea', 'resume', 'idea-001'),
                 ('idea', 'publish', 'idea-001')):
        result = invoke(env, *args, cwd=tmp_path)
        assert 'unexpected external command' not in result.stdout + result.stderr
        assert not Path(env['IDEA_AGENT_LOG']).exists(), args
    assert not (home / '.local/state/t').exists()
    assert not (home / 'code').exists()


def test_readonly_commands_print_without_changing_state(box, tmp_path):
    home, env, _ = box
    result = invoke(env, 'idea', 'list', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'idea' in result.stdout.lower()
    missing = invoke(env, 'idea', 'show', 'idea-001', cwd=tmp_path)
    assert missing.returncode != 0
    assert not (home / '.local/state/t').exists()
    assert not Path(env['IDEA_AGENT_LOG']).exists()


def test_shell_routes_idea_commands_outside_git(box, tmp_path):
    _, env, _ = box
    result = subprocess.run(['zsh', '-lic', 't idea list && t ideate --help'],
                            env=env, cwd=tmp_path, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'idea' in result.stdout.lower()
    assert 'ideate' in result.stdout
    assert not Path(env['IDEA_AGENT_LOG']).exists()


def start_draft(env, cwd):
    code, output = terminal(env, ['idea', 'start', 'Neighborhood walks'], cwd,
                            [('Next step', b'j\r')])
    assert code == 0, output
    listing = invoke(env, 'idea', 'list', cwd=cwd)
    assert listing.returncode == 0, listing.stderr
    ident = re.search(r'\bidea-[a-f\d][a-f\d-]+\b', listing.stdout)
    assert ident and 'Neighborhood walks' in listing.stdout, listing.stdout
    return ident.group()


def test_starts_and_resumes_private_native_session(box, tmp_path):
    home, env, _ = box
    checkout = tmp_path / 'existing-repository'
    git(env, 'init', '-q', checkout)
    env.update(GIT_DIR=str(checkout / '.git'), GIT_WORK_TREE=str(checkout),
               CLAUDE_CODE_SESSION_ID='unrelated-session')
    ident = start_draft(env, checkout)
    code, output = terminal(env, ['idea', 'resume', ident], checkout,
                            [('Next step', b'j\r')])
    assert code == 0, output
    first, resumed = [json.loads(line) for line in Path(env['IDEA_AGENT_LOG']).read_text().splitlines()]
    assert 'design check under publish/ for my file review' in first['args'][-1]
    assert all('design check under publish/' not in arg for arg in resumed['args'])
    sid = first['args'][first['args'].index('--session-id') + 1]
    resume_flag = '--resume' if '--resume' in resumed['args'] else '-r'
    assert resumed['args'][resumed['args'].index(resume_flag) + 1] == sid
    assert resumed['cwd'] == first['cwd']
    workspace = Path(first['cwd'])
    assert workspace.is_relative_to(home.resolve())
    assert workspace.stat().st_mode & 0o077 == 0
    assert (workspace / 'publish/README.md').read_bytes() == b'Reviewed bytes\r\n'
    assert sorted(entry.name for entry in checkout.iterdir()) == ['.git']
    before = ideas_on_disk(home)
    for argv in (('idea', 'list'), ('idea', 'show', ident)):
        result = invoke(env, *argv, cwd=checkout)
        assert result.returncode == 0, result.stderr
        assert ident in result.stdout and 'Neighborhood walks' in result.stdout
        assert '\x1b[' not in result.stdout
    assert ideas_on_disk(home) == before


def test_codex_resume_uses_matching_workspace_conversation(box, tmp_path):
    home, env, tools = box
    config = home / '.config/t'
    config.mkdir(parents=True)
    for name in ('config.sh', 'local.zsh'):
        (config / name).write_text('DEV_AGENT_DEFAULT=codex\n')
    sid = 'b1ee7250-8331-439e-92f5-2222ff28ee21'
    env['IDEA_CODEX_SID'] = sid
    executable(tools / 'codex', '''import datetime, json, os, pathlib, sqlite3, sys, time
args = sys.argv[1:]
sid = os.environ["IDEA_CODEX_SID"]
workspace = pathlib.Path.cwd()
with open(os.environ["IDEA_AGENT_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "cwd": str(workspace)}) + "\\n")
if args[:1] != ["resume"]:
    root = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
    stamp = datetime.datetime.now()
    folder = root / "sessions" / stamp.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    (root / "thread-writer-locks").mkdir()
    db = sqlite3.connect(root / "state_5.sqlite")
    db.execute("create table threads (id, rollout_path, cwd, source, created_at, updated_at, "
               "name, title, first_user_message, archived)")
    for current, cwd, source in (
        (sid, str(workspace), "cli"),
        ("61542ddb-fbc2-440c-8ee5-2143ce56cc1b", str(workspace.parent / "other"), "cli"),
        ("61542ddb-fbc2-440c-8ee5-2143ce56cc1c", str(workspace), '{"subagent":{"thread_spawn":{}}}'),
    ):
        path = folder / ("rollout-" + stamp.strftime("%Y-%m-%dT%H-%M-%S") + "-" + current + ".jsonl")
        path.write_text(json.dumps({"type":"session_meta","payload":{
            "id":current,"cwd":cwd,"source":source,"cli_version":"0.160.0",
            "thread_source":"subagent" if "subagent" in source else "cli"}}) + "\\n")
        db.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?)",
                   (current, str(path), cwd, source, int(time.time()), int(time.time()),
                    "Title", "Title", "Private brainstorming", 0))
    db.commit()
    db.close()
print("ISOLATED NATIVE AGENT", flush=True)
''')
    ident = start_draft(env, tmp_path)
    code, output = terminal(env, ['idea', 'resume', ident], tmp_path, [('Next step', b'j\r')])
    assert code == 0, output
    first, resumed = [json.loads(line) for line in Path(env['IDEA_AGENT_LOG']).read_text().splitlines()]
    assert resumed['args'][:2] == ['resume', sid]
    assert resumed['cwd'] == first['cwd']


def test_live_idea_refuses_second_writer(box, tmp_path):
    _, env, _ = box
    release = tmp_path / 'release'
    log = Path(env['IDEA_AGENT_LOG'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(start_draft, dict(env, IDEA_RELEASE=str(release)), tmp_path)
        try:
            deadline = time.monotonic() + 10
            while not log.exists() and not first.done() and time.monotonic() < deadline:
                time.sleep(.02)
            assert log.exists(), first.result() if first.done() else 'agent never started'
            listing = invoke(env, 'idea', 'list', cwd=tmp_path)
            ident = re.search(r'\bidea-[a-f\d][a-f\d-]+\b', listing.stdout).group()
            code, output = terminal(env, ['idea', 'resume', ident], tmp_path, [])
            assert code != 0, output
            assert any(word in output.lower() for word in ('active', 'busy', 'running', 'in use')), output
            assert len(log.read_text().splitlines()) == 1
        finally:
            release.touch()
        first.result()


def test_state_under_existing_checkout_refuses_launch(box, tmp_path):
    _, env, _ = box
    checkout = tmp_path / 'existing-repository'
    git(env, 'init', '-q', checkout)
    env['XDG_STATE_HOME'] = str(checkout / 'bad-state')
    code, output = terminal(env, ['idea', 'start', 'Unsafe location'], tmp_path, [])
    assert code != 0, output
    assert not Path(env['IDEA_AGENT_LOG']).exists()
    assert 'git' in output.lower(), output


def test_agent_startup_failure_can_retry_without_guessing_a_session(box, tmp_path):
    _, env, _ = box
    code, output = terminal(dict(env, IDEA_FAIL_START='1'), ['idea', 'start', 'Retry idea'],
                            tmp_path, [])
    assert code == 17, output
    ident = re.search(r'\bidea-[a-f\d][a-f\d-]+\b', invoke(env, 'idea', 'list').stdout).group()
    code, output = terminal(env, ['idea', 'resume', ident], tmp_path, [('Next step', b'j\r')])
    assert code == 0, output
    first, retried = [json.loads(line) for line in Path(env['IDEA_AGENT_LOG']).read_text().splitlines()]
    assert retried['cwd'] == first['cwd']
    assert '--session-id' in retried['args'] and '--resume' not in retried['args']


def github_fixture(env, tools, tmp_path):
    remote = tmp_path / 'github.git'
    git(env, 'init', '--bare', '-b', 'main', '-q', remote)
    config = Path(env['HOME']) / '.gitconfig'
    env['GIT_CONFIG_GLOBAL'] = str(config)
    git(env, 'config', '--global', 'user.name', 'Fixture')
    git(env, 'config', '--global', 'user.email', 'fixture@example.invalid')
    env.update(IDEA_REMOTE=str(remote), IDEA_GH_LOG=str(tmp_path / 'gh.jsonl'),
               IDEA_GH_STATE=str(tmp_path / 'gh-state.json'),
               IDEA_REAL_GIT=shutil.which('git', path=env['PATH']))
    executable(tools / 'git', '''import os, pathlib, subprocess, sys
args = sys.argv[1:]
git = os.environ["IDEA_REAL_GIT"]
remote = os.environ["IDEA_REMOTE"]
ssh_url = "git@github.com:sample-user/neighborhood-walks.git"
https_url = "https://github.com/sample-user/neighborhood-walks.git"
if "clone" in args and any(url in args for url in (ssh_url, https_url)):
    url = ssh_url if ssh_url in args else https_url
    result = subprocess.run([git, *(remote if arg == url else arg for arg in args)])
    if result.returncode:
        raise SystemExit(result.returncode)
    if os.environ.get("IDEA_CLONE_LINK"):
        os.symlink(os.environ["IDEA_CLONE_LINK"], pathlib.Path(args[-1]) / "docs",
                   target_is_directory=True)
    os.execv(git, [git, "-C", args[-1], "remote", "set-url", "origin", url])
if ("ls-remote" in args or "push" in args) and "origin" in args:
    if "push" in args and os.environ.get("IDEA_DENY_PUSH") and pathlib.Path(os.environ["IDEA_DENY_PUSH"]).exists():
        raise SystemExit("fixture rejected push")
    args = [remote if arg == "origin" else arg for arg in args]
os.execv(git, [git, *args])
''')
    executable(tools / 'gh', '''import json, os, pathlib, sys
args = sys.argv[1:]
assert args and args[0] == "api", args
state = pathlib.Path(os.environ["IDEA_GH_STATE"])
with open(os.environ["IDEA_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
method = "GET"
for flag in ("-X", "--method"):
    if flag in args:
        method = args[args.index(flag) + 1]
if "user" in args:
    if os.environ.get("IDEA_MUTATE_ON_OWNER"):
        pathlib.Path(os.environ["IDEA_MUTATE_ON_OWNER"]).write_text("Unreviewed change")
    print("sample-user" if "--jq" in args else '{"login":"sample-user"}')
elif "user/orgs" in args:
    print("" if "--jq" in args else "[]")
elif "user/repos" in args and method == "POST":
    assert not state.exists()
    data = json.load(sys.stdin) if "--input" in args else {}
    if not data:
        data = dict(arg.split("=", 1) for arg in args if "=" in arg)
    assert data["name"] == "neighborhood-walks", data
    private = data["private"] in (True, "true")
    repo = {"id": 41, "name": "neighborhood-walks", "full_name": "sample-user/neighborhood-walks",
            "private": private, "visibility": "private" if private else "public",
            "default_branch": "main", "owner": {"login": "sample-user"},
            "ssh_url": "git@github.com:sample-user/neighborhood-walks.git",
            "clone_url": "https://github.com/sample-user/neighborhood-walks.git",
            "html_url": "https://github.com/sample-user/neighborhood-walks"}
    state.write_text(json.dumps(repo))
    print(json.dumps(repo))
elif "repos/sample-user/neighborhood-walks" in args or "repositories/41" in args:
    if not state.exists():
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    repo = json.loads(state.read_text())
    if method == "PATCH":
        repo.update(allow_squash_merge=True, allow_merge_commit=False, allow_rebase_merge=False,
                    delete_branch_on_merge=True, allow_auto_merge=True)
        state.write_text(json.dumps(repo))
    if method == "GET" and os.environ.get("IDEA_GH_MISMATCH"):
        key = os.environ["IDEA_GH_MISMATCH"]
        repo[key] = not repo[key] if key == "private" else repo[key] + 1
    print(json.dumps(repo))
else:
    raise SystemExit("unhandled synthetic GitHub request: " + repr(args))
''')
    return remote


def publish_actions(private=True, *, select=b'\x01\r', confirm=b'y'):
    return [
        ('Files to publish', select), ('Repository name', b'\r'),
        ('GitHub owner', b'\r'), ('Visibility', b'\r' if private else b'j\r'),
        ('Alias', b'\r'), ('Create repository · review', confirm),
    ]


def published_files(env, remote):
    return git(env, '-C', remote, 'ls-tree', '-r', '--name-only', 'main').splitlines()


@pytest.mark.parametrize('private', [True, False], ids=['private', 'public'])
def test_publishes_only_reviewed_files(box, tmp_path, private):
    home, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    workspace = Path(json.loads(Path(env['IDEA_AGENT_LOG']).read_text())['cwd'])
    (workspace / 'publish/linked.txt').symlink_to(workspace / 'private-notes.txt')
    if private:
        code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    else:
        # The new alias must already work in the calling shell after publication.
        code, output = run_terminal(
            ['zsh', '-lic', 't idea publish "$IDEA_ID" && t repo cd neighborhood-walks && pwd'],
            dict(env, IDEA_ID=ident), tmp_path, (44, 20), publish_actions(private=False))
        assert str(home / 'code/neighborhood-walks') in output
    assert code == 0, output
    repo = json.loads(Path(env['IDEA_GH_STATE']).read_text())
    assert repo['private'] is private
    assert published_files(env, remote) == ['README.md', 'docs/routes.txt']
    checkout = home / 'code/neighborhood-walks'
    assert (checkout / 'README.md').read_bytes() == b'Reviewed bytes\r\n'
    assert (checkout / 'docs/routes.txt').read_text() == 'Oak Loop\n'
    assert (workspace / 'private-notes.txt').exists()
    assert 't open neighborhood-walks' in output
    repos = invoke(env, 'repo', 'list', cwd=tmp_path)
    assert 'neighborhood-walks' in repos.stdout, repos.stderr


def test_cancelling_review_keeps_draft_without_creating_remote(box, tmp_path):
    home, env, tools = box
    ident = start_draft(env, tmp_path)
    github_fixture(env, tools, tmp_path)
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path,
                            publish_actions(confirm=b'q'))
    assert code == 130, output
    assert not Path(env['IDEA_GH_STATE']).exists()
    assert not (home / 'code/neighborhood-walks').exists()
    listing = invoke(env, 'idea', 'list', cwd=tmp_path)
    assert ident in listing.stdout


def test_publish_accepts_symlink_above_the_checkout_root(box, tmp_path):
    home, env, tools = box
    home_alias = tmp_path / 'home-alias'
    home_alias.symlink_to(home, target_is_directory=True)
    env['HOME'] = str(home_alias)
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    assert code == 0, output
    assert published_files(env, remote) == ['README.md', 'docs/routes.txt']
    assert (home / 'code/neighborhood-walks/docs/routes.txt').read_text() == 'Oak Loop\n'


def test_publish_rejects_symlink_inside_the_pending_checkout(box, tmp_path):
    home, env, tools = box
    ident = start_draft(env, tmp_path)
    github_fixture(env, tools, tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    env['IDEA_CLONE_LINK'] = str(outside)
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    assert code == 1, output
    assert 'unsafe local clone directory' in output
    assert not (outside / 'routes.txt').exists()
    assert not (home / 'code/neighborhood-walks').exists()


@pytest.mark.parametrize('selection,files', [(b'j \r', ['__all']), (b'\r', [])],
                         ids=['one-file', 'empty'])
def test_publish_single_file_or_empty_commit(box, tmp_path, selection, files):
    _, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    workspace = Path(json.loads(Path(env['IDEA_AGENT_LOG']).read_text())['cwd'])
    (workspace / 'publish/__all').write_text('This is an ordinary file.')
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path,
                            publish_actions(select=selection))
    assert code == 0, output
    assert published_files(env, remote) == files


def test_publication_retry_keeps_reviewed_commit(box, tmp_path):
    home, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    gate = tmp_path / 'deny-push'
    gate.touch()
    env['IDEA_DENY_PUSH'] = str(gate)
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    assert code != 0, output
    assert Path(env['IDEA_GH_STATE']).exists()
    assert not (home / 'code/neighborhood-walks').exists()
    gate.unlink()
    workspace = Path(json.loads(Path(env['IDEA_AGENT_LOG']).read_text())['cwd'])
    (workspace / 'publish/README.md').write_text('Unreviewed revision\n')
    (workspace / 'publish/later.txt').write_text('Unreviewed new file\n')
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path,
                            [('Retry publication', b'\r')])
    assert code == 0, output
    assert published_files(env, remote) == ['README.md', 'docs/routes.txt']
    assert (home / 'code/neighborhood-walks/README.md').read_bytes() == b'Reviewed bytes\r\n'
    requests = [json.loads(line) for line in Path(env['IDEA_GH_LOG']).read_text().splitlines()]
    assert sum('user/repos' in request for request in requests) == 1


def test_publication_retry_recovers_verified_checkout_after_rename(box, tmp_path):
    home, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    checkout = home / 'code/neighborhood-walks'
    harness = tmp_path / 'crash-after-clone.py'
    harness.write_text('''import os, runpy, sys
source, destination, identifier = sys.argv[1:]
original = os.rename
def interrupt_after_rename(src, dest, *args, **kwargs):
    result = original(src, dest, *args, **kwargs)
    if os.path.abspath(dest) == destination:
        os._exit(73)
    return result
os.rename = interrupt_after_rename
sys.argv = [source, "idea", "publish", identifier]
runpy.run_path(source, run_name="__main__")
''')
    code, output = run_terminal([sys.executable, str(harness), str(ROOT / 'bin/t'),
                                 str(checkout), ident], env, tmp_path, (82, 25), publish_actions())
    assert code == 73, output
    inode = checkout.stat().st_ino
    assert published_files(env, remote) == ['README.md', 'docs/routes.txt']
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path,
                            [('Retry publication', b'\r')])
    assert code == 0, output
    assert checkout.stat().st_ino == inode
    assert 'neighborhood-walks' in invoke(env, 'repo', 'list').stdout


@pytest.mark.parametrize('changed', ['id', 'private'])
def test_publication_refuses_mismatched_github_identity(box, tmp_path, changed):
    home, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    env['IDEA_GH_MISMATCH'] = changed
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    assert code != 0, output
    assert Path(env['IDEA_GH_STATE']).exists()
    assert git(env, '-C', remote, 'for-each-ref', 'refs/heads/main') == ''
    assert not (home / 'code/neighborhood-walks').exists()


def test_publication_refuses_unreviewed_second_push_destination(box, tmp_path):
    _, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    for url in ('git@github.com:sample-user/neighborhood-walks.git',
                'git@github.com:someone-else/not-reviewed.git'):
        git(env, 'config', '--global', '--add', 'remote.origin.pushurl', url)
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    assert code != 0, output
    assert git(env, '-C', remote, 'for-each-ref', 'refs/heads/main') == ''


def test_changed_files_require_new_review_before_creation(box, tmp_path):
    _, env, tools = box
    ident = start_draft(env, tmp_path)
    remote = github_fixture(env, tools, tmp_path)
    workspace = Path(json.loads(Path(env['IDEA_AGENT_LOG']).read_text())['cwd'])
    env['IDEA_MUTATE_ON_OWNER'] = str(workspace / 'publish/README.md')
    code, output = terminal(env, ['idea', 'publish', ident], tmp_path, publish_actions())
    assert code != 0, output
    assert not Path(env['IDEA_GH_STATE']).exists()
    assert git(env, '-C', remote, 'for-each-ref', 'refs/heads/main') == ''
