"""Shared RailUI model usage, request controls, provider help and read-only monitors."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import runpy
import secrets
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import webbrowser

LIB = Path(__file__).resolve().parent


def helper(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def manager():
    return helper('t_local_manager')


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:500]


def label(state):
    return {'running': 'Generating', 'queued': 'Queued', 'waiting_for_memory': 'Waiting for memory',
            'waiting_for_legacy': 'Waiting for an older session', 'completed': 'Completed',
            'cancelled': 'Cancelled', 'timed_out': 'Queue timed out', 'failed': 'Failed', 'working': 'Working',
            'loading': 'Loading', 'unknown': 'Unknown', 'cancelling': 'Cancelling'}.get(state, clean(state))


def status_lines(view):
    lines = ['Manager     ' + view['manager'], 'Policy      Fair turns · one active generation']
    if view.get('error'):
        lines.append(clean(view['error']))
    for name, base in view.get('endpoints', {}).items():
        lines.append(name + '  ' + base)
    for row in view.get('models', []):
        memory = row.get('memory_bytes')
        lines.append(clean(row['model']) + ' · ' + row['provider'] + ' · ' +
                     (f'{memory / 1024**3:.1f} GiB' + (' estimated' if row.get('estimated') else '') if memory is not None else 'memory unknown') +
                     ' · ' + ('Loaded by t' if row.get('owned') else 'Loaded in provider app'))
    if not view.get('models'):
        lines.append('No loaded models reported.')
    return lines


def request_lines(row):
    return [clean(row['model']), row['provider'] + ' · ' + row['kind'], 'Owner       ' + clean(row.get('owner_label', 'Local client')), 'State       ' + label(row['state']),
            'Queue       ' + str(row.get('queue_position') or '—'),
            'Queue time  ' + str(row.get('queue_seconds', 0)) + 's',
            'Execution   ' + str(row.get('execution_seconds', 0)) + 's',
            'Tokens      ' + str(row.get('tokens') if row.get('tokens') is not None else 'not reported'),
            *([clean(row['error'])] if row.get('error') else [])]


def page(ui, title, lines):
    return ui.page(title, [clean(x) for x in lines], {'enter', 'q', 'esc'}, keys='enter / q back')


def cancel_request(request_id, ui=None):
    m = manager()
    row = next((x for x in m['status']().get('requests', []) if x['id'] == request_id), None)
    if not row:
        raise ValueError('Unknown request ID')
    if row['state'] in ('running', 'loading', 'cancelling'):
        if ui is None:
            raise ValueError('Open this command in a terminal to review cancellation of an active response')
        if ui.pick('Cancel active request?', [('keep', 'Keep running'), ('cancel', 'Cancel this request')],
                   context=[row['model'], 'Only this request is cancelled.', 'The next turn waits for the provider to stop.']) != 'cancel':
            return {'id': request_id, 'state': row['state']}
    return m['rpc']('cancel', request=request_id)


def requests_menu(ui):
    focus = None
    while True:
        view = manager()['status']()
        rows = view.get('requests', [])
        choices = [(x['id'], x.get('owner_label', 'Local client') + ' · ' + x['kind'].title() + ' · ' + x['model'].split('/')[-1]) for x in rows]
        choices += [('refresh', 'Refresh'), ('back', 'Back')]
        picked = ui.pick('Requests', choices, values={x['id']: label(x['state']) for x in rows},
                         context=[] if rows else ['No requests yet.'],
                         default=next((i for i, (key, _) in enumerate(choices) if key == focus), 0))
        if picked in (None, 'back'):
            return
        focus = picked
        row = next((x for x in rows if x['id'] == picked), None)
        if not row:
            continue
        while True:
            row = next((x for x in manager()['status']().get('requests', []) if x['id'] == picked), row)
            active = row['state'] not in ('completed', 'cancelled', 'failed', 'timed_out', 'unknown')
            action = ui.pick('Request ' + row['id'][:12], [('refresh', 'Refresh'), ('cancel', 'Cancel request'), ('back', 'Back')],
                             context=request_lines(row), disabled={} if active else {'cancel': 'This request is not active.'})
            if action in (None, 'back'):
                break
            if action == 'cancel':
                try:
                    cancel_request(row['id'], ui)
                except (OSError, ValueError) as exc:
                    page(ui, 'Cancellation unavailable', [str(exc)])


def save_machine(value):
    m = manager()
    value = m['settings'](value)
    view = m['status']()
    if view['manager'] in ('running', 'blocked'):
        m['rpc']('settings', value=value)
    elif view['manager'] == 'stale':
        raise ValueError('Resolve the stale manager before changing runtime settings')
    else:
        m['private_directory'](m['root']())
        m['write_json'](m['root']() / 'settings.json', value)


def machine_menu(ui, provider=None):
    original = manager()['settings']()
    draft = json.loads(json.dumps(original))
    while True:
        if provider:
            config = draft['providers'][provider]
            rows = [('mode', 'Run using'), ('url', 'Local server URL'), ('save', 'Review and save'),
                    ('reset', 'Reset to defaults'), ('discard', 'Discard edits'), ('back', 'Back')]
            values = {'mode': config['mode'], 'url': config['url']}
        else:
            rows = [('memory', 'Memory budget'), ('idle', 'Idle cache lifetime'), ('save', 'Review and save'),
                    ('reset', 'Reset to defaults'), ('discard', 'Discard edits'), ('back', 'Back')]
            values = {'memory': str(draft['memory_percent']) + '% of RAM', 'idle': str(draft['idle_seconds'] // 60) + ' minutes'}
        action = ui.pick('Provider runtime' if provider else 'Machine settings', rows, values=values,
                         context=['Changes apply when local sessions have finished.'], accents={'save': 'g'},
                         disabled={'save': 'No pending changes.'} if draft == original else {})
        if action in (None, 'back'):
            if draft != original:
                choice = ui.pick('Unsaved machine settings', [('keep', 'Keep editing'), ('discard', 'Discard edits')])
                if choice != 'discard':
                    continue
            return
        if action == 'discard':
            draft = json.loads(json.dumps(original))
        elif action == 'reset':
            defaults = manager()['settings']({})
            if provider:
                draft['providers'][provider] = defaults['providers'][provider]
            else:
                draft.update(memory_percent=defaults['memory_percent'], idle_seconds=defaults['idle_seconds'])
        elif action == 'memory':
            value = ui.pick('Memory budget', [(str(n), str(n) + '% of RAM') for n in (60, 70, 80)])
            if value:
                draft['memory_percent'] = int(value)
        elif action == 'idle':
            value = ui.pick('Idle cache lifetime', [(str(n), str(n // 60) + ' minutes') for n in (60, 300, 900)])
            if value:
                draft['idle_seconds'] = int(value)
        elif action == 'mode':
            value = ui.pick('Run using', [('desktop', 'Desktop app'), ('headless', 'Command line · no app window')])
            if value:
                draft['providers'][provider]['mode'] = value
        elif action == 'url':
            value = ui.cooked_input('Local server URL (blank keeps current) → ', prefill=draft['providers'][provider]['url'])
            if value:
                draft['providers'][provider]['url'] = value
        elif action == 'save':
            if ui.pick('Review machine settings', [('save', 'Save settings'), ('back', 'Keep editing')],
                       context=([provider, 'Run using   ' + draft['providers'][provider]['mode'],
                                 'Server      ' + draft['providers'][provider]['url']] if provider else
                                 ['Memory budget   ' + str(draft['memory_percent']) + '% of RAM',
                                  'Idle lifetime   ' + str(draft['idle_seconds'] // 60) + ' minutes'])) == 'save':
                try:
                    save_machine(draft)
                    original = json.loads(json.dumps(draft))
                except (OSError, ValueError) as exc:
                    page(ui, 'Settings not saved', [str(exc)])


def provider_menu(ui, provider):
    apps = helper('t_local_apps')
    info = apps['provider_info'](provider)
    while True:
        action = ui.pick(info['name'], [('guide', 'CLI guide'), ('open', 'Open desktop app'),
                         ('runtime', 'Configure runtime'), ('back', 'Back')])
        if action in (None, 'back'):
            return
        if action == 'runtime':
            machine_menu(ui, provider)
        elif action == 'open':
            try:
                apps['open_desktop'](provider)
                page(ui, 'Desktop app opened', [info['name'],
                     'The app may use a different server from t’s private runtime. Use t’s monitor for t’s requests.'])
            except ValueError as exc:
                choice = ui.pick('App unavailable', [('help', 'Installation instructions'), ('retry', 'Try again'), ('back', 'Back')],
                                 context=[str(exc)])
                if choice == 'help':
                    webbrowser.open(info['download'])
                elif choice == 'retry':
                    try:
                        apps['open_desktop'](provider)
                    except ValueError as retry:
                        page(ui, 'Could not open app', [str(retry)])
        else:
            guide = apps['cli_guide'](provider, manager()['status']().get('endpoints', {}).get(provider))
            selected = ui.pick(info['name'] + ' · CLI guide', [(str(i), x['label']) for i, x in enumerate(guide)] + [('back', 'Back')])
            if selected in (None, 'back'):
                continue
            item = guide[int(selected)]
            while True:
                choice = ui.pick(item['label'], [('copy', 'Copy command'), ('docs', 'Official CLI documentation'), ('back', 'Back')],
                                 context=[item['command'], item['description'], item['note'],
                                          'Runtime: ' + (item['endpoint'] or 'not running')])
                if choice in (None, 'back'):
                    break
                if choice == 'docs':
                    webbrowser.open(info['docs'])
                else:
                    copy_command(item['command'])


def copy_command(command):
    import shutil
    target = (['pbcopy'] if sys.platform == 'darwin' else ['clip'] if sys.platform == 'win32' else
              ['wl-copy'] if shutil.which('wl-copy') else ['xclip', '-selection', 'clipboard'] if shutil.which('xclip') else None)
    if not target:
        raise ValueError('Clipboard tool unavailable; copy the displayed command manually')
    subprocess.run(target, input=command, text=True, check=True, timeout=5)


def run_model_action(ui, operation, model):
    """Cancellable action page; a task is accepted before claiming inference readiness."""
    cancel = threading.Event()
    result = {}
    started = time.time()
    def run():
        try:
            result['value'] = operation(cancel)
        except Exception as exc:
            result['error'] = str(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while thread.is_alive():
        view = manager()['status']()
        rows = [x for x in view.get('requests', []) if getattr(cancel, 'client_id', None) and x.get('client') == cancel.client_id and x['created'] >= started]
        lines = [model.split('/')[-1], label(rows[-1]['state']) if rows else 'Preparing model action…']
        choice = ui.pick('Model test', [('refresh', 'Refresh progress'), ('cancel', 'Cancel this test')], context=lines)
        if choice in (None, 'cancel'):
            cancel.set()
            thread.join(timeout=35)
            if thread.is_alive():
                page(ui, 'Cancellation pending', ['The provider must confirm it stopped before another request runs.'])
                return None
    if result.get('error'):
        page(ui, 'Model action failed', [result['error'], 'Downloaded files and previous verification evidence are retained.'])
        return None
    value = result.get('value', {})
    page(ui, 'Model test complete', ['Ready · test response succeeded.' if value.get('ready') else
                                   'Speed measurement saved.' if value.get('median') else 'Files downloaded.'])
    return value


def monitor_lines(view):
    return status_lines(view) + [''] + [x['id'][:10] + ' · ' + x['model'].split('/')[-1] + ' · ' + label(x['state'])
                                      for x in view.get('requests', [])[-12:]]


def monitor_cli(ui):
    focus = 0
    while True:
        lines = monitor_lines(manager()['status']())
        focus = min(focus, max(0, len(lines) - 1))
        display = [('  ' + ui.clip(text, ui.cols() - 4), i) for i, text in enumerate(lines)]
        ui.frame('Local model usage', display, focus, '↑↓ scroll · q back · refreshes every second')
        ready, _, _ = select.select([sys.stdin], [], [], 1)
        if ready:
            key = ui.read_key()
            if key in ('q', 'esc', 'abort'):
                return
            if key in ('down', 'j'):
                focus = min(len(lines) - 1, focus + 1)
            elif key in ('up', 'k'):
                focus = max(0, focus - 1)


DASHBOARD = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>t · Local model usage</title><style>body{background:#171d24;color:#e7edf4;font:15px/1.6 system-ui;max-width:1000px;margin:40px auto;padding:20px}h1{font-size:24px}h2{font-size:16px;color:#83cbd2}section{border:1px solid #3c4b5c;padding:20px;margin:16px 0;border-radius:8px}p{white-space:pre-wrap}small{color:#a5b1bf}</style>
<h1>Local model usage</h1><small>Read-only · refreshes every second · no prompts or responses retained</small><section><h2 id="state">Connecting…</h2><p id="models"></p></section><section><h2>Requests</h2><div id="requests"></div></section>
<script>const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);async function tick(){try{const r=await fetch('/status',{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw Error();const v=await r.json();document.querySelector('#state').textContent='Manager: '+v.manager;document.querySelector('#models').textContent=v.models.map(m=>m.model+' · '+m.provider+' · '+(m.memory_bytes==null?'memory unknown':(m.memory_bytes/1073741824).toFixed(1)+' GiB'+(m.estimated?' estimated':''))+' · '+(m.owned?'Loaded by t':'Loaded in provider app')).join('\\n')||'No loaded models reported.';const root=document.querySelector('#requests');root.replaceChildren();for(const x of v.requests){const p=document.createElement('p');p.textContent=(x.owner_label||'Local client')+' · '+x.model+' · '+x.kind+' · '+x.state+' · queued '+x.queue_seconds+'s · executing '+x.execution_seconds+'s';root.append(p)}}catch(e){document.querySelector('#state').textContent='Monitor disconnected'}setTimeout(tick,1000)}tick();</script>'''


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        host = '127.0.0.1:' + str(self.server.server_port)
        if self.headers.get('Host') != host or self.headers.get('Origin') not in (None, 'http://' + host):
            self.send_error(403)
            return
        if self.path == '/':
            body, content = DASHBOARD.encode(), 'text/html; charset=utf-8'
        elif self.path == '/status' and hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            body, content = json.dumps(manager()['status']()).encode(), 'application/json'
        else:
            self.send_error(403)
            return
        self.send_response(200)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def monitor_gui():
    with ThreadingHTTPServer(('127.0.0.1', 0), MonitorHandler) as server:
        server.token = secrets.token_urlsafe(32)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/#{server.token}'
        print('Local usage monitor · Ctrl-C closes this read-only view.')
        print('Local usage monitor: ' + url, flush=True)
        webbrowser.open(url)
        try:
            while thread.is_alive():
                thread.join(timeout=1)
        finally:
            server.shutdown()
            thread.join()


def hub(ui, api):
    focus = 'requests'
    while True:
        view = manager()['status']()
        choices = [('monitor', 'Monitor usage'), ('requests', 'Requests'), ('models', 'Models'),
                   ('provider', 'Configure provider & model'), ('machine', 'Machine settings'),
                   ('worker', 'Install / configure local worker'), ('back', 'Back to t config')]
        action = ui.pick('Local worker & models', choices, context=status_lines(view),
                         sections={'monitor': 'Inspect', 'provider': 'Configure'},
                         default=next((i for i, (k, _) in enumerate(choices) if k == focus), 0))
        if action in (None, 'back'):
            return
        focus = action
        try:
            if action == 'requests':
                requests_menu(ui)
            elif action == 'models':
                local = helper('t_local_inference')
                selected = local['select_model'](ui, use_label='Select for local worker')
                if selected and ui.pick('Review worker model', [('save', 'Save selection'), ('back', 'Keep current model')],
                                        context=[selected['model'], selected['provider']]) == 'save':
                    worker = helper('t_local_worker')
                    worker['save_settings'](dict(worker['settings'](), **selected))
            elif action == 'machine':
                machine_menu(ui)
            elif action == 'provider':
                provider = ui.pick('Configure provider & model', [('lmstudio', 'LM Studio'), ('ollama', 'Ollama'), ('back', 'Back')])
                if provider not in (None, 'back'):
                    provider_menu(ui, provider)
            elif action == 'worker':
                ui.restore()
                try:
                    helper('t_local_setup')['run'](SimpleNamespace(show=False, local_model=None, disable_local=False, yes=False), api)
                finally:
                    ui.raw()
            elif action == 'monitor':
                choice = ui.pick('Monitor model usage', [('cli', 'In this terminal'), ('gui', 'In a browser window'),
                                 ('lmstudio', 'LM Studio · CLI guide & app'), ('ollama', 'Ollama · CLI guide & app'), ('back', 'Back')])
                if choice == 'cli':
                    monitor_cli(ui)
                elif choice == 'gui':
                    ui.restore()
                    try:
                        monitor_gui()
                    except KeyboardInterrupt:
                        pass
                    finally:
                        ui.raw()
                elif choice in ('lmstudio', 'ollama'):
                    provider_menu(ui, choice)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            page(ui, 'Local action unavailable', [str(exc)])


def command(args, api):
    m = manager()
    resource, action = args.models_action, args.runtime_action
    ui = None
    try:
        if resource == 'runtime' and action == 'status':
            value = m['status']()
            lines = status_lines(value)
        elif resource == 'requests' and action in ('list', 'show'):
            value = m['status']().get('requests', [])
            if action == 'show':
                value = next((x for x in value if x['id'] == args.request_id), None)
                if not value:
                    raise ValueError('Unknown request ID')
                lines = request_lines(value)
            else:
                lines = [x['id'] + ' · ' + label(x['state']) + ' · ' + x['model'] for x in value] or ['No requests.']
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                if resource == 'requests' and action == 'cancel':
                    value = cancel_request(args.request_id)
                    print(json.dumps(value))
                    return 0
                raise ValueError('Open this action in a terminal')
            ui = api['_RailUI']()
            ui.raw()
            if resource == 'requests':
                value = cancel_request(args.request_id, ui)
                ui.restore()
                print(json.dumps(value))
            elif action == 'edit':
                machine_menu(ui)
            elif args.view == 'cli':
                monitor_cli(ui)
            else:
                ui.restore()
                monitor_gui()
            return 0
        print(json.dumps(value, indent=2) if getattr(args, 'json', False) else '\n'.join(lines))
        return 0
    finally:
        if ui:
            ui.restore()
