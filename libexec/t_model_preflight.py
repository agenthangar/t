"""Explicit model-test admission and interruption of the displayed session set."""
import fcntl
import os
from pathlib import Path
import runpy
import subprocess
import threading
import time

LIB = Path(__file__).resolve().parent


def helper(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def legacy_owners(manager, view):
    if view.get('legacy_owned'):
        return []
    path = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 't/local-worker/worker.lock'
    try:
        with path.open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return []
            except BlockingIOError:
                pass
    except FileNotFoundError:
        return []
    try:
        result = subprocess.run(['lsof', '-t', str(path)], capture_output=True, text=True, timeout=3)
        pids = sorted({int(p) for p in result.stdout.split() if p.isdigit()} - {view.get('pid')})
    except (OSError, subprocess.SubprocessError):
        pids = []
    return [dict(id='legacy:' + str(pid), pid=pid, identity=manager['process_identity'](pid),
                 model='Local model · older worker', kind='legacy', tmux='', pane='') for pid in pids] or [
                     dict(id='legacy:unknown', pid=0, identity=None, model='Local model · older worker', kind='legacy', tmux='', pane='')]


def snapshot(manager):
    view = manager['rpc']('test-preflight')
    return dict(view, legacy=legacy_owners(manager, view))


def same_owners(before, after):
    return all(before.get(k) == after.get(k) for k in ('generation', 'owners', 'legacy'))


def session_targets(owners, recovery, manager, activities=None):
    """Only verified local CLI ancestry supplies a session name or control target."""
    sockets = {o['tmux'] for o in owners if o.get('tmux', '').startswith('/')}
    if any(not o.get('tmux') for o in owners):
        result = recovery['run'](['tmux', 'display-message', '-p', '#{socket_path}'])
        if not result.returncode and result.stdout.strip().startswith('/'):
            sockets.add(result.stdout.strip())
    panes = [(socket, p) for socket in sorted(sockets) for p in recovery['panes'](socket)]
    targets = {}
    for item in owners:
        target = None
        if item['identity'] and manager['process_identity'](item['pid']) == item['identity']:
            for socket, pane in panes:
                if not recovery['descendant'](item['pid'], pane['pane_pid']):
                    continue
                candidate = recovery['owner'](socket, pane)
                if (candidate.get('agent') in ('codex', 'claude') and candidate.get('pane_dead') != '1'
                        and candidate.get('sid')
                        and (recovery['descendant'](item['pid'], candidate['pid'])
                             or (item['kind'] == 'session' and recovery['descendant'](candidate['pid'], item['pid'])))
                        and not recovery['descendant'](os.getpid(), candidate['pid'])):
                    target = candidate
                break
        key = (target['socket'], target['pane'], target['sid']) if target else item['id']
        row = targets.setdefault(key, dict(target=target, label=target['session'].removeprefix('dev-') if target else
                                         'Process ' + str(item['pid']) if item['pid'] else 'Unknown session', models=[], owners=[], activities=[]))
        if item['model'] not in row['models']:
            row['models'].append(item['model'])
        row['owners'].append(item)
        activity = (activities or {}).get(item['id'], 'Older worker holds runtime' if item['kind'] == 'legacy' else 'Local work')
        if activity not in row['activities']:
            row['activities'].append(activity)
    return list(targets.values())


def session_lines(rows):
    return ['Sessions using the runtime (' + str(len(rows)) + ')'] + [
        '  ' + row['label'] + ' · ' + ', '.join(row['models']) +
        ' · ' + ', '.join(row['activities']) +
        ('' if row['target'] else ' · interruption unavailable') for row in rows]


class Reservation:
    def __init__(self, manager, token):
        self.manager, self.token = manager, token
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()

    def call(self, action):
        return self.manager['rpc'](action, token=self.token)

    def heartbeat(self):
        while not self.done.wait(2):
            try:
                self.call('test-heartbeat')
            except (OSError, ValueError) as exc:
                self.error = str(exc)
                return

    def close(self):
        self.done.set()
        self.thread.join(timeout=6)
        try:
            self.call('test-release')
        except (OSError, ValueError):
            pass  # A restarted/expired manager cannot be released using an old token.


def wait_for_release(ui, lease, rows, timeout=35):
    lines = []
    start = time.monotonic()
    ready = False
    def refresh():
        nonlocal ready
        if lease.error:
            raise ValueError(lease.error)
        result = lease.call('test-ready')
        if result.get('error'):
            raise ValueError(result['error'])
        ready = result['ready']
        if not ready and time.monotonic() - start >= timeout:
            raise ValueError('The listed sessions have not released the runtime. No test started; stop their local tasks or wait for them to finish.')
        lines[:] = session_lines(rows) + ['', 'Interruption requested. Waiting for runtime release…',
                                         'No test inference has started.']
        return ready
    choice = ui.pick('Waiting for runtime release', [('cancel', 'Cancel this test')], context=lines, refresh=refresh)
    return ready and choice == '__done__'


def prepare(ui, model):
    """False cancels, None explicitly queues, Reservation holds the exclusive test turn."""
    manager, recovery = helper('t_local_manager'), helper('t_recovery')
    lease = None
    focus = 'wait'
    requested = []
    try:
        view = manager['ensure'](testing=True)
        if not view.get('test_preflight'):
            raise ValueError('The running local manager predates model-test preflight. Let its sessions finish, then restart the local runtime before testing.')
        while True:
            view = snapshot(manager)
            if view.get('reserved'):
                raise ValueError('Another model test holds the runtime. Wait for it to finish before starting this test.')
            owners = view['owners'] + view['legacy']
            rows = session_targets(owners, recovery, manager, view.get('activities')) if owners else []
            if rows:
                entries = [('session:' + str(i), row['label'] + ' · ' + ', '.join(row['models']) + ' · ' + ', '.join(row['activities'])) for i, row in enumerate(rows)]
                entries += [('wait', 'Wait for a turn'), ('interrupt', 'Interrupt all sessions'), ('back', 'Back')]
                choice = ui.pick('Local runtime is in use', entries,
                    default=next((i for i, (key, _) in enumerate(entries) if key == focus), len(rows)),
                    sections={'session:0': 'Sessions using the runtime (' + str(len(rows)) + ')', 'wait': 'Actions'},
                    context=['Test · ' + model.split('/')[-1],
                             'Stop the current turn in every listed session.',
                             'Conversations stay open; work may need resuming.',
                             'The test starts after all listed sessions release the runtime.'],
                    disabled={'interrupt': 'Some owners cannot be verified or interrupted here. Stop their local tasks in the owning sessions, or wait.'}
                             if any(not r['target'] for r in rows) else {})
                focus = choice
                if choice and choice.startswith('session:'):
                    row = rows[int(choice.split(':')[1])]
                    ui.page('Session using the runtime', session_lines([row]), {'enter', 'q', 'esc'}, keys='enter / q back')
                    continue
                if choice == 'wait':
                    return None
                if choice != 'interrupt':
                    return False
                if any(not r['target'] for r in rows):
                    raise ValueError('Some session owners cannot be verified or interrupted here')
            if not same_owners(view, snapshot(manager)):
                continue
            if any(not recovery['current'](r['target']) for r in rows):
                continue
            token = manager['rpc']('test-reserve', generation=view['generation'], owners=view['owners'], pid=os.getpid())['token']
            lease = Reservation(manager, token)
            # Reservation freezes managed admission. Verify the whole displayed
            # set once more before sending any input, including legacy holders.
            if not same_owners(view, snapshot(manager)) or any(not recovery['current'](r['target']) for r in rows):
                lease.close()
                lease = None
                continue
            if rows:
                # Validate all targets before the first send; per-target current
                # checks below catch replacement during a partial delivery.
                for row in rows:
                    target = row['target']
                    if not recovery['current'](target):
                        raise ValueError('Session ownership changed during interruption. No test started; review the current sessions.')
                    result = recovery['tmux'](target['socket'], 'send-keys', '-t', target['pane'], 'Escape')
                    if result.returncode:
                        raise ValueError('Could not interrupt ' + row['label'] + '. No test started; previously requested interruptions may still finish.')
                    requested.append(row['label'])
                lease.call('test-interrupt')
            if wait_for_release(ui, lease, rows):
                return lease
            lease.close()
            return False
    except (OSError, ValueError) as exc:
        if lease:
            lease.close()
        ui.page('Model test not started', [str(exc), *(['Interruption requested for: ' + ', '.join(requested)] if requested else [])],
                {'enter', 'q', 'esc'}, keys='enter / q back')
        return False
    except BaseException:
        if lease:
            lease.close()
        raise
