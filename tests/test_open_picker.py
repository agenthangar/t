"""Public open picker parity with the detailed session list, using sandbox stubs."""
import pytest

from test_agent_seam import zsh  # noqa: F401 — shared isolated shell fixture


ROWS = r'''
  _dev_session_rows() {
    [[ $1 == --details ]] || { print -u2 MISSING_DETAILS; return 1; }
    printf 'sid1\t%s\tapi-2\tattached\tactive\tFirst task · #12 merged, still in progress\tcodex\tgpt-test-astra\n' "$HOME/code/api"
    printf -- '-\t%s\tapi-4\tapp\tnone\tNo conversation recorded\tcodex\t-\n' "$HOME/code/api"
    printf -- '-\t%s\tweb-6\tdetached\tidle\t(idle — no conversation)\tclaude\t-\n' "$HOME/code/web"
    printf 'sid3\t%s\tweb:abcdef12\tattached\tactive\tForeground task\tclaude\tclaude-test-model\n' "$HOME/code/web"
  }
'''


def picker_stub(tmp_path, selection="api-2"):
    stub = tmp_path / "stubbin/fzf"
    stub.write_text(r"""#!/usr/bin/env python3
import os, sys, subprocess
from pathlib import Path
if '--help' in sys.argv:
    print('--id-nth')
    sys.exit()
rows = sys.stdin.read().splitlines()
open(os.environ['PICKER_LOG'], 'w').write('\n'.join(rows))
Path(os.environ['PICKER_LOG'] + '.all').write_text(Path(os.environ['T_PICKER_ALL']).read_text())
for index, action in enumerate(filter(None, os.environ.get('PICK_ACTIONS', '').split(','))):
    subprocess.run(['sh', os.environ['T_PICKER_TOGGLE'], action], check=True)
    Path(os.environ['PICKER_LOG'] + '.header').write_text(Path(os.environ['T_PICKER_CURRENT_HEADER']).read_text())
    rows = Path(os.environ['T_PICKER_CURRENT']).read_text().splitlines()
    Path(os.environ['PICKER_LOG'] + f'.step{index}').write_text('\n'.join(rows))
selected = os.environ.get('PICK_LABELS', '').split(',')
for row in rows:
    if row.split('\t')[1] in selected:
        print(row)
if selected == ['']:
    sys.exit(130)
""")
    stub.chmod(0o755)
    return dict(PICKER_LOG=str(tmp_path / "picker.log"), PICK_LABELS=selection)


def test_bare_open_inside_worktree_shows_every_detail_and_repo(zsh, tmp_path):
    env = picker_stub(tmp_path)
    result = zsh(ROWS + r'''
      mkdir -p "$HOME/code/.worktrees/api/9"
      cd "$HOME/code/.worktrees/api/9"
      _t_dev() { print -r -- "CLI $*"; }
      _t_open --attached
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout
    rows = (tmp_path / "picker.log").read_text().splitlines()
    assert [row.split('\t')[1] for row in rows] == ['api-2', 'api-4', 'web-6', 'web:abcdef12']
    display = [row.split('\t')[-1] for row in rows]
    assert all('STATUS' not in row for row in display)
    assert '● ✓ ↻' in display[0] and '› cli' in display[0]
    assert '⬡ codex' in display[0] and 'gpt-test-astra' in display[0]
    assert display[0].endswith('First task · #12 merged, still in progress')
    assert '▣ desktop' in display[1] and display[1].endswith('No conversation recorded')
    assert '(idle — no conversation)' in display[2]
    assert 'claude-test-model' in display[3] and 'Foreground task' in display[3]
    assert 'CLI api 2 --local' in result.stdout


def test_open_repo_filter_keeps_desktop_and_cli(zsh, tmp_path):
    env = picker_stub(tmp_path, 'api-4')
    result = zsh(ROWS + r'''
      _t_open_app() { print -r -- "APP $*"; }
      _t_dev() { print UNEXPECTED_CLI; }
      _t_open api --attached
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout
    rows = (tmp_path / 'picker.log').read_text().splitlines()
    assert [row.split('\t')[1] for row in rows] == ['api-2', 'api-4']
    assert 'APP api 4 --app' in result.stdout
    assert 'UNEXPECTED' not in result.stdout


def test_open_mixed_batch_routes_each_interface_before_first_attach(zsh, tmp_path):
    env = picker_stub(tmp_path, 'api-2,api-4,web-6,web:abcdef12')
    result = zsh(ROWS + r'''
      DEV_OPEN_MODE_DEFAULT=app
      _t_open_app() { print -r -- "APP $*"; }
      _t_dev() { print -r -- "CLI $*"; }
      _dev_open_tab() { print -r -- "TAB $*"; }
      _t_open --attached
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout
    assert 'APP api 4 --app' in result.stdout
    assert 'TAB t open web 6 --cli --local' in result.stdout
    assert 'TAB t open web:abcdef12 --cli --local' in result.stdout
    assert 'CLI api 2 --local' in result.stdout
    assert result.stdout.index('APP') < result.stdout.index('CLI')
    assert result.stdout.index('TAB') < result.stdout.index('CLI')


@pytest.mark.parametrize('args', ['api 2', '2', 'api --new', '--new', 'api --fg'])
def test_explicit_open_target_bypasses_picker(zsh, tmp_path, args):
    result = zsh(r'''
      _t_infer_repo() { print api; }
      _t_dev() { print -r -- "DIRECT $*"; }
      _t_open_picker() { print UNEXPECTED_PICKER; }
      _t_open ''' + args, _tty=True)
    assert result.returncode == 0, result.stdout
    assert 'DIRECT' in result.stdout and 'UNEXPECTED' not in result.stdout


def test_open_cancel_never_falls_through_to_fresh(zsh, tmp_path):
    env = picker_stub(tmp_path, '')
    result = zsh(ROWS + r'''
      _t_dev() { print UNEXPECTED_FRESH; }
      _t_open; print -r -- "rc=$?"
    ''', _tty=True, **env)
    assert 'rc=1' in result.stdout and 'UNEXPECTED' not in result.stdout


def test_picker_formatter_preserves_full_metadata_and_legacy_rows(t_mod, monkeypatch, capsys):
    import io
    title = 'Full title with spaces, PR #123 merged, still in progress and a very long continuation'
    raw = (f'local\tsid\t/code/api\tapi-2\tattached\tactive\t{title}\tcodex\tvery-long-recorded-model-name\n'
           'mini\t-\t/code/web\tweb-4\tapp\tnone\tNo conversation recorded\tcodex\t-\n'
           'mini\tsid2\t/code/api\tapi:abcdef12\tdetached\tactive\tLegacy row\n')
    monkeypatch.setattr(t_mod.sys, 'stdin', io.StringIO(raw))
    assert t_mod.main(['__session-picker-rows', '--remote']) == 0
    rows = capsys.readouterr().out.splitlines()
    assert len(rows) == 3
    assert rows[0].split('\t')[:3] == ['local', 'api-2', 'attached']
    assert title in rows[0] and 'very-long-recorded-model-name' in rows[0]
    assert len(rows[0].split('\t')) == 5
    assert 'very-long-recorded-model-name' in rows[0].split('\t')[3]
    assert 'very-long-recorded-model-name' not in rows[0].split('\t')[-1]
    assert rows[0].split('\t')[-1].endswith(title)
    assert '↻' in rows[0] and '● ✓' in rows[0]
    assert '▣ desktop' in rows[1] and 'mini' in rows[1]
    assert '✱ claude' in rows[2] and 'Legacy row' in rows[2]
    assert t_mod._session_picker_rows([]) == []
    fallback = t_mod._session_picker_rows([dict(host='local', slot='api', state='detached')])
    assert fallback[0].split('\t')[3].startswith('api  -  ✱ claude')


@pytest.mark.parametrize('args,actions,selected,attached', [('', 'all', 'api-2', 'shown'),
                                                            ('--attached', 'default', 'web-6', 'hidden')])
def test_open_hotkeys_show_all_and_restore_defaults(zsh, tmp_path, args, actions, selected, attached):
    env = picker_stub(tmp_path, selected)
    result = zsh(ROWS + r'''
      _t_dev() { print -r -- "CLI $*"; }
      _t_open ''' + args, _tty=True, PICK_ACTIONS=actions, **env)
    assert result.returncode == 0, result.stdout
    assert 'CLI ' + selected.replace('-', ' ') + ' --local' in result.stdout
    header = (tmp_path / 'picker.log.header').read_text()
    assert 'Ctrl-T attached: ' + attached in header


def test_open_attached_toggle_can_hide_after_show_all(zsh, tmp_path):
    env = picker_stub(tmp_path, 'web-6')
    result = zsh(ROWS + r'''
      _t_dev() { print -r -- "CLI $*"; }
      _t_open
    ''', _tty=True, PICK_ACTIONS='all,first', **env)
    assert result.returncode == 0, result.stdout
    assert 'CLI web 6 --local' in result.stdout
    assert 'api-2' in (tmp_path / 'picker.log.step0').read_text()
    assert 'api-2' not in (tmp_path / 'picker.log.step1').read_text()
    assert 'Ctrl-T attached: hidden' in (tmp_path / 'picker.log.header').read_text()


def test_open_remote_picker_retains_model_and_selected_host(zsh, tmp_path):
    env = picker_stub(tmp_path, 'alias-2,web-4')
    result = zsh(r'''
      REMOTE_HOSTS[mini]=mini
      _dev_rows_all() {
        [[ $1 == --details ]] || return 1
        printf 'mini\tsid1\t%s\talias-2\tattached\tactive\tRemote first\tcodex\tgpt-test-astra\n' "$HOME/code/api"
        printf 'studio\tsid2\t%s\tweb-4\tdetached\tactive\tRemote second\tclaude\tclaude-test-model\n' "$HOME/code/web"
        printf 'local\tsid3\t%s\tapi-3\tdetached\tactive\tLocal hidden\tcodex\tgpt-test-astra\n' "$HOME/code/api"
      }
      _dev_remote_open() { print -r -- "REMOTE $*"; }
      _dev_open_tab() { print -r -- "TAB $*"; }
      _t_open -r --attached
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout
    rows = (tmp_path / 'picker.log').read_text()
    assert 'mini' in rows and 'studio' in rows and 'claude-test-model' in rows
    assert 'Local hidden' not in rows
    assert 'TAB t open web 4 --cli --host studio' in result.stdout
    assert 'REMOTE mini alias 2 --cli' in result.stdout


def test_open_hides_attached_and_loaded_desktop_by_default(zsh, tmp_path):
    env = picker_stub(tmp_path, 'web-6')
    rows = ROWS.replace('\n  }', r'''
      printf 'sid4\t%s\tweb-7\tapp\tactive\tLoaded desktop\tcodex\tgpt-test-astra\n' "$HOME/code/web"
    }''')
    result = zsh(rows + r'''
      _t_dev() { print -r -- "CLI $*"; }
      _t_open
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout
    rows = (tmp_path / 'picker.log').read_text().splitlines()
    assert [row.split('\t')[1] for row in rows] == ['api-4', 'web-6']
    assert 'CLI web 6 --local' in result.stdout


def test_open_all_attached_does_not_create_fresh_session(zsh, tmp_path):
    env = picker_stub(tmp_path)
    result = zsh(r'''
      _dev_session_rows() {
        printf 'sid\t%s\tapi-2\tattached\tactive\tAttached\tcodex\tgpt-test-astra\n' "$HOME/code/api"
        printf 'sid2\t%s\tapi-4\tapp\tactive\tLoaded desktop\tcodex\tgpt-test-astra\n' "$HOME/code/api"
      }
      _t_dev() { print UNEXPECTED_FRESH; }
      _t_open
    ''', _tty=True, **env)
    assert result.returncode == 1, result.stdout
    assert 'UNEXPECTED' not in result.stdout
    assert (tmp_path / 'picker.log').read_text() == ''
    assert 'api-2' in (tmp_path / 'picker.log.all').read_text()


def test_open_attached_flag_with_explicit_slot_stays_direct(zsh):
    result = zsh(r'''
      _t_dev() { print -r -- "DIRECT $*"; }
      _t_open api 2 --attached
    ''')
    assert result.stdout.strip() == 'DIRECT api 2'


def test_open_batch_tab_failure_reports_exact_attach_command(zsh):
    result = zsh(r'''
      _dev_open_tab() { return 1; }
      _t_open() { print -r -- "OPEN $*"; }
      _t_open_selected $'local\tapi-2\tdetached\tFirst\nlocal\tapi-4\tdetached\tSecond' '' ''
    ''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Attach with: t open api 4 --cli --local' in result.stdout
    assert 'OPEN api 2 --cli --local' in result.stdout
