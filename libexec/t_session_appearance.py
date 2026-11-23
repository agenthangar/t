"""Workflow colors for a verified managed tmux pane."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import sys

ROOT = Path(__file__).resolve().parent.parent
OPTIONS = ('window-style', 'window-active-style')
COMMAND = '@t-appearance-command'
TOGGLE = '@t-appearance-toggle'
CLICK = '@t-appearance-click'
PRIORITY = '@t-appearance-priority'
PALETTE = '@t-appearance-palette'
SHORTCUT = '@t-appearance-shortcut'
STYLE = '@t-appearance-style'
STAGE = '@t-appearance-stage'
WORKFLOW_STATUS = '@t-appearance-workflow'
WORKFLOW_CONFIG = '@t-appearance-config'
KEY = 'a'
NOTE = 't: session appearance'
PRIORITY_KEY = 'P'
PRIORITY_NOTE = 't: toggle priority'
PRIORITY_LABEL = 'Shift-P'
STATUS_KEY = 'MouseUp1Status'
STATUS_NOTE = 't: status line priority'
STATUS_RANGE = 't-priority'
STATUS_LEFT = '#[push-default]#{T;=/#{status-left-length}:status-left}#[pop-default]'
STATUS_RIGHT = '#{T;=/#{status-right-length}:status-right}'
STATUS_IDENTITY = ('#[push-default]#{?#{@t-appearance-command},#[fg=#09232c#,bg=#9de6f5#,bold],}'
                   '#{?#{&&:#{@t-appearance-command},#{<:#{client_width},48}},'
                   ' #{=10:#{s/^dev-//:session_name}} ,'
                   '#{T;=/#{status-left-length}:status-left}}#[pop-default]')
LEGACY_BADGE = ('#[push-default]#{?#{@t-appearance-command},#{?#{@t-appearance-toggle},#[range=user|t-priority],}'
                '#{?#{==:#{@t-appearance-priority},1},#[reverse] Priority On #[default], Priority Off }'
                '#[range=right] ,}#[pop-default]')
LEGACY_CLICK_BADGE = ('#{?#{@t-appearance-command},#[push-default]'
                '#{?#{@t-appearance-toggle},#[range=user|t-priority],}'
                '#{?#{==:#{@t-appearance-priority},1},#[fg=#ffffff#,bg=#633f89#,bold] Priority On ,'
                '#[fg=#edf6fa#,bg=#203d4a#,bold] Priority Off }'
                '#[pop-default range=left] ,}')
LEGACY_PANE_BADGE = LEGACY_CLICK_BADGE.replace('#{@t-appearance-toggle}', '#{@t-appearance-click}')
LEGACY_HINT = ('#{?#{@t-appearance-shortcut},#[push-default fg=#09232c#,bg=#9de6f5]'
               ' #{s/^C-/Ctrl-/:prefix}#{?#{<:#{client_width},48},→, then }F7 '
               '#[pop-default] ,}')
STATUS_HINT = ('#{?#{@t-appearance-shortcut},#[push-default fg=#09232c#,bg=#9de6f5]'
               ' #{s/^C-/Ctrl-/:prefix}#{?#{<:#{client_width},48},→, then }#{@t-appearance-shortcut} '
               '#[pop-default] ,}')
STATUS_REMAINING = '#{e|-:#{client_width},#{e|+:#{w;T;=/#{status-left-length}:status-left},34}}'
STATUS_BUDGET = f'#{{?#{{<:{STATUS_REMAINING},#{{status-right-length}}}},{STATUS_REMAINING},#{{status-right-length}}}}'
STATUS_COMPACT_RIGHT = f'#{{?#{{>:{STATUS_BUDGET},0}},#{{T;=/{STATUS_BUDGET}:status-right}},}}'
LEGACY_RIGHT = f'#{{?#{{@t-appearance-command}},{STATUS_COMPACT_RIGHT},{STATUS_RIGHT}}}'

# Use a native window range for the remaining tabs and a separate mouse range for the action.
CURRENT_WINDOW = '#{T:window-status-current-format}'
MANAGED_CURRENT_WINDOW = ('#{?#{@t-appearance-command},#{?#{>:#{session_windows},1},#{window_index}*,},'
                          + CURRENT_WINDOW + '}')
STATUS_BADGE = ('#{?#{@t-appearance-command},#[push-default]'
                '#{?#{@t-appearance-click},#[range=user|t-priority],}'
                '#{?#{==:#{@t-appearance-priority},1},'
                '#[fg=#ffffff#,bg=#633f89#,bold][ ★ #[underscore]Clear priority#[nounderscore] ],'
                '#[fg=#edf6fa#,bg=#324e5a#,bold][ ☆ #[underscore]Mark priority#[nounderscore] ]}'
                '#[pop-default norange] ,}')

# Strip tmux's stock title and timestamp together before expanding custom fields.
OLD_RIGHT_CONTENT = '#{T:#{s/"##.=[0-9]+.pane_title." //:status-right}}'
CLOCK_RIGHT_CONTENT = OLD_RIGHT_CONTENT.replace('//:status-right', '//;s/ .d-.b-.y$//:status-right')
RIGHT_CONTENT = '#{T:#{s/"##.=[0-9]+.pane_title."( .H..M( .d-.b-.y)?)? *//:status-right}}'
RIGHT_LIMIT = ('#{?#{<:#{client_width},64},0,'
               '#{?#{&&:#{<:#{client_width},88},#{>:#{status-right-length},16}},16,#{status-right-length}}}')
MANAGED_RIGHT = f'#{{?#{{>:{RIGHT_LIMIT},0}},#{{=/{RIGHT_LIMIT}:{RIGHT_CONTENT}}},}}'
STATUS_TRAILING = (f'#{{?#{{@t-appearance-command}},#[fill=#203d4a fg=#b5c7ce bg=#203d4a]'
                   f'{MANAGED_RIGHT},{STATUS_RIGHT}}}')

IDENTITY_WIDTH = ('#{?#{<:#{client_width},48},#{w: #{=10:#{s/^dev-//:session_name}} },'
                  '#{w:#{T;=/#{status-left-length}:status-left}}}')
# Include the gap after the button, the truncation marker and the space after the title.
ACTION_WIDTH = '#{?#{==:#{@t-appearance-priority},1},23,22}'
TAB_SPACE = '#{?#{>:#{session_windows},1},14,0}'
TITLE_SPACE = (f'#{{e|-:#{{e|-:#{{e|-:#{{client_width}},{IDENTITY_WIDTH}}},{ACTION_WIDTH}}},'
               f'#{{e|+:{TAB_SPACE},#{{w:{MANAGED_RIGHT}}}}}}}')
SPINNER = 's/^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] +//'
SHORT_SESSION = '#{s/^dev-//:session_name}'
TITLE = ('#{?#{==:#{s/^.* [|] //:pane_title},' + SHORT_SESSION + '},'
         '#{' + SPINNER + ';s/ [|] [^|]*$//:pane_title},#{' + SPINNER + ':pane_title}}')
STATUS_TITLE = (f'#{{?#{{&&:#{{@t-appearance-command}},#{{>:{TITLE_SPACE},0}}}},'
                f'#[push-default norange fg=#edf6fa bg=#203d4a]#{{q/h:#{{=/{TITLE_SPACE}/…:{TITLE}}}}}'
                '#[pop-default] ,}')

LIGHT_THEME = ('#{?#{==:#{theme},light},1,#{?#{==:#{theme},dark},0,'
               '#{||:#{==:#{client_active_theme},light},#{==:#{client_theme},light}}}}')
STATUS_COLORS = {
    'identity': ('#eef1f6', '#273142'),
    'workflow': ('#f8fafc', '#202836'),
    'ink': ('#1b2534', '#eef2fa'),
    'muted': ('#4e5c70', '#b4bfd0'),
    'session': ('#dbe7ff', '#344d77'),
    'session_ink': ('#214783', '#d8e5ff'),
    'model': ('#006458', '#8ee2cd'),
    'active': ('#d5eee7', '#22423f'),
    'priority': ('#eee2f3', '#463653'),
    'priority_ink': ('#603475', '#f1d7ff'),
    'priority_on': ('#69417e', '#d1b0eb'),
    'priority_on_ink': ('#ffffff', '#30213e'),
}


def status_color(role):
    light, dark = STATUS_COLORS[role]
    return f'#{{?{LIGHT_THEME},{light},{dark}}}'


def status_style(fg, bg, attributes='', fill=False):
    background = status_color(bg)
    return (f'#[fg={status_color(fg)} bg={background}'
            + (f' fill={background}' if fill else '')
            + (f' {attributes}' if attributes else '') + ']')


COMPACT_STATUS = '#{<:#{client_width},48}'
STATUS_SESSION = f'#{{=12/…:{SHORT_SESSION}}}'
EXTRA_LEFT = '#{?#{==:#{T:status-left},[#{session_name}] },,#{T;=/#{status-left-length}:status-left}}'
PRIORITY_ON = '#{==:#{@t-appearance-priority},1}'
PRIORITY_ACTION = (
    '#{?#{@t-appearance-click},#[range=user|t-priority],#[norange]}'
    f'#{{?{PRIORITY_ON},{status_style("priority_on_ink", "priority_on", "bold")},'
    f'{status_style("priority_ink", "priority", "bold")}}}'
    f' #{{?{PRIORITY_ON},★,☆}} #{{?{COMPACT_STATUS},Priority,#{{?{PRIORITY_ON},Clear priority,Mark priority}}}} '
    '#[norange]'
)
ACTION_SPACE = f'#{{?{COMPACT_STATUS},15,21}}'
EXTRA_SPACE = f'#{{e|+:#{{w:{EXTRA_LEFT}}},#{{w:{MANAGED_RIGHT}}}}}'
OCCUPIED_SPACE = f'#{{e|+:#{{e|+:{ACTION_SPACE},{TAB_SPACE}}},#{{e|+:#{{w:{STATUS_SESSION}}},{EXTRA_SPACE}}}}}'
MANAGED_TITLE_SPACE = f'#{{e|-:#{{client_width}},{OCCUPIED_SPACE}}}'
MANAGED_LEADING = (
    status_style('session_ink', 'session', 'bold') + f' {STATUS_SESSION} '
    + status_style('ink', 'identity', 'bold')
    + f'#{{?#{{>:{MANAGED_TITLE_SPACE},0}}, #{{q/h:#{{=/{MANAGED_TITLE_SPACE}/…:{TITLE}}}}},}} '
    + EXTRA_LEFT
)
MANAGED_TRAILING = (
    '#[norange]' + status_style('muted', 'identity', 'nobold', fill=True)
    + f'#{{?{MANAGED_RIGHT},{MANAGED_RIGHT} ,}}' + PRIORITY_ACTION
)
STATUS_MODELS = (
    '#{?#{@t-appearance-command},#[align=left]'
    + status_style('ink', 'workflow', fill=True) + '#{E:@t-appearance-workflow},}'
)
STAGE_NAMES = {'planning': ('Plan', 'Plan'), 'implementation': ('Build', 'Build'),
               'review': ('Review', 'Rev'), 'delivery': ('Deliver', 'Ship')}


def helper(name):
    return runpy.run_path(str(ROOT / 'libexec' / (name + '.py')))


def get(tmux, pane, option, inherited=False):
    flags = '-Apqv' if inherited else '-pqv'
    return tmux('show-options', flags, '-t', pane, option).stdout.rstrip('\n')


def set_option(tmux, pane, option, value):
    args = ('-pu', '-t', pane, option) if value is None else ('-p', '-t', pane, option, value)
    if tmux('set-option', *args).returncode:
        raise ValueError('The pane style could not be updated.')


def load_option(tmux, pane, name):
    value = get(tmux, pane, name)
    return json.loads(value) if value else {}


def with_background(style, color):
    fields = [part for part in re.split(r'[ ,]+', style) if part and not part.startswith('bg=')]
    return ','.join([*fields, 'bg=' + color])


def apply(tmux, pane, color):
    """Change the pane's default background and restore its prior option values."""
    saved = load_option(tmux, pane, STYLE)
    if color == 'default' and not saved:
        return
    updates = {}
    before = {option: get(tmux, pane, option) or None for option in OPTIONS}
    try:
        for option, previous in before.items():
            entry = saved.get(option, {})
            still_owned = entry and previous == entry['applied']
            if color == 'default':
                if still_owned:
                    set_option(tmux, pane, option, entry['original'])
                continue
            original = entry['original'] if still_owned else previous
            base = entry['base'] if still_owned else get(tmux, pane, option, inherited=True)
            set_option(tmux, pane, option, with_background(base, color))
            updates[option] = {'original': original, 'base': base, 'applied': get(tmux, pane, option)}
        set_option(tmux, pane, STYLE, json.dumps(updates) if updates else None)
    except ValueError:
        for option, previous in before.items():
            set_option(tmux, pane, option, previous)
        raise


def verified_target(recovery, socket, pane):
    row = next((row for row in recovery['panes'](socket) if row['pane'] == pane), None)
    if row and row['session'].startswith('dev-') and row.get('pane_dead') == '0':
        if (ROOT / '.git').is_file() and not Path(row['cwd']).resolve().is_relative_to(ROOT):
            return None
        target = recovery['owner'](socket, row)
        if target and target.get('pid') and recovery['descendant'](target['pid'], row['pane_pid']):
            return target
    return None


def priority_path(target):
    sid, agent, cwd = (target[key] for key in ('sid', 'agent', 'cwd'))
    if agent == 'codex':
        transcript = helper('t_lifecycle')['selected_rollout'](sid)
        with transcript.open('rb') as stream:
            first = json.loads(stream.readline(1024 * 1024))
        if first.get('type') != 'session_meta' or first.get('payload', {}).get('id') != sid:
            raise ValueError('The selected transcript does not match this conversation.')
    else:
        root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        transcript = root / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', cwd) / (sid + '.jsonl')
    if not transcript.is_file() or transcript.is_symlink():
        raise ValueError('The conversation history is unavailable.')
    return transcript.parent / sid / 't-priority.log'


def read_priority(fd):
    os.lseek(fd, -min(8, os.fstat(fd).st_size), os.SEEK_END)
    return os.read(fd, 8).endswith(b'on\n')


def priority(path):
    if path.parent.is_symlink():
        raise ValueError('The conversation asset directory is unsafe.')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return read_priority(fd)
    finally:
        os.close(fd)


def save_priority(path, enabled):
    if path.parent.is_symlink():
        raise ValueError('The conversation asset directory is unsafe.')
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if read_priority(fd) != enabled:
            os.write(fd, b'on\n' if enabled else b'off\n')
            os.fsync(fd)
    finally:
        os.close(fd)


def workflow_for(target):
    workflow = helper('t_workflow')
    document, _ = workflow['load']()
    policy = workflow['resolve_context'](document, target['cwd'], target['agent'])['workflow']
    return workflow, policy


def model_name(model):
    """Use familiar family names for the configured Codex and Claude models."""
    codex = re.fullmatch(r'gpt-[\d.]+-(astra|sol|terra)', model)
    claude = re.match(r'claude-(opus|sonnet|haiku)(?:-|$)', model)
    if codex or claude:
        return (codex or claude)[1].title()
    return re.sub(r'[^a-zA-Z0-9._/+-]', '-', model) or 'Default'


def workflow_status(workflow, policy, tool, section, active_stage):
    waiting = section == 'session' and active_stage == 'waiting'
    selected = active_stage if section == 'workflow' else None
    rows = []
    if policy.get('enabled') and policy.get('inference', {}).get('mode') != 'local-only':
        main = workflow['main_session_settings'](policy, tool)
        if workflow['model_stages'](policy):
            keys = ['planning', 'implementation']
            if not workflow['pr_review_skipped'](policy):
                keys.append('review')
            keys.append('delivery')
            for key in keys:
                settings = main if key == 'delivery' else workflow['stage_settings'](policy, key, tool)
                rows.append((key, STAGE_NAMES[key], model_name(settings.get('model', '')), settings.get('effort', '')))

    if selected and selected not in {row[0] for row in rows}:
        label = re.sub(r'[^a-zA-Z0-9 _-]', '', selected).replace('-', ' ').title()
        rows.append((selected, (label[:18], label[:8]), '', ''))

    def render(compact):
        parts, length = [], 1
        gap = ' ' if compact else '   '
        for key, names, model, effort in rows:
            label = names[compact]
            model_limit = (5 if len(rows) >= 4 else 6) if compact else 16
            if model and len(model) > model_limit:
                model = model[:model_limit - 1] + '…'
            effort = (' ' + effort) if effort and not compact else ''
            marked = key == selected
            bg = 'active' if marked else 'workflow'
            emphasis = 'bold underscore' if marked else 'nobold nounderscore'
            part = status_style('ink', bg, emphasis) + label
            if model:
                part += ' ' + status_style('model', bg, 'bold') + model
            if effort:
                part += status_style('muted', bg, 'nobold') + effort
            parts.append(part + status_style('ink', 'workflow', 'nobold nounderscore'))
            length += len(label) + (len(model) + 1 if model else 0) + len(effort) + len(gap)
        suffix = ''
        if waiting:
            suffix = '#[align=right]' + status_style('ink', 'workflow', 'bold')
            suffix += f'#{{?#{{>=:#{{client_width}},{length + 9}}}, Waiting , Wait }}'
            length += 9
        return (' ' + gap.join(parts) if parts else '') + suffix, length

    wide, width = render(False)
    compact, _ = render(True)
    return f'#{{?#{{<:#{{client_width}},{max(64, width)}}},{compact},{wide}}}'


def paint_workflow(tmux, pane, workflow, policy, tool, section, stage):
    rendered = workflow_status(workflow, policy, tool, section, stage)
    if get(tmux, pane, WORKFLOW_STATUS) != rendered:
        set_option(tmux, pane, WORKFLOW_STATUS, rendered)


def stage_for(tmux, pane, target):
    current = load_option(tmux, pane, STAGE)
    if current.get('sid') == target['sid'] and current.get('owner_start') == target['start']:
        return current['section'], current['stage']
    return 'session', 'idle'


@contextmanager
def locked_pane(recovery, socket, pane):
    if not re.fullmatch(r'%\d+', pane):
        raise ValueError('Invalid tmux pane.')
    path = recovery['cache'](socket) / ('appearance-' + pane[1:] + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def paint_priority(tmux, pane, colors, enabled):
    apply(tmux, pane, colors['priority' if enabled else 'standard'])
    set_option(tmux, pane, PRIORITY, '1' if enabled else '0')


def apply_current(tmux, pane, enabled, section, stage, workflow, policy, preview=None):
    colors = {mode: workflow['stage_background'](policy, section, stage, mode)
              for mode in ('standard', 'priority')}
    if preview != (enabled, colors):
        paint_priority(tmux, pane, colors, enabled)
        set_option(tmux, pane, PALETTE, json.dumps(colors))


def change_priority(tmux, pane, target, path, enabled=None, preview=None):
    current = priority(path)
    enabled = not current if enabled is None else enabled
    section, stage = stage_for(tmux, pane, target)
    workflow, policy = workflow_for(target)
    save_priority(path, enabled)
    try:
        apply_current(tmux, pane, enabled, section, stage, workflow, policy, preview)
    except ValueError:
        save_priority(path, current)
        raise


def toggle_priority(recovery, socket, pane):
    with locked_pane(recovery, socket, pane):
        tmux = lambda *args: recovery['tmux'](socket, *args)
        # Preview the last verified stage's colors locally. Only fresh ownership
        # evidence selects the conversation whose priority record may change.
        colors = load_option(tmux, pane, PALETTE)
        previous = {}
        painted = {}
        preview = None
        try:
            if colors:
                previous = {key: get(tmux, pane, key) for key in (*OPTIONS, STYLE, PRIORITY)}
                enabled = previous[PRIORITY] != '1'
                paint_priority(tmux, pane, colors, enabled)
                painted = {key: get(tmux, pane, key) for key in previous}
                preview = (enabled, colors)
            target = verified_target(recovery, socket, pane)
            if not target:
                raise ValueError('The managed conversation in this pane could not be verified.')
            change_priority(tmux, pane, target, priority_path(target), preview=preview)
        except Exception:
            for key, value in previous.items():
                if not painted or get(tmux, pane, key) == painted[key]:
                    set_option(tmux, pane, key, value or None)
            raise


def refresh(recovery, socket, pane, sid=None, agent_pid=None):
    with locked_pane(recovery, socket, pane):
        target = verified_target(recovery, socket, pane)
        if not target or (sid is not None and (target['sid'], target['pid']) != (sid, agent_pid)):
            return
        tmux = lambda *args: recovery['tmux'](socket, *args)
        section, stage = stage_for(tmux, pane, target)
        workflow, policy = workflow_for(target)
        apply_current(tmux, pane, priority(priority_path(target)), section, stage, workflow, policy)
        paint_workflow(tmux, pane, workflow, policy, target.get('agent', 'codex'), section, stage)


def refresh_registered(recovery, socket):
    tmux = lambda *args: recovery['tmux'](socket, *args)
    config = str(helper('t_workflow')['config_path']().resolve())
    for row in recovery['panes'](socket):
        pane = row['pane']
        try:
            expected = [str(Path(__file__).resolve()), 'popup', socket, pane]
            if (get(tmux, pane, WORKFLOW_CONFIG) == config
                    and shlex.split(get(tmux, pane, COMMAND))[1:] == expected):
                refresh(recovery, socket, pane)
        except (OSError, ValueError):
            continue


def install_status_badge(tmux, session, pane):
    target = '=' + session + ':'
    primary, secondary = 'status-format[0]', 'status-format[1]'

    def current(option, inherited=True):
        return tmux('show-options', '-Aqv' if inherited else '-qv',
                    '-t', target, option).stdout.rstrip('\n')

    count, template, second = map(current, ('status', primary, secondary))
    if count != 'on' and not (count == '2' and second == STATUS_MODELS):
        return
    # User-defined second lines take precedence over the managed two-row layout.
    if second and second != STATUS_MODELS and (current(secondary, False) or not second.startswith('#[align=left]#{R:')):
        return
    updated = template
    if PRIORITY_ACTION not in template:
        old_trailing = f'#{{?#{{@t-appearance-command}},{MANAGED_RIGHT},{STATUS_RIGHT}}}'
        updated = updated.replace(CLOCK_RIGHT_CONTENT, RIGHT_CONTENT).replace(OLD_RIGHT_CONTENT, RIGHT_CONTENT)
        updated = updated.replace(old_trailing, STATUS_TRAILING, 1)
        updated = updated.replace(STATUS_IDENTITY + STATUS_BADGE + STATUS_TITLE, STATUS_LEFT, 1)
        updated = updated.replace(STATUS_TRAILING, STATUS_RIGHT, 1)
        updated = updated.replace(STATUS_IDENTITY, STATUS_LEFT, 1).replace(LEGACY_RIGHT, STATUS_RIGHT, 1)
        for old in (LEGACY_BADGE, LEGACY_CLICK_BADGE, LEGACY_PANE_BADGE, LEGACY_HINT, STATUS_HINT):
            updated = updated.replace(old, '', 1)
        if STATUS_LEFT not in updated or STATUS_RIGHT not in updated:
            return
        updated = updated.replace(STATUS_LEFT, f'#{{?#{{{COMMAND}}},{MANAGED_LEADING},{STATUS_LEFT}}}', 1)
        updated = updated.replace(STATUS_RIGHT, f'#{{?#{{{COMMAND}}},{MANAGED_TRAILING},{STATUS_RIGHT}}}', 1)
        if MANAGED_CURRENT_WINDOW not in updated:
            updated = updated.replace(CURRENT_WINDOW, MANAGED_CURRENT_WINDOW, 1)
    if (updated, second, count) != (template, STATUS_MODELS, '2'):
        # A local array shadows the global array. Roll back to inheritance if the
        # array didn't already exist and the second or third write fails.
        had_array = bool(current('status-format', False))
        changes = [(primary, updated), (secondary, STATUS_MODELS), ('status', '2')]
        before = {option: current(option, False) for option, _ in changes}
        written = []
        for option, value in changes:
            if tmux('set-option', '-t', target, option, value).returncode:
                for changed in reversed(written):
                    if before[changed]:
                        tmux('set-option', '-t', target, changed, before[changed])
                    else:
                        tmux('set-option', '-u', '-t', target, changed)
                if not had_array and primary in written:
                    tmux('set-option', '-u', '-t', target, 'status-format')
                return
            written.append(option)
    # Use mouse-up so existing mouse-down handlers keep their window and session actions.
    existing = tmux('list-keys', '-T', 'root', STATUS_KEY)
    clickable = False
    if not existing.stdout.strip() or STATUS_NOTE in tmux('list-keys', '-N', '-T', 'root', STATUS_KEY).stdout:
        selected = f'#{{&&:#{{==:#{{mouse_status_range}},{STATUS_RANGE}}},#{{!=:#{{{TOGGLE}}},}}}}'
        clickable = tmux('bind-key', '-N', STATUS_NOTE, '-T', 'root', STATUS_KEY, 'if-shell', '-F', selected,
                         "run-shell -b 'sh -c #{q:@t-appearance-toggle}'").returncode == 0
    if clickable or get(tmux, pane, CLICK):
        set_option(tmux, pane, CLICK, '1' if clickable else None)


def bind_prefix(tmux, key, note, condition, command, unavailable):
    existing = tmux('list-keys', '-T', 'prefix', key)
    if existing.stdout.strip() and note not in tmux('list-keys', '-N', '-T', 'prefix', key).stdout:
        return False
    return tmux('bind-key', '-N', note, '-T', 'prefix', key, 'if-shell', '-F', condition,
                'run-shell -b ' + shlex.quote(command),
                'display-message ' + shlex.quote(unavailable)).returncode == 0


def install(recovery, socket, session):
    row = next((row for row in recovery['panes'](socket) if row['session'] == session), None)
    if not row or not session.startswith('dev-') or row.get('pane_dead') != '0':
        return
    if (ROOT / '.git').is_file() and not Path(row['cwd']).resolve().is_relative_to(ROOT):
        return
    pane = row['pane']
    tmux = lambda *args: recovery['tmux'](socket, *args)
    # run-shell expands formats. display-popup's shell command alone does not.
    show = ('tmux -S #{q:socket_path} display-popup -E -B -c #{q:client_name} -w 72% -h 65% '
            '#{q:@t-appearance-command}')
    bind_prefix(tmux, KEY, NOTE, '#{!=:#{@t-appearance-command},}', show,
                'No t session appearance controls in this pane')
    shortcut = bind_prefix(tmux, PRIORITY_KEY, PRIORITY_NOTE, '#{!=:#{@t-appearance-toggle},}',
                           'sh -c #{q:@t-appearance-toggle}', 'No t Priority control in this pane')
    if shortcut:
        set_option(tmux, pane, SHORTCUT, PRIORITY_LABEL)
    elif get(tmux, pane, SHORTCUT):
        set_option(tmux, pane, SHORTCUT, None)
    callback = [sys.executable, str(Path(__file__).resolve()), 'popup', socket, pane]
    set_option(tmux, pane, COMMAND, shlex.join(callback))
    set_option(tmux, pane, TOGGLE, shlex.join([*callback[:2], 'toggle', *callback[3:]]))
    set_option(tmux, pane, WORKFLOW_CONFIG, str(helper('t_workflow')['config_path']().resolve()))
    install_status_badge(tmux, session, pane)
    tmux('run-shell', '-b', shlex.join([*callback[:2], 'refresh', socket, pane]))


def popup(recovery, socket, pane):
    target = verified_target(recovery, socket, pane)
    if not target:
        raise ValueError('The managed conversation in this pane could not be verified.')
    tmux = lambda *args: recovery['tmux'](socket, *args)
    path = priority_path(target)
    enabled = priority(path)
    section, stage = stage_for(tmux, pane, target)
    workflow, policy = workflow_for(target)
    cli = runpy.run_path(str(ROOT / 'bin' / 't'))
    stages = {('session', 'idle'): 'No active stage', ('session', 'waiting'): 'Waiting on you'}
    workflow_stages = dict(cli['_workflow_palette_stages'](workflow, policy))
    stages.update({('workflow', key): workflow_stages.pop(key, title)
                   for key, title in [('planning', 'Plan'), ('implementation', 'Build'),
                                      ('review', 'Review'), ('delivery', 'Deliver')]
                   if key != 'review' or not workflow['pr_review_skipped'](policy)})
    for key in policy.get('stage_backgrounds', {}).get('workflow', {}):
        if key != 'review' or not workflow['pr_review_skipped'](policy):
            workflow_stages.setdefault(key, key.replace('-', ' ').title())
    stages.update({('workflow', key): title for key, title in workflow_stages.items()
                   if key != 'review' or not workflow['pr_review_skipped'](policy)})
    ui = cli['_RailUI']()
    ui.raw()
    try:
        choice = ui.pick('Session appearance', [('priority', 'Clear priority' if enabled else 'Mark as priority'),
                         ('stage', 'Active stage ›'), ('back', 'Back to session')],
                         values={'priority': 'On' if enabled else 'Off', 'stage': stages.get((section, stage), stage)})
        if choice in (None, 'back'):
            return
        if choice == 'stage':
            picked = ui.pick('Active stage', list(stages.items()), default=list(stages).index((section, stage))
                             if (section, stage) in stages else 0)
            if picked is None:
                return
            section, stage = picked
        with locked_pane(recovery, socket, pane):
            if not recovery['current'](target):
                raise ValueError('The managed conversation changed while the controls were open.')
            if choice == 'priority':
                change_priority(tmux, pane, target, path, not enabled)
            else:
                workflow, policy = workflow_for(target)
                apply_current(tmux, pane, priority(path), section, stage, workflow, policy)
                set_option(tmux, pane, STAGE, json.dumps(dict(sid=target['sid'], owner_start=target['start'],
                                                             section=section, stage=stage)))
                paint_workflow(tmux, pane, workflow, policy, target['agent'], section, stage)
    finally:
        ui.restore()


if __name__ == '__main__':
    action, socket, *args = sys.argv[1:]
    actions = {'install': install, 'refresh': refresh, 'refresh-registered': refresh_registered,
               'popup': popup, 'toggle': toggle_priority}
    if action in actions:
        recovery = helper('t_recovery')
        if action == 'refresh-registered' and socket == 'default':
            socket = recovery['run'](['tmux', 'display-message', '-p', '#{socket_path}']).stdout.strip()
        if socket.startswith('/'):
            try:
                actions[action](recovery, socket, *args)
            except (OSError, ValueError, RuntimeError) as exc:
                if action in ('popup', 'toggle'):
                    recovery['tmux'](socket, 'display-message', '-t', args[0], 't: ' + str(exc))
