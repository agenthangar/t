"""Folder labels must not change slot IDs, saved paths, or writer ownership."""
from pathlib import Path
import re
import shlex

import pytest

from test_plugin import shell
from test_t import _config_with

ROOT = Path(__file__).resolve().parents[1]


def configured_shell(tmp_path, code):
    return shell(tmp_path, 'tmux() { return 1; }; _codex_threads() { :; }; ' + code,
                 local_text='DEV_REPOS[api]="$HOME/code with spaces/api"\n'
                            'DEV_REPOS[alias]=$DEV_REPOS[api]\n'
                            'DEV_WORKTREE_ROOT="$HOME/work trees"\n')


@pytest.mark.parametrize('leaf,slot', [('12', '12'), ('api-12', '12'),
                                      ('api-012', '012'), ('other-12', None),
                                      ('api-12.debris', None), ('api-api-12', None)])
def test_python_and_shell_resolve_same_numeric_identity(tmp_path, monkeypatch, t_mod, leaf, slot):
    root = tmp_path / 'home/work trees'
    cfg = _config_with(t_mod, tmp_path, monkeypatch, {'api': shlex.quote(str(tmp_path / 'home/code with spaces/api'))}, shlex.quote(str(root)))
    cwd = str(root / 'api' / leaf / 'subdir')
    result = configured_shell(tmp_path, '_dev_repo_of_dir ' + shlex.quote(cwd))
    if slot:
        assert result.stdout.strip() == 'api\t' + slot
        assert cfg.repo_of_dir(cwd) == ('api', slot)
    else:
        assert result.returncode != 0
        assert cfg.repo_of_dir(cwd) == (None, None)


@pytest.mark.parametrize('layout', ['new', 'legacy', 'labeled', 'collision', 'dangling'])
def test_path_resolution_preserves_old_paths_and_refuses_ambiguity(tmp_path, t_mod, monkeypatch, layout):
    root = tmp_path / 'home/work trees'
    parent = root / 'api'
    parent.mkdir(parents=True)
    old, new = parent / '4', parent / 'api-4'
    if layout in ('legacy', 'collision'):
        old.mkdir()
        (old / 'keep').write_text('existing contents')
    if layout in ('labeled', 'collision'):
        new.mkdir()
    if layout == 'dangling':
        old.symlink_to(parent / 'missing')
    cfg = _config_with(t_mod, tmp_path, monkeypatch, {'api': '/code/api'}, shlex.quote(str(root)))
    result = configured_shell(tmp_path, '_dev_worktree_path api 4')
    if layout == 'collision':
        assert result.returncode != 0 and 'both worktree layouts' in result.stderr
        with pytest.raises(ValueError, match='both worktree layouts'):
            cfg.worktree_path('api', '4')
    else:
        expected = old if layout in ('legacy', 'dangling') else new
        assert result.stdout.strip() == str(expected)
        assert cfg.worktree_path('api', '4') == str(expected)
    if old.is_dir():
        assert (old / 'keep').read_text() == 'existing contents'
    with pytest.raises(ValueError, match='numeric'):
        cfg.worktree_path('api', '../4')


def test_discovery_keeps_saved_numeric_and_named_slots_without_folders(tmp_path):
    home = tmp_path / 'home'
    root = home / 'work trees/api'
    (root / 'api-7').mkdir(parents=True)
    (root / '8').mkdir()
    (root / 'api-9.debris').mkdir()
    for leaf in ('12', 'api-13'):
        encoded = re.sub('[^A-Za-z0-9]', '-', str(root / leaf))
        (home / '.claude/projects' / encoded).mkdir(parents=True)
    result = configured_shell(tmp_path, '_dev_repo_slots alias')
    assert result.stdout.splitlines() == ['7', '8', '12', '13']


@pytest.mark.parametrize('leaf', ['4', 'api-4'])
def test_transfer_preflight_accepts_exact_layout_and_rejects_other_roots(tmp_path, leaf):
    cwd = tmp_path / 'home/work trees/api' / leaf
    code = '_dev_worktree_accept_cwd ' + shlex.quote(str(cwd))
    result = configured_shell(tmp_path, code)
    assert result.returncode == 0, result.stderr
    other = configured_shell(tmp_path, '_dev_worktree_accept_cwd /Users/other/worktrees/api/' + leaf)
    assert other.returncode != 0 and 'align registered' in other.stderr
    nested = configured_shell(tmp_path, code + '/subdir')
    assert nested.returncode != 0 and 'worktree root' in nested.stderr
    sibling = cwd.parent / ('api-4' if leaf == '4' else '4')
    sibling.mkdir(parents=True)
    collision = configured_shell(tmp_path, code)
    assert collision.returncode != 0 and 'another folder layout' in collision.stderr
    assert sibling.is_dir() and not cwd.exists()


@pytest.mark.parametrize('peer', ['current', 'old', 'different-root'])
def test_remote_negotiation_uses_login_shell_and_actionable_old_peer_failure(tmp_path, peer):
    cwd = tmp_path / 'home/work trees/api/api-4'
    # Execute the exact SSH payload in a disposable shell, with current or old
    # receiver capabilities. No credentials, network, or live host is involved.
    setup = '' if peer == 'current' else ('unfunction _dev_worktree_accept_cwd; ' if peer == 'old'
                                         else 'DEV_WORKTREE_ROOT=/other/root; ')
    result = configured_shell(tmp_path, f'''
ssh() {{
  [[ $1 == test-host && $2 == 'zsh -lic '* ]] || return 98
  local payload=${{2#'zsh -lic '}}
  payload=${{(Q)payload}}
  ( {setup}eval "$payload" )
}}
_dev_worktree_remote_accept test-host {shlex.quote(str(cwd))}
''')
    if peer == 'current':
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert 'update t on both hosts' in result.stderr
        assert 'source was not stopped' in result.stderr


@pytest.mark.parametrize('leaf', ['4', 'api-4'])
def test_desktop_discovery_and_navigation_preserve_full_path(tmp_path, leaf):
    cwd = tmp_path / 'home/work trees/api' / leaf
    (cwd / '.git').mkdir(parents=True)
    result = configured_shell(tmp_path, '''
_dev_app_slot_reserved() { [[ -e $1/.git ]]; }
_dev_ps_snapshot() { :; }
_dev_fg_rows() { :; }
_dev_session_rows
_t_cd api 4 || return
print -r -- "cwd=$PWD"
''')
    assert result.returncode == 0, result.stderr
    assert f'\t{cwd}\tapi-4\tapp\t' in result.stdout
    assert f'cwd={cwd}' in result.stdout


@pytest.mark.parametrize('leaf', ['4', 'api-4'])
def test_transcript_projection_keeps_exact_recorded_workspace(tmp_path, monkeypatch, t_mod, leaf):
    root = tmp_path / 'wt'
    cfg = _config_with(t_mod, tmp_path, monkeypatch, {'api': '/code/api'}, shlex.quote(str(root)))
    cwd = str(root / 'api' / leaf)
    rec = {'sid': '1234abcd', 'cwd': cwd, 'n_prompts': 1, 'last_ts': 1}
    row = t_mod._session_row(cfg, rec, {}, 2)
    assert row['slot'] == 'api-4' and row['worktree'] == cwd
    assert row['sid'] == rec['sid']
    assert t_mod._session_ref_resolve('api-4', cfg, [rec], {}) is rec


@pytest.mark.parametrize('leaf', ['4', 'api-4'])
def test_public_move_refuses_old_peer_before_stopping_or_copying(tmp_path, leaf):
    cwd = tmp_path / 'home/work trees/api' / leaf
    result = configured_shell(tmp_path, f'''
rsync() {{ print -u2 UNEXPECTED_COPY; return 99; }}
ssh() {{ return 1; }}
_dev_self_sid() {{ :; }}
_codex_thread_lookup() {{ printf 'exact-id\\trollout\\t%s\\n' {shlex.quote(str(cwd))}; }}
_dev_app_slot_reserved() {{ return 1; }}
_dev_codex_rollout_integrity() {{ return 0; }}
_tbeam_kill_owner() {{ print -u2 UNEXPECTED_STOP; return 99; }}
_dev_worktree_beam_push() {{ print -u2 UNEXPECTED_PUSH; return 99; }}
_t_beam -s exact-id test-host
''')
    assert result.returncode != 0
    assert 'source was not stopped' in result.stderr
    assert 'UNEXPECTED' not in result.stderr


def test_navigation_ignores_unmanaged_worktree_names(tmp_path):
    (tmp_path / 'home/work trees/api/unmanaged/.git').mkdir(parents=True)
    canonical = tmp_path / 'home/code with spaces/api'
    canonical.mkdir(parents=True)
    result = configured_shell(tmp_path, '_t_cd api && print -r -- "$PWD"')
    assert result.returncode == 0 and result.stdout.strip() == str(canonical)
    result = configured_shell(tmp_path, '_t_cd')
    assert result.returncode != 0 and 'no managed worktrees' in result.stderr


def test_desktop_claim_cannot_switch_layout_after_lock_selection(tmp_path):
    old = tmp_path / 'home/work trees/api/4'
    (old / '.git').mkdir(parents=True)
    named = old.parent / 'api-4'
    result = configured_shell(tmp_path, f'''
_dev_ps_snapshot() {{ :; }}
_dev_cwd_pids() {{ :; }}
_dev_app_slot_reserve() {{ print -u2 UNEXPECTED_RESERVATION; return 99; }}
_t_open_app_claim api 4 {shlex.quote(str(named))}
''')
    assert result.returncode != 0
    assert 'another worktree path' in result.stderr
    assert 'UNEXPECTED_RESERVATION' not in result.stderr
    assert old.is_dir() and not named.exists()
