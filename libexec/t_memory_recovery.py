"""Explicit application quits; model admission remains owned by the scheduler."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

GIB = 1024**3
LIB = Path(__file__).resolve().parent
# Exclude terminal hosts conservatively, even when tmux detached from its parent.
PROTECTED = {'com.apple.Terminal', 'com.googlecode.iterm2', 'com.mitchellh.ghostty',
             'dev.warp.Warp-Stable', 'net.kovidgoyal.kitty', 'org.alacritty',
             'com.openai.chat', 'com.openai.codex', 'com.microsoft.VSCode',
             'com.todesktop.230313mzl4w4u92', 'ai.elementlabs.lmstudio', 'com.ollama.ollama'}
INVENTORY = '''ObjC.import('AppKit');
function run() {
 const apps = $.NSWorkspace.sharedWorkspace.runningApplications;
 const rows = [];
 for (let i=0;i<apps.count;i++) {
  const a=apps.objectAtIndex(i);
  if(a.activationPolicy!==0) continue;
  rows.push({pid:Number(a.processIdentifier),name:ObjC.unwrap(a.localizedName)||'',
             bundle:ObjC.unwrap(a.bundleIdentifier)||''});
 }
 return JSON.stringify(rows);
}'''
QUIT = '''ObjC.import('AppKit');
function run(argv) {
 const a=$.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(argv[0]));
 if(!a || a.isTerminated) return 'closed';
 return a.terminate ? 'requested' : 'refused';
}'''


def helper(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def processes():
    raw = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,uid=,rss=,lstart='], text=True, timeout=5)
    result = {}
    for line in raw.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and all(x.isdigit() for x in fields[:4]):
            pid, parent, uid, rss = map(int, fields[:4])
            result[pid] = dict(pid=pid, parent=parent, uid=uid, bytes=rss*1024, identity=fields[4])
    return result


def ancestors(table, pid):
    found = set()
    while pid in table and pid not in found:
        found.add(pid)
        pid = table[pid]['parent']
    return found


def running_apps():
    if sys.platform != 'darwin':
        raise ValueError('Application controls are available on macOS. Release memory using your operating system.')
    table = processes()
    protected = ancestors(table, os.getpid())
    raw = subprocess.check_output(['/usr/bin/osascript', '-l', 'JavaScript', '-e', INVENTORY], text=True, timeout=10)
    result = []
    for app in json.loads(raw):
        app['name'] = ''.join(c for c in app['name'] if c.isprintable())[:200]
        process = table.get(app['pid'])
        if (not process or process['uid'] != os.getuid() or app['pid'] in protected
                or not app['name'] or not app['bundle'] or app['bundle'] in PROTECTED
                or any(x in app['bundle'].lower() for x in ('terminal', 'iterm', 'lmstudio', 'ollama'))):
            continue
        estimate = sum(p['bytes'] for p in table.values() if p['uid'] == os.getuid()
                       and app['pid'] in ancestors(table, p['pid']))
        result.append(dict(app, identity=process['identity'], estimated_bytes=estimate))
    return sorted(result, key=lambda row: (-row['estimated_bytes'], row['name']))


def quit_app(app):
    """Revalidate identity immediately before a normal Cocoa terminate request."""
    current = next((row for row in running_apps() if row['pid'] == app['pid']), None)
    if not current or any(current[k] != app[k] for k in ('identity', 'bundle', 'name')):
        return 'No longer eligible'
    subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', QUIT, str(app['pid'])],
                   check=True, capture_output=True, text=True, timeout=10)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        process = processes().get(app['pid'])
        if not process or process['identity'] != app['identity']:
            return 'Quit'
        time.sleep(.1)
    return 'Still open'


def blocker(row):
    memory = row.get('memory') or {}
    short = memory.get('shortfall_bytes')
    label = 'Waiting for GPU memory' if memory.get('resource') == 'gpu' else 'Waiting for memory'
    if row.get('state') in ('timed_out', 'failed') or row.get('timeout_phase'):
        label = 'Timed out · waiting for memory'
    if short is not None:
        label += f' · {short/GIB:.1f} GiB short'
    return label


def details(row):
    lines = [blocker(row)]
    if row.get('created'):
        lines.append(f"Elapsed {max(0, int(time.time()-row['created']))}s")
    if row.get('deadline'):
        remaining = max(0, int(row['deadline']-time.time()))
        lines.append('Queue deadline · ' + (f'{remaining}s remaining' if remaining else 'expired · Retry required'))
    if row.get('memory'):
        lines.append(row['memory'].get('reason', 'Memory measurement unavailable'))
    return lines


def free_memory(ui, current, status=None):
    """Browsing does not cancel/requeue. Every quit requires reviewed app names."""
    status = status or helper('t_local_manager')['status']
    notice = []
    while True:
        row = current()
        view = status()
        released = [x for x in view.get('released_models', []) if x.get('released_at', 0) >= row.get('created', 0)]
        context = details(row) + ['Already unloaded: ' + (', '.join(x['model'] for x in released) or 'none')]
        context += notice
        try:
            apps = running_apps()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            apps = []
            context.append(str(exc))
        external = {x['provider'] for x in view.get('models', []) if not x.get('owned') and x.get('provider') in ('lmstudio', 'ollama')}
        choices = [('apps', 'Select apps…')] + [(p, 'Open '+helper('t_local_apps')['provider_info'](p)['name']+' model controls') for p in sorted(external)]
        choices += [('refresh', 'Remeasure'), ('back', 'Back')]
        action = ui.pick('Free memory', choices, context=context,
                         disabled={} if apps else {'apps': 'No eligible user applications.'}, back_label='back')
        if action in (None, 'back'):
            return
        if action in external:
            try:
                helper('t_local_apps')['open_desktop'](action)
            except (OSError, ValueError) as exc:
                notice = [str(exc)]
        if action != 'apps':
            continue
        selected = ui.pick('Select apps to quit', [(str(a['pid']), a['name']) for a in apps], multi=True,
                           values={str(a['pid']): f"{a['estimated_bytes']/GIB:.1f} GiB estimated" for a in apps},
                           context=['Nothing is selected initially. Space marks apps; Enter reviews.'], back_label='back')
        if not selected:
            continue
        reviewed = [a for a in apps if str(a['pid']) in selected]
        if not reviewed:
            continue
        if ui.pick('Quit selected apps?', [('quit', 'Quit selected apps'), ('back', 'Back')],
                   context=[a['name'] for a in reviewed]+['Normal quit requests; each app may show a save dialog.'], back_label='back') != 'quit':
            continue
        notice = []
        for app in reviewed:
            try:
                result = quit_app(app)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                result = 'Still open · '+str(exc)
            # Remeasure physical memory after each request; estimates never grant admission.
            available = helper('t_local_runtime')['available_memory']()
            notice.append(app['name']+' · '+result)
            notice.append('Available: '+(f'{available/GIB:.1f} GiB measured' if available is not None else 'measurement unavailable'))
            current()  # Refresh scheduler evidence independently of the app estimate.
