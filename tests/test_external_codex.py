"""External desktop inventory never turns a shared service into a close target."""
import fcntl
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from test_agent_seam import zsh, _fg_world  # noqa: F401
from test_kill_multiselect import _picker_stub, PICKER_MOCK
from test_picker_ui import picker  # noqa: F401


SID = '12345678-1234-5678-9abc-123456789abc'
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def desktop(tmp_path):
    spec = importlib.util.spec_from_file_location('external', ROOT / 'libexec/t_external_codex.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / 'codex'
    locks = home / 'thread-writer-locks'
    locks.mkdir(parents=True)
    cwd = tmp_path / 'project'
    cwd.mkdir()
    rollout = home / 'rollout.jsonl'
    rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(cwd)}}))
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('create table threads (id, cwd, rollout_path, title, source, archived)')
        db.execute('insert into threads values (?,?,?,?,?,0)',
                   (SID, str(cwd), str(rollout), 'External\nconversation', 'vscode'))
    lock = (locks / (SID + '.lock')).open('w')
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = dict(ps='100 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n'
                    '101 100 /Applications/ChatGPT.app/Contents/Resources/codex\n',
                 files=f'p101\nn{locks}/{SID}.lock\n', calls=[])
    def run(args):
        state['calls'].append(args)
        return subprocess.CompletedProcess(args, 0, state['ps' if args[0] == 'ps' else 'files'], '')
    yield module, home, cwd, rollout, lock, state, run
    lock.close()


def test_desktop_requires_live_writer_and_exact_rollout(desktop):
    m, home, cwd, rollout, lock, state, run = desktop
    rows = m.discover(home, runner=run, transcripts=True)
    assert rows == [f'{SID}\t{cwd}\tproject:app-{SID[:8]}\tapp\tactive\tExternal conversation\tcodex\t{rollout}']
    assert all(c[0] in ('ps', 'lsof') for c in state['calls'])
    assert m.discover(home, [f'{SID}\t{cwd}\tapi-1'], runner=run) == []
    fcntl.flock(lock, fcntl.LOCK_UN)
    assert m.discover(home, runner=run) == []  # Saved history is not a running session.


@pytest.mark.parametrize('failure', ['wrong-id', 'wrong-cwd', 'symlink', 'subagent', 'archived', 'missing-rollout'])
def test_desktop_invalid_metadata_never_becomes_live(desktop, failure):
    m, home, cwd, rollout, lock, state, run = desktop
    if failure == 'wrong-id':
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'other', 'cwd': str(cwd)}}))
    elif failure == 'wrong-cwd':
        rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID, 'cwd': '/other'}}))
    elif failure == 'symlink':
        target = rollout.with_suffix('.saved'); rollout.rename(target); rollout.symlink_to(target)
    elif failure == 'missing-rollout':
        rollout.unlink()
    else:
        with sqlite3.connect(home / 'state_5.sqlite') as db:
            db.execute('update threads set source=?' if failure == 'subagent' else 'update threads set archived=?',
                       ('{"subagent":{}}' if failure == 'subagent' else 1,))
    assert m.discover(home, runner=run) == []


@pytest.mark.parametrize('ps', [
    '101 1 /Applications/ChatGPT.app/Contents/Resources/codex\n',
    '100 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n101 1 codex\n',
    '100 1 /Applications/ChatGPT.app/Contents/MacOS/helper\n101 100 codex\n',
])
def test_bundle_path_or_unrelated_app_is_not_desktop_ownership(desktop, ps):
    m, home, cwd, rollout, lock, state, run = desktop
    state['ps'] = ps
    assert m.discover(home, runner=run) == []


@pytest.mark.parametrize('change', ['exit', 'reparent', 'new-holder', 'release'])
def test_desktop_rechecks_ownership_after_metadata(desktop, change):
    m, home, cwd, rollout, lock, state, run = desktop
    n = 0
    def racing(args):
        nonlocal n
        if args[0] == 'ps':
            n += 1
        if n == 2:
            if change == 'exit': state['ps'] = ''
            if change == 'reparent': state['ps'] = state['ps'].replace('101 100', '101 1')
            if change == 'new-holder': state['files'] = state['files'].replace('p101', 'p102')
            if change == 'release': fcntl.flock(lock, fcntl.LOCK_UN)
        return run(args)
    assert m.discover(home, runner=racing) == []


def test_desktop_missing_state_and_inspection_errors(desktop, capsys, monkeypatch):
    m, home, cwd, rollout, lock, state, run = desktop
    assert m.discover(home / 'missing', runner=run) == []
    def failure(args):
        return subprocess.CompletedProcess(args, 1, '', 'permission denied')
    with pytest.raises(ValueError, match='ownership'):
        m.discover(home, runner=failure)
    monkeypatch.setattr(m, 'discover', lambda *a, **k: (_ for _ in ()).throw(ValueError('unavailable')))
    import io
    monkeypatch.setattr(m.sys, 'stdin', io.StringIO(''))
    assert m.main([str(home)]) == 0
    assert 'discovery unavailable' in capsys.readouterr().err


def test_external_app_close_never_reaches_process_or_remote_dispatch(zsh):
    label = f'project:app-{SID[:8]}'
    result = zsh(f'''
      _dev_fg_match() {{ print -u2 UNSAFE_PROCESS_LOOKUP; return 0; }}
      _dev_remote_fg_open() {{ print -u2 UNSAFE_REMOTE; return 0; }}
      _dev_kill_fg {label} 1
      _dev_open_fg {label}
    ''')
    assert result.returncode == 1
    assert 'open or close it in Codex' in result.stderr
    assert 'UNSAFE' not in result.stderr
    if zsh.log.exists():
        assert 'kill-session' not in zsh.log.read_text()


@pytest.mark.parametrize('snapshot', ['', '1'])
@pytest.mark.parametrize('terminal', [True, False])
def test_bundled_codex_cli_requires_terminal_and_excludes_service(zsh, tmp_path, snapshot, terminal):
    executable = '/Applications/ChatGPT.app/Contents/Resources/codex'
    env = _fg_world(zsh, tmp_path, siblings=((4243, executable), (4244, executable)))
    args = tmp_path / 'args'; args.write_text('4243 codex\n4244 codex app-server\n')
    # Extend only the synthetic ps fixture; never inspect a real PID's terminal.
    ps = tmp_path / 'stubbin/ps'
    ps.write_text(ps.read_text().replace('pid="${@: -1}"',
        'if [[ "$2" == tty= ]]; then echo "${FAKE_TTY:-??}"; exit 0; fi\npid="${@: -1}"'))
    for producer in ('_dev_fg_rows --ownership-handles', '_dev_fg_pids'):
        result = zsh(producer, **env, FAKE_ARGS=str(args), FAKE_TTY='ttys001' if terminal else '??',
                     FAKE_PS_SNAPSHOT=snapshot)
        assert result.returncode == 0, result.stderr
        assert ('p4243' in result.stdout) is terminal
        assert 'p4244' not in result.stdout


def test_supervised_slot_chooses_client_and_does_not_duplicate_it(zsh, tmp_path):
    env = _fg_world(zsh, tmp_path)
    (tmp_path / 'ps.txt').write_text('1 0 launchd\n4200 1 python\n4241 4200 codex\n4242 4200 codex\n')
    args = tmp_path / 'args'; args.write_text('4241 codex app-server --listen stdio\n4242 codex --remote ws://localhost\n')
    result = zsh('''
      tmux() {
        case $1 in
          list-sessions) print dev-api-1 ;;
          list-panes) print 'dev-api-1|4200' ;;
        esac
      }
      _dev_session_claude_pid dev-api-1
      _dev_fg_rows --ownership-handles
      _dev_fg_pids
    ''', **env, FAKE_ARGS=str(args), FAKE_PS_SNAPSHOT='1')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['4242']


def test_external_rows_survive_picker_filters_and_refuse_app_close(zsh):
    log = _picker_stub(zsh)
    rows = '\n'.join([
        f'-\t/work\tproject:p4242\tattached\tactive\tStandalone Codex\tcodex\tmodel',
        f'{SID}\t/work\tproject:app-{SID[:8]}\tapp\tactive\tApp conversation\tcodex\tmodel',
    ])
    result = zsh(PICKER_MOCK + '_dev_session_rows() { print -r -- "$FAKE_DETAILS"; }; _dev_kill_pick',
                 _tty=True, FAKE_DETAILS=rows, FZF_LOG=str(log), FZF_CANCEL='1')
    assert result.returncode == 130
    assert not Path(f'{log}.visible').read_text().strip()
    all_rows = Path(f'{log}.all').read_text()
    assert 'project:p4242' in all_rows and f'project:app-{SID[:8]}' in all_rows
    result = zsh('_dev_kill_selected "$PICK"', PICK=f'local\tproject:app-{SID[:8]}\tapp\tSample')
    assert result.returncode == 1 and 'Individual app close is unavailable' in result.stderr


@pytest.mark.parametrize('force', [False, True])
def test_explicit_standalone_codex_confirms_even_without_transcript(zsh, tmp_path, force):
    env = _fg_world(zsh, tmp_path, siblings=((4243, 'codex'),))
    result = zsh('''
      read() { if [[ $1 == -q ]]; then print -u2 CONFIRMED; return 0; fi; builtin read "$@"; }
      kill() { print -r -- "SIGNAL $*"; }
      _dev_kill_fg p4243 ''' + ('1' if force else ''), **env,
                 FAKE_START='Fri Nov 20 08:00:00 2026')
    assert result.returncode == 0, result.stderr
    assert 'SIGNAL -TERM 4243' in result.stdout
    assert ('CONFIRMED' in result.stderr) is (not force)


def test_standalone_owner_change_during_confirmation_never_signals(zsh, tmp_path):
    env = _fg_world(zsh, tmp_path, siblings=((4243, 'codex'),))
    result = zsh('''
      read() { if [[ $1 == -q ]]; then print '1 0 launchd' > "$FAKE_PS"; return 0; fi; builtin read "$@"; }
      kill() { print -u2 UNSAFE_SIGNAL; }
      _dev_kill_fg p4243
    ''', **env, FAKE_START='Fri Nov 20 08:00:00 2026')
    assert result.returncode == 1
    assert 'nothing stopped' in result.stderr and 'UNSAFE_SIGNAL' not in result.stderr


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.fzf_compat
def test_external_picker_renders_and_reveals_hidden_sessions(picker, monkeypatch, width, height):
    import test_open_picker
    monkeypatch.setattr(test_open_picker, 'ROWS', r'''
      _dev_session_rows() {
        printf -- '-\t%s\tapi-4\tdetached\tactive\tFinished task\tcodex\tmodel\n' "$HOME/code/api"
        printf -- '-\t%s\tweb:p4242\tattached\tactive\tDirect terminal\tcodex\tmodel\n' "$HOME/code/web"
        printf '12345678-1234-5678-9abc-123456789abc\t%s\tweb:app-12345678\tapp\tactive\tApp conversation\tcodex\tmodel\n' "$HOME/code/web"
      }
    ''')
    start, wait, keys, finish = picker
    start(profile='public-kill', width=width, height=height)
    before = wait('1/1 (0)')
    assert '2 hidden' in before
    keys('C-f')
    after = wait('3/3 (0)')
    assert 'Filters OFF' in after and 'app-' in after and 'p4242' in after
    captures = Path('artifacts/external-codex')
    captures.mkdir(parents=True, exist_ok=True)
    (captures / f'{width}x{height}.txt').write_text(before + '\n\nFILTERS OFF\n\n' + after)
    assert finish('Escape', status=130) == []
