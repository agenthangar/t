"""Repository selectors resolve at shell grammar boundaries in isolated homes."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which('zsh'), reason='zsh is required')


def shell(tmp_path, code):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    bins = tmp_path / 'bin'
    bins.mkdir(exist_ok=True)
    for name in ('t', 'tmux', 'ssh'):
        stub = bins / name
        stub.write_text('#!/bin/sh\necho "UNEXPECTED ' + name + '" >&2\nexit 97\n')
        stub.chmod(0o755)
    env = {**os.environ, 'HOME': str(home), 'ZDOTDIR': str(home),
           'XDG_CONFIG_HOME': str(home / '.config'),
           'XDG_CACHE_HOME': str(home / '.cache'),
           'XDG_STATE_HOME': str(home / '.local/state'),
           'TMUX_TMPDIR': str(tmp_path / 'tmux'), 'T_NO_UPDATE_CHECK': '1',
           'PATH': f"{bins}:{os.environ['PATH']}"}
    for key in tuple(env):
        if key.startswith('COV_CORE_') or key in ('TMUX', 'T_LOCAL_RC'):
            env.pop(key)
    return subprocess.run(['zsh', '-f', '-c',
        f'source {shlex.quote(str(ROOT / "t.plugin.zsh"))}\n' + code],
        env=env, text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize('registrations,selector,expected', [
    ('app "$HOME/code/application"', 'app', 'app'),
    ('app "$HOME/code/application"', 'application', 'app'),
    ('zz "$HOME/code/application///" aa "$HOME/code/application"', 'application', 'aa'),
    ('long "$HOME/code/a/../application" a "$HOME/code/application"', 'application', 'a'),
    ('app "$HOME/missing parent/hyphen-name/"', 'hyphen-name', 'app'),
    ('app "$HOME/code/application" application "$HOME/code/another"', 'application', 'application'),
])
def test_resolver_alias_precedence_and_deterministic_names(tmp_path, registrations, selector, expected):
    result = shell(tmp_path, f'DEV_REPOS=({registrations}); _dev_resolve_repo {selector}')
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected + '\n'
    assert result.stderr == ''


def test_resolver_groups_symlink_aliases_without_requiring_checkout(tmp_path):
    home = tmp_path / 'home'
    (home / 'actual/application').mkdir(parents=True)
    (home / 'linked').symlink_to(home / 'actual', target_is_directory=True)
    result = shell(tmp_path, '''DEV_REPOS=(zz "$HOME/actual/application" a "$HOME/linked/application")
        _dev_resolve_repo application
        _dev_resolve_repo absent; print -r -- "unknown=$?"
        print -rl -- ${(ok)DEV_REPOS}
    ''')
    assert result.stdout.splitlines() == ['a', 'unknown=1', 'a', 'zz']
    assert result.stderr == ''


@pytest.mark.parametrize('command', [
    '_dev_resolve_repo application', 't cd application 2', 't session cd application 2',
    't repo cd application', 't open application 2 --cli', '_t_dev application 2 --local',
    '_t_resume application 2', '_t_pop application 2', '_dev_kill application 2 1',
    '_dev_remote_kill application 2 1', '_dev_remote_resolve application 2',
    '_t_remote_open_local application 2', '_t_beam application 2 --host example --detach',
])
def test_ambiguous_names_stop_before_navigation_or_session_operations(tmp_path, command):
    result = shell(tmp_path, f'''
        DEV_REPOS=(a "$HOME/first/application" b "$HOME/second/application")
        _t_infer_repo() {{ print -u2 UNEXPECTED-infer; return 97; }}
        before=$PWD
        {command}; rc=$?
        print -r -- "rc=$rc unchanged=$([[ $PWD == $before ]] && print yes)"
    ''')
    assert result.returncode == 0
    assert result.stdout == 'rc=2 unchanged=yes\n'
    assert "ambiguous repository 'application'" in result.stderr
    assert 'a, b' in result.stderr
    assert 'UNEXPECTED' not in result.stderr


@pytest.mark.parametrize('command', ['t cd', 't session cd'])
@pytest.mark.parametrize('selector', ['app', 'application'])
@pytest.mark.parametrize('leaf', ['2', 'application-2'])
def test_cd_enters_same_existing_slot_without_tmux(tmp_path, command, selector, leaf):
    home = tmp_path / 'home'
    slot = home / 'worktrees/application' / leaf
    (slot / '.git').mkdir(parents=True)
    result = shell(tmp_path, f'''
        DEV_REPOS=(app "$HOME/code/application")
        DEV_WORKTREE_ROOT="$HOME/worktrees"
        {command} {selector} 2 && print -r -- "$PWD"
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(slot) + '\n'
    assert result.stderr == ''


@pytest.mark.parametrize('selector', ['app', 'application'])
def test_repo_cd_enters_checkout(tmp_path, selector):
    checkout = tmp_path / 'home/code/application'
    checkout.mkdir(parents=True)
    result = shell(tmp_path, f'DEV_REPOS=(app "$HOME/code/application"); t repo cd {selector}; print -r -- "$PWD"')
    assert result.stdout == str(checkout) + '\n'
    assert result.stderr == ''


def test_open_mode_and_exact_keyword_alias_settings(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(app "$HOME/code/application" ls "$HOME/code/ls")
        DEV_OPEN_MODE[app]=app
        _t_open_app() { print -r -- "APP $*"; }
        git() { return 1; }
        _dev_branch_for() { print -u2 -r -- "BRANCH $1"; print main; }
        t open application 2
        t session open ls 2 --cli
    ''')
    assert result.returncode == 1
    assert result.stdout.splitlines() == ['APP application 2', f'Repo dir not found: {tmp_path}/home/code/ls']
    assert result.stderr == 'BRANCH ls\n'


def test_direct_workers_resolve_before_existing_target_lookup(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(app "$HOME/code/application")
        _dev_kill_pick() { print -r -- "KILL $1"; }
        _dev_kill application
        _t_open() { print -r -- "OPEN $*"; }
        _t_remote_open_local application 2
        _dev_worktree_enabled() { print -u2 -r -- "RESUME $1"; return 1; }
        _t_resume application 2
        tmux() { print -r -- "$*" >> "$HOME/tmux-lookup"; return 1; }
        _dev_remote_delegate() { return 1; }
        _t_pop application 2
        cat "$HOME/tmux-lookup"
    ''')
    assert result.stdout.splitlines()[:2] == ['KILL app', 'OPEN app 2 --cli --local']
    assert 'RESUME app' in result.stderr
    assert '=dev-app-2:' in result.stdout
    assert 'UNEXPECTED' not in result.stderr


@pytest.mark.parametrize('action', ['open'])
def test_remote_target_keeps_selector_even_when_local_alias_differs(tmp_path, action):
    result = shell(tmp_path, f'''
        DEV_REPOS=(application "$HOME/code/other" app "$HOME/code/application")
        REMOTE_HOSTS=(far user@example)
        ssh() {{ print -r -- "SSH $*"; return 17; }}
        t remote {action} application 2 --host far
    ''')
    assert result.returncode == 17
    words = shlex.split(result.stdout.removeprefix('SSH user@example '))
    worker = shlex.split(words[2])
    assert worker[worker.index('_t_remote_open_local' if action == 'open' else '_t_resume') + 1] == 'application'
    assert result.stderr == ''


def test_open_host_path_does_not_resolve_local_ambiguous_name(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(a "$HOME/a/application" b "$HOME/b/application")
        _dev_remote_open() { print -r -- "REMOTE $*"; return 17; }
        t open application 2 --host far --cli
    ''')
    assert result.returncode == 17
    assert result.stdout == 'REMOTE far application 2 --cli\n'
    assert result.stderr == ''


def test_completions_offer_names_only_in_existing_repository_positions(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(app "$HOME/code/application" a "$HOME/a/duplicate" b "$HOME/b/duplicate")
        _values() { print -rl -- "$@"; }
        _message() { print -r -- "MESSAGE $*"; }
        _files() { print FILES; }
        words=(t session cd ''); CURRENT=4; _t
        print BOUNDARY
        words=(t repo clone example ''); CURRENT=5; _t
    ''')
    assert result.returncode == 0, result.stderr
    existing, new = result.stdout.split('BOUNDARY\n')
    assert 'app\n' in existing and 'application\n' in existing
    assert 'duplicate\n' not in existing
    assert new == 'MESSAGE local alias\n'


def test_source_worker_filters_by_resolved_checkout_and_sibling_owner(tmp_path):
    result = shell(tmp_path, r'''
        DEV_REPOS=(a "$HOME/code/application")
        DEV_WORKTREE_ROOT="$HOME/worktrees"
        _dev_session_rows_impl() {
          printf 'same\t%s\tremote-alias-2\tdetached\tactive\ttask\tcodex\n' "$HOME/worktrees/application/application-2"
          printf 'other\t%s\tother-2\tdetached\tactive\ttask\tcodex\n' "$HOME/code/other"
        }
        _dev_session_rows --repo application 2
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('same\t')
    assert 'remote-alias-2' in result.stdout and 'other\t' not in result.stdout
    assert result.stderr == ''


def test_pull_preserves_source_selector_and_propagates_source_ambiguity(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(application "$HOME/code/unrelated")
        rsync() { :; }
        ssh() { print -u2 -r -- "SSH $*"; return 2; }
        _dev_pull far user@example application 2 ''
    ''')
    assert result.returncode == 2
    words = shlex.split(result.stderr.removeprefix('SSH user@example '))
    assert shlex.split(words[2].split('; _dev_session_rows ', 1)[1]) == ['--repo', 'application', '2']
    assert 'functions[_dev_session_rows_scoped]' in words[2]
    assert result.stdout == ''


def test_direct_move_source_resolves_name_before_targeting_session(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(a "$HOME/code/application")
        rsync() { :; }
        tmux() { print -u2 -r -- "$*"; return 1; }
        _t_beam application 2 --host far --detach
    ''')
    assert result.returncode == 1
    assert 'no such session: dev-a-2' in result.stderr
    assert 'unknown repository' not in result.stderr


@pytest.mark.parametrize('registered', [
    'a "$HOME/a/application" b "$HOME/b/application"',
    'application "$HOME/code/unrelated"',
    '',
])
def test_remote_move_browsing_leaves_selector_to_explicit_source(tmp_path, registered):
    result = shell(tmp_path, f'''
        DEV_REPOS=({registered})
        _dev_remote_scan_with_timeout() {{ print -u2 -r -- "SOURCE $*"; return 2; }}
        t remote move application --from far --host destination --detach
    ''')
    assert result.returncode == 2
    args = shlex.split(result.stderr.removeprefix('SOURCE ').split('zsh -lic ', 1)[1].rsplit(' ', 2)[0])
    assert shlex.split(args[0].split('; _dev_session_rows ', 1)[1]) == ['--repo', 'application', '']
    assert 'functions[_dev_session_rows_scoped]' in args[0]
    assert 'ambiguous repository' not in result.stderr
    assert result.stdout == ''


def test_resolver_realpath_handles_symlink_parent_before_dot_segments(tmp_path):
    home = tmp_path / 'home'
    (home / 'actual/nested').mkdir(parents=True)
    (home / 'actual/application').mkdir()
    (home / 'linked').symlink_to(home / 'actual/nested', target_is_directory=True)
    result = shell(tmp_path, '''
        DEV_REPOS=(zz "$HOME/linked/../application" a "$HOME/actual/application")
        _dev_resolve_repo application
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'a\n'
    assert result.stderr == ''


@pytest.mark.parametrize('command', ['t cd', 't session cd'])
def test_numeric_directory_name_does_not_override_slot_only_grammar(tmp_path, command):
    slot = tmp_path / 'home/worktrees/application/application-2'
    (slot / '.git').mkdir(parents=True)
    result = shell(tmp_path, f'''
        DEV_REPOS=(a "$HOME/code/2" current "$HOME/code/application")
        DEV_WORKTREE_ROOT="$HOME/worktrees"
        _t_infer_repo() {{ print current; }}
        {command} 2 && print -r -- "$PWD"
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(slot) + '\n'
    assert result.stderr == ''


def test_numeric_open_and_resume_keep_cwd_slot_shorthand(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(a "$HOME/code/2" current "$HOME/code/application")
        _t_infer_repo() { print current; }
        git() { return 1; }
        _dev_branch_for() { print -u2 -r -- "BRANCH $1"; print main; }
        t open 2 --cli
        _dev_worktree_enabled() { print -u2 -r -- "RESUME $1"; return 1; }
        _t_resume 2
    ''')
    assert 'BRANCH current' in result.stderr
    assert 'RESUME current' in result.stderr
    assert 'Repo dir not found:' in result.stdout
    assert '/home/code/application' in result.stdout
    assert 'UNEXPECTED' not in result.stderr


def test_scoped_remote_worker_rejects_older_source_before_unfiltered_rows(tmp_path):
    result = shell(tmp_path, '''
        worker=$(_dev_repo_rows_command application 2)
        unfunction _dev_session_rows_scoped
        _dev_session_rows() { print -u2 UNEXPECTED-unfiltered-owner; }
        eval "$worker"
    ''')
    assert result.returncode == 1
    assert 'update t on the source host' in result.stderr
    assert 'UNEXPECTED' not in result.stderr
    assert result.stdout == ''


@pytest.mark.parametrize('selector,collision,expected', [
    ('app', False, 'app'), ('application', False, 'app'),
    ('application', True, 'application'),
])
def test_remote_resume_names_share_verified_local_identity(tmp_path, selector, collision, expected):
    extra = 'DEV_REPOS[application]="$HOME/code/other"' if collision else ''
    result = shell(tmp_path, f'''
        DEV_REPOS=(app "$HOME/code/application")
        {extra}
        REMOTE_HOSTS=(far user@example)
        python3() {{ [[ $2 == __repo-identity && $3 == {expected} ]] || return 98; print org/project; }}
        ssh() {{ print -r -- "SSH $*"; return 17; }}
        t remote resume {selector} 2 --host far
    ''')
    assert result.returncode == 17, result.stderr
    worker = shlex.split(result.stdout.removeprefix('SSH user@example '))[2]
    assert 'command t __resume-alias org/project' in worker
    assert '_T_RESUME_REPO_IDENTITY=org/project' in worker
    assert result.stderr == ''


def test_remote_resume_target_only_name_stays_literal(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(app "$HOME/code/application")
        REMOTE_HOSTS=(far user@example)
        python3() { print BAD-identity >&2; return 98; }
        ssh() { print -r -- "SSH $*"; return 17; }
        t remote resume remote-only 2 --host far
    ''')
    assert result.returncode == 17
    worker = shlex.split(shlex.split(result.stdout.removeprefix('SSH user@example '))[2])
    assert worker[worker.index('_t_resume') + 1] == 'remote-only'
    assert result.stderr == ''


def test_remote_resume_ambiguous_name_stops_before_identity_or_ssh(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(a "$HOME/a/application" b "$HOME/b/application")
        REMOTE_HOSTS=(far user@example)
        python3() { print BAD-identity >&2; return 98; }
        ssh() { print BAD-ssh >&2; return 98; }
        t remote resume application 2 --host far
    ''')
    assert result.returncode == 2
    assert 'ambiguous repository' in result.stderr and 'BAD-' not in result.stderr


@pytest.mark.parametrize('selector', ['app', 'application'])
def test_origin_names_reach_resume_and_remote_identity(tmp_path, selector, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home/.config"))
    checkout = tmp_path / 'home/code/owner-application'
    checkout.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), 'remote', 'add', 'origin', 'git@github.com:team/application.git'], check=True)
    result = shell(tmp_path, f'''
        DEV_REPOS=(app "$HOME/code/owner-application")
        _dev_worktree_enabled() {{ print -u2 -r -- "RESUME $1"; return 1; }}
        t resume {selector} 2
        REMOTE_HOSTS=(far user@example)
        python3() {{ [[ $2 == __repo-identity && $3 == app ]] || return 98; print org/project; }}
        ssh() {{ print -r -- "SSH $*"; return 17; }}
        t remote resume {selector} 2 --host far
    ''')
    assert result.returncode == 17, result.stderr
    assert 'RESUME app' in result.stderr
    worker = shlex.split(result.stdout.removeprefix('SSH user@example '))[2]
    assert 'command t __resume-alias org/project' in worker
    assert 'UNEXPECTED' not in result.stderr


def test_origin_completion_is_linear_fresh_and_suppresses_combined_collisions(tmp_path):
    result = shell(tmp_path, '''
        DEV_REPOS=(long "$HOME/one" a "$HOME/one" b "$HOME/two/application")
        _dev_repo_origin_name() { print -r -- "$1" >> "$HOME/reads"; print application; }
        _dev_repo_selectors
        print READS=$(wc -l < "$HOME/reads" | tr -d ' ')
        _dev_resolve_repo application; print "rc=$?"
        _dev_repo_origin_name() { print changed; }
        _dev_repo_selectors
        _dev_repo_origin_name() { print -u2 UNEXPECTED; }
        _dev_resolve_repo a
    ''')
    assert result.returncode == 0
    first, second = result.stdout.split('READS=2\n')
    assert first.splitlines() == ['a', 'b', 'long', 'one']
    assert second.startswith('rc=2\n') and second.endswith('a\n')
    assert 'application\n' in second  # basename remains available after origin edit
    assert 'changed\n' not in second  # both distinct checkouts still collide
    assert 'ambiguous repository' in result.stderr and 'UNEXPECTED' not in result.stderr


@pytest.mark.parametrize('command', ['t resume application 2', 't repo cd application', '_t_remote_open_local application 2'])
def test_real_origin_collision_stops_shell_actions(tmp_path, command, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    for leaf in ['owner-application', 'application']:
        checkout = home / leaf
        checkout.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
        subprocess.run(['git', '-C', str(checkout), 'remote', 'add', 'origin',
                        'https://user:private-token@example.com/org/application.git'], check=True)
    result = shell(tmp_path, f'''
        DEV_REPOS=(a "$HOME/owner-application" b "$HOME/application")
        {command}
    ''')
    assert result.returncode == 2
    assert "ambiguous repository 'application'" in result.stderr
    assert 'a, b' in result.stderr and 'private-token' not in result.stderr
    assert 'UNEXPECTED' not in result.stderr and not result.stdout
