"""Pane-scoped recovery controls and checkpointed native provider continuation."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import runpy
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

LIB = Path(__file__).resolve().parent


def helper(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def install(socket_path, pane, command, replace=False):
    local = helper('t_local_status')
    tmux = lambda *args: local['tmux'](socket_path, *args)
    existing = tmux('show-options', '-p', '-v', '-t', pane, '@t-model-command').stdout.strip()
    if existing and not replace:
        return False
    keys = tmux('list-keys', '-T', 'prefix', 'F6')
    if keys.returncode == 0 and keys.stdout.strip() and 't: model controls' not in tmux('list-keys', '-N', '-T', 'prefix', 'F6').stdout:
        return False  # Never overwrite a user's key.
    result = tmux('bind-key', '-N', 't: model controls', '-T', 'prefix', 'F6',
                  'if-shell', '-F', '#{!=:#{@t-model-command},}',
                  'display-popup -E -w 90% -h 90% "#{@t-model-command}"',
                  'display-message "No model controller in this pane"')
    if result.returncode:
        return False
    return tmux('set-option', '-p', '-t', pane, '@t-model-command', command).returncode == 0


def pane_requests(socket_path, pane):
    local = helper('t_local_status')
    info = local['pane_info'](socket_path, pane)
    if not info:
        raise ValueError('The source pane no longer exists')
    view = helper('t_local_manager')['status']()
    clients = {c['id'] for c in view.get('clients', []) if c.get('tmux') == socket_path and c.get('pane') == pane and c.get('owner_verified')}
    rows = [r for r in view.get('requests', []) if r.get('client') in clients]
    return primary_request(rows)


def primary_request(rows):
    pending = [r for r in rows if r['state'] not in ('completed', 'failed', 'cancelled', 'timed_out', 'busy')]
    blocked = next((r for r in pending if r.get('memory')), None)
    failure = next((r for r in reversed(rows) if r.get('memory') and r['state'] in ('failed', 'timed_out', 'busy')), None)
    if failure and any(r['state'] == 'completed' and r.get('created', 0) > failure.get('finished', 0) for r in rows):
        failure = None
    if blocked:
        return blocked
    if failure and not any(r.get('created', 0) > failure.get('finished', 0) for r in pending):
        return failure
    return next(iter(pending), None) or failure or {}


def call(endpoint, action='status', **data):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(20)
        connection.connect(endpoint)
        connection.sendall(json.dumps(dict(data, action=action)).encode()+b'\n')
        with connection.makefile('rb') as stream:
            value = json.loads(stream.readline(1024*1024))
        if value.get('error'):
            raise ValueError(value['error'])
        return value


def model_choices(provider, context):
    if provider == 'local':
        catalogue = helper('t_local_inference')['catalogue'](context=context)
        manager, runtime = helper('t_local_manager'), helper('t_local_runtime')
        view, available = manager['status'](), runtime['available_memory']()
        rows = []
        for r in catalogue['models']:
            enabled = True
            try:
                manager['require_enabled'](r['provider'])
                availability = local_availability(r, context, catalogue.get('hardware', {}), view, available, runtime)
            except ValueError as exc:
                enabled, availability = False, str(exc)
            rows.append(dict(id=r['provider']+'|'+r['model'], model=r['model'], provider=r['provider'],
                     label=r['model'].split('/')[-1], ready=bool(enabled and r.get('ready') and r.get('fit') is True),
                     images='image' in r.get('input_modalities', []),
                     detail=('Verified on this Mac at '+str(context//1024)+'K' if r.get('ready') else 'Not verified')+
                     ' · '+availability))
        return rows
    agent = 'codex' if provider == 'openai' else 'claude'
    app = helper('t_offline')['ui_helpers']()
    raw = app['_config_live_models'](agent)
    normalized = app['_config_catalog'](agent, raw)
    return [dict(id=r['id'], model=r['id'], provider=provider, label=r['label'], ready=True,
                 images=provider == 'openai' and any('image' in v.get('inputModalities', []) for v in raw if v.get('model') == r['id']),
                 detail='Available in native catalogue') for r in normalized]


def local_availability(row, context, machine, view, available, runtime):
    if view.get('manager') != 'running':
        return 'Availability unknown · runtime '+view.get('manager', 'unavailable')
    loaded = view.get('models', [])
    exact = next((r for r in loaded if r.get('model') == row['model'] and r.get('provider') == row['provider']
                  and r.get('context') == context and r.get('revision') == row.get('revision') and r.get('revision')), None)
    if exact:
        return 'Loaded · '+('generating' if exact.get('active') else 'available in shared queue')
    if any(r.get('memory_bytes') is None for r in loaded):
        return 'Availability unknown · unverified model memory'
    required, ram = row.get('memory_bytes'), machine.get('ram_bytes')
    if required is None or not ram or available is None:
        return 'Availability unknown · memory not measured'
    memory = runtime['memory_shortfall'](required, ram, available, sum(r['memory_bytes'] for r in loaded),
                                         view.get('settings', {}).get('memory_percent', 80))
    if memory['shortfall_bytes']:
        return f"Waiting for memory · {memory['shortfall_bytes']/1024**3:.1f} GiB short"
    if 'NVIDIA' in machine.get('accelerator', ''):
        free, total = runtime['gpu_available'](), machine.get('vram_bytes')
        if free is None or not total:
            return 'Availability unknown · GPU not measured'
        if required > min(total*.85, free-total*.1):
            return 'Waiting for GPU memory'
    return 'Memory available · shared queue'


def startup_argv(argv, continuation):
    """Keep initial input while discarding incompatible cloud launch defaults."""
    text, images = [], []
    args = iter(argv[1:])
    values = {'-c', '--config', '--enable', '--disable', '-m', '--model', '-s', '--sandbox',
              '-a', '--ask-for-approval', '--add-dir', '-C', '--cd'}
    for arg in args:
        if arg == '--':
            text.extend(args)
            break
        if arg in ('--image', '-i'):
            path = next(args, None)
            if not path:
                raise ValueError('Missing startup image path')
            images += ['--image', path]
        elif arg in values:
            next(args, None)
        elif not arg.startswith('-'):
            text.append(arg)
    return [*continuation, *images, *(['--', ' '.join(text)] if text else [])]


def choose(ui, view, fetch=model_choices):
    while True:
        provider = ui.pick('Choose model', [('local', 'Local'), ('openai', 'OpenAI'), ('claude', 'Claude'), ('back', 'Back')],
                           context=['Same worktree · current turn stays queued while browsing.'], back_label='back')
        if provider in (None, 'back'):
            return
        try:
            models = fetch(provider, view['context_tokens'])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            ui.page('Models unavailable', [str(exc)], {'enter', 'q', 'esc'})
            continue
        disabled = {r['id']: 'Verify this model before use.' for r in models if not r['ready']}
        disabled.update({r['id']: 'Screenshot input support is not verified.' for r in models if view.get('attachments') and not r['images']})
        values = {r['id']: r['detail'] for r in models}
        for r in models:
            if (provider == 'local' and r['model'] == view.get('model') and r['provider'] == view.get('provider')
                    and view.get('request', {}).get('memory')):
                values[r['id']] = values[r['id']].split(' · ', 1)[0]+' · '+helper('t_memory_recovery')['blocker'](view['request'])
        selected = ui.pick(provider.title()+' models', [(r['id'], r['label']) for r in models]+[('back', 'Back')],
                           values=values, disabled=disabled,
                           context=['Benchmark unavailable · speed not measured'] if not models else [], back_label='back')
        row = next((r for r in models if r['id'] == selected), None)
        if not row or selected in disabled:
            continue
        context = ['Destination: '+provider.title()+' · '+row['model'], 'Worktree: '+view['cwd'],
                   'Transfer: bounded task instructions and recent evidence.',
                   'Screenshots: '+str(view.get('attachments', 0))+' · '+('supported' if row['images'] else 'support unverified'),
                   'Source conversation is retained if continuation fails.']
        if ui.pick('Continue in this worktree?', [('continue', 'Continue'), ('back', 'Back')], context=context, back_label='back') == 'continue':
            return dict(row, destination=provider, generation=view['generation'])


def waiting_menu(ui, status, submit=None):
    def summary(view):
        row = view.get('request', {})
        verification = view.get('verification', 'Verification unavailable')
        state = helper('t_memory_recovery')['blocker'](row) if row.get('memory') else 'Current state: '+row.get('state', 'idle')
        if getattr(ui, 'cols', lambda: 80)() <= 44:
            verification = verification.replace('Verified on this Mac at ', 'Verified here · ')
            state = state.replace('Waiting for memory', 'Waiting').replace('Timed out · waiting for memory', 'Timed out · memory')
        return [verification, state]
    while True:
        view = status()
        row = view.get('request', {})
        context = summary(view)
        expired = bool(row.get('timeout_phase') or row.get('state') == 'timed_out')
        rows = [('memory', 'Free memory…'), ('model', 'Choose model…'), ('details', 'Details')]
        if expired:
            rows.insert(0, ('retry', 'Retry'))
        rows.append(('back', 'Back to conversation'))
        disabled = {} if submit and view.get('can_switch') else {'model': view.get('unavailable', 'No complete handoff checkpoint is available.'),
                                                                  'retry': 'Retry the turn in the native conversation.'}
        def refresh():
            new = status()
            new_row = new.get('request', {})
            if bool(new_row.get('timeout_phase') or new_row.get('state') == 'timed_out') != expired:
                return True
            context[:] = summary(new)
            return False
        action = ui.pick('Session controls · Prefix F6', rows, context=context, disabled=disabled, refresh=refresh, back_label='back')
        if action in (None, 'back'):
            return
        if action == 'memory':
            helper('t_memory_recovery')['free_memory'](ui, lambda: status().get('request', {}))
        elif action == 'details':
            ui.page('Request details', helper('t_memory_recovery')['details'](row), {'enter', 'q', 'esc'})
        elif action in ('model', 'retry') and action not in disabled:
            fresh = status()
            selection = choose(ui, fresh) if action == 'model' else dict(destination='retry', generation=fresh['generation'])
            if selection:
                try:
                    submit(selection)
                    return
                except (OSError, ValueError) as exc:
                    ui.page('Continuation unavailable', [str(exc), 'Source conversation retained.'], {'enter', 'q', 'esc'})


class Controller:
    def __init__(self, checkpoint, chosen, client_id=None, verification='Verification unavailable'):
        self.cp, self.chosen, self.client_id = checkpoint, chosen, client_id
        self.pending = queue.Queue(maxsize=1)
        self.socket_path = os.environ.get('TMUX', '').rsplit(',', 2)[0]
        self.pane = os.environ.get('TMUX_PANE', '')
        self.local = helper('t_local_status')
        self.verification = verification
        self.info = self.local['pane_info'](self.socket_path, self.pane)

    def verify(self):
        info = self.local['pane_info'](self.socket_path, self.pane)
        if not self.info or not info or info[:3] != self.info[:3] or not self.local['ancestry'](os.getpid(), int(info[1])):
            raise ValueError('Pane ownership changed; reopen controls in the owning session')

    def request(self):
        if not self.client_id:
            return {}
        view = helper('t_local_manager')['status']()
        rows = [r for r in view.get('requests', []) if r.get('client') == self.client_id]
        return primary_request(rows)

    def status(self):
        self.verify()
        with self.cp.lock:
            reason = ''
            try:
                self.cp.prompt()
            except ValueError as exc:
                reason = str(exc)
            return dict(cwd=self.cp.cwd, generation=self.cp.generation, attachments=len(self.cp.attachments),
                        model=self.chosen.get('model'), provider=self.chosen.get('provider'), context_tokens=self.chosen['context_tokens'], request=self.request(),
                        verification=self.verification, can_switch=not reason, unavailable=reason)

    def accept(self, selection):
        self.verify()
        with self.cp.lock:
            self.cp.prompt()
            if selection.get('generation') != self.cp.generation:
                raise ValueError('Conversation changed while choosing; review the updated context')
            if selection.get('destination') not in ('local', 'openai', 'claude', 'retry'):
                raise ValueError('Invalid destination')
            if selection['destination'] == 'retry':
                row = self.request()
                if not row.get('timeout_phase') and row.get('state') != 'timed_out':
                    raise ValueError('The request has not expired')
            else:
                rows = model_choices(selection['destination'], self.chosen['context_tokens'])
                row = next((r for r in rows if r['id'] == selection.get('id')), None)
                if not row or not row['ready'] or (self.cp.attachments and not row['images']):
                    raise ValueError('Destination readiness or screenshot support changed')
                selection = dict(row, destination=selection['destination'], generation=selection['generation'])
            try:
                self.pending.put_nowait(selection)
            except queue.Full:
                raise ValueError('A continuation is already pending') from None
        return {'accepted': True}

    def monitor(self, cp, frontend, terminal):
        try:
            action = self.pending.get_nowait()
        except queue.Empty:
            return None
        self.verify()
        with cp.lock:
            if cp.generation != action['generation']:
                cp.notice = 'Conversation changed; continuation canceled. Open Prefix F6 to review again.'
                return None
            cp.prompt()
        return action

    @contextmanager
    def serve(self):
        self.verify()
        controller = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                try:
                    data = json.loads(self.rfile.readline(65537))
                    value = controller.status() if data.get('action') == 'status' else controller.accept(data['selection']) if data.get('action') == 'switch' else {'error': 'Unknown action'}
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    value = {'error': str(exc)}
                self.wfile.write(json.dumps(value).encode()+b'\n')
        with tempfile.TemporaryDirectory(prefix='t-model-', dir='/tmp') as root:
            endpoint = str(Path(root)/'control.sock')
            with socketserver.ThreadingUnixStreamServer(endpoint, Handler) as server:
                server.daemon_threads = True
                os.chmod(endpoint, 0o600)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--control', endpoint])
                installed = install(self.socket_path, self.pane, command, replace=True)
                try:
                    yield self
                finally:
                    if installed and self.local['tmux'](self.socket_path, 'show-options', '-p', '-v', '-t', self.pane, '@t-model-command').stdout.strip() == command:
                        self.local['tmux'](self.socket_path, 'set-option', '-pu', '-t', self.pane, '@t-model-command')
                    server.shutdown()
                    thread.join(timeout=2)


def run(argv, chosen, cwd):
    """Own each Codex backend until stopped; retain every source checkpoint."""
    offline, local = helper('t_offline'), helper('t_local_inference')
    _, continuation = offline['launch_options'](argv, cwd)
    root = local['helper']('t_local_worker')['state_root']()/('continuation-'+str(time.time_ns()))
    root.mkdir(mode=0o700, parents=True)
    current, native, seed, prompt, attempt = 'local', startup_argv(argv, continuation), None, None, 0
    selected = dict(chosen)
    while True:
        directory = root/str(attempt);directory.mkdir(mode=0o700);attempt += 1
        cp = offline['Checkpoint'](directory, cwd, seed=seed, handoff=prompt, allow_images=True)
        def runner(local_argv, env):
            cp.native_home = env.get('CODEX_HOME')
            controller = Controller(cp, selected, env.get('T_LOCAL_CLIENT_ID'), env.get('T_LOCAL_VERIFICATION', 'Verification unavailable'))
            with controller.serve():
                return offline['supervise'](native, dict(selected, on_cloud_failure='pause'), cwd,
                    checkpoint=cp, monitor=controller.monitor, env=env, server_options=offline['local_server_options'](local_argv))
        try:
            if current == 'local':
                outcome = local['lead'](native, cwd=cwd, chosen=dict(selected, mode='local-only'), runner=runner)
            else:
                cp.native_home = os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))
                controller = Controller(cp, selected)
                with controller.serve():
                    outcome = offline['supervise'](native, dict(selected, on_cloud_failure='pause'), cwd,
                                                 checkpoint=cp, monitor=controller.monitor)
        except offline['RuntimeStopError']:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Never discard retained source history or silently restart a failed handoff.
            print('Continuation failed: '+str(exc)+'\nSource checkpoint retained: '+str(root), file=sys.stderr)
            return 1
        if not isinstance(outcome, dict) or not outcome.get('action'):
            result = outcome.get('exit_code', 1) if isinstance(outcome, dict) else outcome
            if result and seed is not None:
                print('Continuation stopped; source checkpoint retained: '+str(root), file=sys.stderr)
            return result
        action = outcome['action']
        seed, prompt = cp, cp.prompt(action['destination'])
        attachments = [part for path in cp.attachment_paths() for part in ('--image', path)]
        if action['destination'] == 'retry':
            native = [*continuation, *attachments, '--', prompt]
            continue
        if action['destination'] == 'claude':
            executable = shutil.which('claude')
            if not executable or attachments:
                raise ValueError('Claude continuation unavailable; source retained: '+str(root))
            # Native Claude owns its own model picker after this explicit handoff.
            return subprocess.call([executable, '--model', action['model'], '--', prompt], cwd=cwd)
        current = action['destination']
        selected.update(model=action['model'], provider=action['provider'])
        native = [*continuation, *attachments, '--model', action['model'], '--', prompt]


def main(argv):
    with helper('t_offline')['recovery_ui']() as ui:
        if len(argv) == 2 and argv[0] == '--control':
            return waiting_menu(ui, lambda: call(argv[1]), lambda selected: call(argv[1], 'switch', selection=selected))
        if len(argv) == 3 and argv[0] == '--waiting':
            return waiting_menu(ui, lambda: dict(request=pane_requests(argv[1], argv[2]), verification='Local worker', can_switch=False,
                                                unavailable='Use the native /model picker in the source conversation.'))
        raise ValueError('Open model controls with tmux Prefix F6')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        print(str(exc) or 'Canceled', file=sys.stderr)
        raise SystemExit(1)
