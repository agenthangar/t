"""Public open picker parity with the detailed session list, using sandbox stubs."""
import pytest

from test_agent_seam import zsh  # noqa: F401 — shared isolated shell fixture


ROWS = r'''
  _dev_session_rows() {
    [[ $1 == --details ]] || { print -u2 MISSING_DETAILS; return 1; }
    printf 'sid1\t%s\tapi-2\tattached\tactive\tFirst task · #12 merged, still in progress\tcodex\tgpt-test-astra\t2026-10-23T16:41:07Z\n' "$HOME/code/api"
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
Path(os.environ['PICKER_LOG'] + '.header').write_text(Path(os.environ['T_PICKER_CURRENT_HEADER']).read_text())
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


def test_open_picker_does_not_run_callers_exit_hook_in_subshell(zsh, tmp_path):
    env = picker_stub(tmp_path)
    result = zsh(ROWS + r'''
      autoload -Uz add-zsh-hook
      terminal_save() { print -u2 -- "SESSION_SAVE subshell=$ZSH_SUBSHELL"; }
      add-zsh-hook zshexit terminal_save
      _t_dev() { print -r -- "CLI $*"; }
      _t_open --attached
      print -r -- "PICKER_RC=$?"
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert output.count('SESSION_SAVE') == 1, output
    assert 'SESSION_SAVE subshell=0' in output
    assert 'PICKER_RC=0' in result.stdout


def test_bare_open_inside_worktree_shows_every_detail_and_repo(zsh, tmp_path):
    env = picker_stub(tmp_path)
    result = zsh(ROWS + r'''
      mkdir -p "$HOME/code/.worktrees/api/9"
      cd "$HOME/code/.worktrees/api/9"
      _t_dev() { print -r -- "CLI $*"; }
      COLUMNS=200
      _t_open --attached
    ''', _tty=True, TZ='UTC', **env)
    assert result.returncode == 0, result.stdout
    rows = (tmp_path / "picker.log").read_text().splitlines()
    assert [row.split('\t')[1] for row in rows] == ['api-2', 'api-4', 'web-6', 'web:abcdef12']
    display = [row.split('\t')[-1].rstrip() for row in rows]
    assert all('STATUS' not in row for row in display)
    heading = (tmp_path / 'picker.log.header').read_text().splitlines()[-1]
    assert heading.split() == ['REPO', 'SLOT', 'AGENT', 'HOST', 'UI', 'STATUS', 'MODEL', 'WORKING', 'ON', 'LAST', 'TURN']
    assert '● ✓ ↻' in display[0] and '› cli' in display[0]
    assert '⬡ codex' in display[0] and 'gpt-test-astra' in display[0]
    assert 'First task · #12 merged, still in progress' in display[0]
    assert '2026-10-23' in display[0]
    assert '▣ desktop' in display[1] and 'No conversation recorded' in display[1]
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
    assert result.returncode == 1, result.stdout
    rows = (tmp_path / 'picker.log').read_text().splitlines()
    assert [row.split('\t')[1] for row in rows] == ['api-2', 'api-4']
    assert 'retired integration' in result.stdout
    assert 'UNEXPECTED' not in result.stdout


def test_open_mixed_batch_opens_valid_selections_in_windows(zsh, tmp_path):
    env = picker_stub(tmp_path, 'api-2,api-4,web-6,web:abcdef12')
    result = zsh(ROWS + r'''
      DEV_OPEN_MODE_DEFAULT=app
      _t_open_app() { print -r -- "APP $*"; }
      _t_dev() { print -r -- "CLI $*"; }
      _dev_open_window() { print -r -- "WINDOW $*"; }
      _t_open --attached
    ''', _tty=True, **env)
    assert result.returncode == 1, result.stdout
    assert 'retired integration' in result.stdout
    assert 'WINDOW t open web 6 --cli --local' in result.stdout
    assert 'WINDOW t open web:abcdef12 --cli --local' in result.stdout
    assert 'WINDOW t open api 2 --cli --local' in result.stdout
    assert 'CLI ' not in result.stdout


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


@pytest.mark.parametrize('args,actions,selected,attached', [('', 'off', 'api-2', 'OFF'),
                                                            ('--attached', 'on', 'web-6', 'ON: unattached')])
def test_open_hotkeys_show_all_and_restore_defaults(zsh, tmp_path, args, actions, selected, attached):
    env = picker_stub(tmp_path, selected)
    result = zsh(ROWS + r'''
      _t_dev() { print -r -- "CLI $*"; }
      _t_open ''' + args, _tty=True, PICK_ACTIONS=actions, **env)
    assert result.returncode == 0, result.stdout
    assert 'CLI ' + selected.replace('-', ' ') + ' --local' in result.stdout
    header = (tmp_path / 'picker.log.header').read_text()
    assert 'Filters ' + attached.split(':')[0] in header
    if ': ' in attached:
        assert attached.split(': ', 1)[1] in header
    assert header.splitlines()[-1].endswith('LAST TURN')


def test_open_attached_view_selects_only_attached_after_show_all(zsh, tmp_path):
    env = picker_stub(tmp_path, 'api-2')
    result = zsh(ROWS + r'''
      _t_dev() { print -r -- "CLI $*"; }
      _t_open
    ''', _tty=True, PICK_ACTIONS='first,off,on', **env)
    assert result.returncode == 0, result.stdout
    assert 'CLI api 2 --local' in result.stdout
    assert 'api-2' in (tmp_path / 'picker.log.step0').read_text()
    assert 'web-6' in (tmp_path / 'picker.log.step1').read_text()
    assert 'web-6' not in (tmp_path / 'picker.log.step2').read_text()
    header = (tmp_path / 'picker.log.header').read_text()
    assert 'Filters ON' in header and 'attached ·' in header


def test_open_remote_picker_retains_model_and_selected_host(zsh, tmp_path):
    env = picker_stub(tmp_path, 'alias-2,web-4')
    result = zsh(r'''
      REMOTE_HOSTS[mini]=mini
      _dev_rows_all() {
        [[ $1 == --best-effort && $2 == --details ]] || return 1
        printf 'mini\tsid1\t%s\talias-2\tattached\tactive\tRemote first\tcodex\tgpt-test-astra\t2026-10-23T16:41:07Z\n' "$HOME/code/api"
        printf 'studio\tsid2\t%s\tweb-4\tdetached\tactive\tRemote second\tclaude\tclaude-test-model\n' "$HOME/code/web"
        printf 'local\tsid3\t%s\tapi-3\tdetached\tactive\tLocal included\tcodex\tgpt-test-astra\n' "$HOME/code/api"
      }
      _dev_remote_open() { print -r -- "REMOTE $*"; }
      _dev_open_window() { print -r -- "WINDOW $*"; }
      COLUMNS=200
      _t_open -r --attached
    ''', _tty=True, TZ='UTC', **env)
    assert result.returncode == 0, result.stdout
    rows = (tmp_path / 'picker.log').read_text()
    assert 'mini' in rows and 'studio' in rows and 'claude-test-model' in rows
    assert 'Local included' in rows
    assert 'Last turn' in rows and '2026-10-23' in rows
    assert 'WINDOW t open web 4 --cli --host studio' in result.stdout
    assert 'WINDOW t open alias 2 --cli --host mini' in result.stdout
    assert 'REMOTE ' not in result.stdout


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
    assert result.returncode == 130, result.stdout
    assert 'UNEXPECTED' not in result.stdout
    assert (tmp_path / 'picker.log').read_text() == ''
    assert 'api-2' in (tmp_path / 'picker.log.all').read_text()


def test_open_attached_flag_with_explicit_slot_stays_direct(zsh):
    result = zsh(r'''
      _t_dev() { print -r -- "DIRECT $*"; }
      _t_open api 2 --attached
    ''')
    assert result.stdout.strip() == 'DIRECT api 2'


def test_open_batch_window_failure_reports_every_exact_attach_command(zsh):
    result = zsh(r'''
      _dev_open_window() { return 1; }
      _t_open() { print -r -- "OPEN $*"; }
      _t_open_selected $'local\tapi-2\tdetached\tFirst\nlocal\tapi-4\tdetached\tSecond' '' ''
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Attach with: t open api 4 --cli --local' in result.stderr
    assert 'Attach with: t open api 2 --cli --local' in result.stderr
    assert 'OPEN ' not in result.stdout


@pytest.mark.parametrize('tmux', ['', 'isolated-stub'])
def test_open_single_selection_keeps_existing_attachment_behavior(zsh, tmux):
    result = zsh(r'''
      _dev_open_window() { print UNEXPECTED_WINDOW; }
      _t_open() { print -r -- "OPEN $*"; }
      _t_open_selected $'local\tapi-2\tdetached\tOnly' '' ''
    ''', TMUX=tmux, FAKE_HAS_SESSION='1')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'UNEXPECTED_WINDOW' not in result.stdout
    if tmux:
        assert 'switch-client -t =dev-api-2:' in zsh.log.read_text()
        assert 'OPEN ' not in result.stdout
    else:
        assert 'OPEN api 2 --cli --local' in result.stdout


def test_open_batch_continues_after_first_window_fails(zsh):
    result = zsh(r'''
      _dev_open_window() { print -r -- "WINDOW $*"; [[ $1 != *'api 2 '* ]]; }
      _t_open() { print UNEXPECTED_ATTACH; }
      _t_open_selected $'local\tapi-2\tdetached\tFirst\nmini\tapi-4\tdetached\tSecond' '' ''
    ''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'WINDOW t open api 2 --cli --local' in result.stdout
    assert 'WINDOW t open api 4 --cli --host mini' in result.stdout
    assert 'Attach with: t open api 2 --cli --local' in result.stderr
    assert result.stderr.count('Attach with:') == 1
    assert 'UNEXPECTED_ATTACH' not in result.stdout


def test_open_help_describes_simple_picker(t_mod):
    parser = t_mod.build_parser()._subparsers._group_actions[0].choices['open']
    help_text = parser.format_help() + parser.help_details
    assert 'Ctrl-F toggles filters' in help_text
    assert 'Ctrl-X resets them' in help_text
    assert 'repeat to clear' in help_text
    assert '--new' in help_text
    assert 'Alt-N' not in help_text and 'LAST TURN' in help_text
    assert 'Ctrl-/' not in help_text
    assert 'including the first' in help_text
    assert 'each open in a new window' in help_text


@pytest.mark.parametrize('width', [200, 120, 80, 44, 32])
@pytest.mark.parametrize('remote,scoped', [(False, False), (True, False), (False, True), (True, True)])
def test_open_compact_layout_and_search(t_mod, monkeypatch, capsys, width, remote, scoped):
    import io
    title = 'Fix 界面 login e\u0301 with a long task description that must remain searchable'
    raw = (f'mini\tsid\t/code/api\tapi-2\tattached\tactive\t{title}\tcodex\tgpt-test-astra\t2026-10-23T16:41:07Z\n'
           'local\t-\t/code/web\tweb-4\tapp\tnone\tNo conversation recorded\tcodex\t-\n')
    monkeypatch.setattr(t_mod.sys, 'stdin', io.StringIO(raw))
    monkeypatch.setattr(t_mod, '_columns', lambda: width)
    args = ['__session-picker-rows', '--compact', '--header'] + (['--remote'] if remote else []) + (['--repo'] if scoped else [])
    assert t_mod.main(args) == 0
    heading, *lines = capsys.readouterr().out.splitlines()
    first, unknown = [line.split('\t') for line in lines]
    assert first[:3] == ['mini', 'api-2', 'attached']
    assert len(first) == len(unknown) == 5
    assert title in first[3] and 'gpt-test-astra' in first[3]
    assert 'Last turn' not in first[3] and '2026-10-23' not in first[4]
    assert 'LAST TURN' not in heading
    available = width - (6 if width <= 60 else 12)
    assert t_mod._display_width(heading) <= available
    assert 'WORKING ON' in heading or 'TASK' in heading
    assert 'Fix' in first[4]
    assert all(t_mod._display_width(row[4].rstrip()) <= available for row in (first, unknown))
    if width < 120:
        assert first[4].startswith('●')
        assert 'api-2' in first[4]
        assert '● ✓' in first[4]
        if remote:
            assert 'mini/api-2' in first[4]
    if width >= 120:
        assert '⬡ codex' in first[4] and '› cli' in first[4]


def test_open_local_override_never_scans_remote(zsh, tmp_path):
    env = picker_stub(tmp_path, 'web-6')
    result = zsh(ROWS + r'''
      _dev_rows_all() { print -u2 UNEXPECTED_REMOTE; return 1; }
      _t_dev() { print -r -- "CLI $*"; }
      _t_open --local
    ''', _tty=True, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'UNEXPECTED_REMOTE' not in result.stdout + result.stderr
    assert 'CLI web 6 --local' in result.stdout


@pytest.mark.parametrize('flag', ['', '-r'])
def test_open_default_and_remote_keep_local_and_remote_duplicate_slots(zsh, tmp_path, flag):
    env = picker_stub(tmp_path, 'api-2')
    result = zsh(r'''
      _dev_rows_all() {
        printf 'local\tsid1\t%s\tapi-2\tdetached\tactive\tLocal task\tcodex\tmodel\n' "$HOME/code/api"
        printf 'mini\tsid2\t%s\tapi-2\tdetached\tactive\tRemote task\tcodex\tmodel\n' "$HOME/code/api"
      }
      _t_dev() { print -r -- "CLI $*"; }
      _dev_open_window() { print -r -- "WINDOW $*"; }
      _t_open ''' + flag, _tty=True, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = (tmp_path / 'picker.log').read_text().splitlines()
    assert [row.split('\t')[:2] for row in rows] == [['local', 'api-2'], ['mini', 'api-2']]
    assert 'WINDOW t open api 2 --cli --local' in result.stdout
    assert 'WINDOW t open api 2 --cli --host mini' in result.stdout
    assert 'CLI ' not in result.stdout


@pytest.mark.parametrize('width', [32, 44, 80])
def test_air_status_cells_keep_host_and_slot_aligned(t_mod, monkeypatch, width):
    monkeypatch.setattr(t_mod, '_columns', lambda: width)
    rows = [dict(host='local', slot='api-2', state=state, context=context,
                 summary=summary, agent='codex', model='-') for state, context, summary in
            [('detached', 'none', 'Idle'), ('detached', 'active', 'Working'),
             ('attached', 'active', '#42 merged, still in progress')]]
    heading, *rendered = t_mod._session_picker_rows(rows, remote=True, compact=True, header=True)
    displays = [row.split('\t')[-1] for row in rendered]
    assert len({t_mod._display_width(line[:line.index('local/api-2')]) for line in displays}) == 1
    assert all('local/api-2' in line for line in displays)
    assert 'STATE' in heading


@pytest.mark.parametrize('status', [0, 7])
def test_progress_animation_keeps_exit_hooks_in_parent(zsh, status):
    result = zsh(r'''
      autoload -Uz add-zsh-hook
      terminal_save() { print -u2 -- "SESSION_SAVE subshell=$ZSH_SUBSHELL"; }
      TRAPEXIT() { print -u2 -- "EXIT_TRAP subshell=$ZSH_SUBSHELL"; }
      add-zsh-hook zshexit terminal_save
      _T_PROGRESS=1
      work() { sleep 0.4; print -u2 OPERATION_DIAGNOSTIC; return ''' + str(status) + r'''; }
      _t_with_progress 'Testing animation' work
      print -r -- "OPERATION_RC=$?"
    ''', _tty=True, TERM='xterm-256color')
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert output.count('SESSION_SAVE') == 1, output
    assert output.count('EXIT_TRAP') == 1, output
    assert 'SESSION_SAVE subshell=0' in output
    assert 'EXIT_TRAP subshell=0' in output
    assert f'OPERATION_RC={status}' in output
    assert 'OPERATION_DIAGNOSTIC' in output


@pytest.mark.parametrize('close_rc', [0, 1, 130])
def test_open_kill_callback_keeps_caller_context_and_never_starts_fresh(zsh, close_rc):
    result = zsh(ROWS + r'''
      _t_fzf_filtered() {
        zsh -f "$T_PICKER_KILL_CALLBACK" "$(print -r -- "$2" | head -1)"
        print -u2 -- "CLOSE_RC=$?"
        return 130
      }
      _dev_kill_selected() {
        print -u2 -- "CLOSE $1"
        return ''' + str(close_rc) + r'''
      }
      _t_dev() { print UNEXPECTED_FRESH; }
      _t_open --attached
    ''', _tty=True)
    assert result.returncode == 130, result.stdout + result.stderr
    assert f'CLOSE_RC={close_rc}' in result.stdout + result.stderr
    assert 'CLOSE local' in result.stdout + result.stderr
    assert 'UNEXPECTED' not in result.stdout


def test_open_kill_stale_local_row_never_closes_remote(zsh):
    result = zsh(ROWS + r'''
      _t_fzf_filtered() {
        zsh -f "$T_PICKER_KILL_CALLBACK" "$(print -r -- "$2" | head -1)"
        print -u2 -- "CLOSE_RC=$?"
        return 130
      }
      tmux() { return 1; }
      _dev_kill() { print UNEXPECTED_KILL; }
      _t_open --attached
    ''', _tty=True)
    assert result.returncode == 130
    assert 'CLOSE_RC=1' in result.stdout + result.stderr
    assert 'no longer here' in result.stdout + result.stderr
    assert 'UNEXPECTED' not in result.stdout


@pytest.mark.parametrize('width', [200, 120, 80, 44, 32])
def test_last_turn_column_stays_visible_and_searchable(t_mod, monkeypatch, width):
    monkeypatch.setattr(t_mod, '_columns', lambda: width)
    rows = t_mod._parse_rows('local\tsid\t/code/api\tapi-2\tdetached\tactive\tFix login\tcodex\tgpt-test\t2026-11-07T16:41:07Z\nlocal\tother\t/code/web\tweb-4\tdetached\tactive\tUnknown turn\tcodex\t-\t-\n', host_prefixed=True)
    heading, *output = t_mod._session_picker_rows(rows, compact=True, header=True, last_turn=True)
    assert ('LAST TURN' if width >= 44 else 'TURN') in heading
    stamp = t_mod._session_display_helpers()['format_last_turn'](rows[0]['last_turn_started_at'])
    assert stamp in output[0].split('\t')[3]
    shown = stamp if width >= 100 else stamp[5:16] if width >= 44 else stamp[11:16]
    assert output[0].split('\t')[4].rstrip().endswith(shown)
    assert output[1].split('\t')[4].rstrip().endswith('-')
    available = width - (6 if width <= 60 else 12)
    assert all(t_mod._display_width(line.split('\t')[-1].rstrip()) <= available for line in [heading, *output])


@pytest.mark.parametrize('args', ['', '--local'])
def test_open_requests_waited_snapshot_and_keeps_notice_after_toggle(zsh, tmp_path, args):
    env = picker_stub(tmp_path, 'api-2')
    result = zsh(ROWS + r'''
      _t_discovery_rows() {
        [[ $1 == --open && $2 == --notice-file ]] || return 99
        print -n 'Older results' > "$3"
        local rows=$(_dev_session_rows --details)
        if [[ " $* " == *' --unprefixed '* ]]; then print -r -- "$rows"
        else print -r -- "$rows" | awk 'NF {print "local\t" $0}'; fi
      }
      _t_dev() { print -r -- "CLI $*"; }
      _t_open ''' + args, _tty=True, PICK_ACTIONS='toggle-filters', **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CLI api 2 --local' in result.stdout
    header = (tmp_path / 'picker.log.header').read_text()
    assert 'Filters OFF' in header and 'Older results' in header


@pytest.mark.parametrize('rc', [1, 130])
@pytest.mark.parametrize('args', ['', '--local'])
def test_open_discovery_cancel_and_error_never_start_fresh(zsh, rc, args):
    result = zsh(r'''
      _t_discovery_rows() { return ''' + str(rc) + r'''; }
      _t_dev() { print UNEXPECTED_FRESH; }
      _t_open ''' + args + '; print rc=$?', _tty=True)
    assert f'rc={rc}' in result.stdout and 'UNEXPECTED' not in result.stdout


def test_open_incomplete_empty_scope_never_starts_fresh(zsh):
    result = zsh(r'''
      _t_discovery_rows() {
        print -n 'Incomplete discovery' > "$3"
        printf 'local\tsid\t%s\tweb-9\tattached\tactive\tTask\tcodex\n' "$HOME/code/web"
      }
      _t_dev() { print UNEXPECTED_FRESH; }
      _t_open api; print rc=$?
    ''', _tty=True)
    assert 'rc=1' in result.stdout and 'UNEXPECTED' not in result.stdout
    assert 'discovery is incomplete' in result.stdout + result.stderr
