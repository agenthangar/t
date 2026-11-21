"""Render and filter a private picker snapshot without session actions."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from t_session_details import clip_columns, display_width, enrich, format_last_turn
from t_picker_theme import key_labels, paint, control_rows, sort_rows
from t_session_sort import RESUME_SORTS, SAVED_SORTS, natural, turn_epoch, latest_closed

FACETS = {'agent': ('Ctrl-G', 'agent'), 'interface': ('Ctrl-D', 'interface'),
          'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo')}

OPEN_FACETS = {'agent': ('Ctrl-G', 'agent'), 'progress': ('Ctrl-P', 'progress'),
               'interface': ('Ctrl-D', 'interface'), 'model': ('Ctrl-B', 'model'),
               'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo'),
               'session': ('Ctrl-V', 'session'), 'context': ('Ctrl-E', 'context'),
               'task': ('Ctrl-W', 'task')}

PR_STATES = {'MERGED', 'OPEN', 'CLOSED', 'NONE', 'UNKNOWN', 'MIXED'}
PR_COLORS = {'↴': '95', '○': '92', '◌': '37', '!': '33', '×': '91', '↗': '96', '?': '37'}
PR_CHIP = r'#\d+ [↴○◌!×?](?: ↗)?'


def metadata(source, profile):
    result = {}
    for line in source.splitlines():
        fields = line.split('\t')
        if profile == 'resume':
            if len(fields) < 10:
                continue
            repo, slot, sid, _, _, _, location, _, origin, agent = fields[:10]
            host = (origin if origin not in ('', '-') else 'local') if location == '-' else location
            result[tuple(fields[:3])] = dict(agent=agent or 'unknown',
                host='local' if host == 'here' else host, repo=repo, session=slot,
                turn=fields[10] if len(fields) > 11 else '-',
                pr=fields[12] if len(fields) > 12 and fields[12] in PR_STATES else 'UNKNOWN',
                closed=fields[13] if len(fields) > 14 and location == '-' else '-',
                work=len(fields) > 16 and fields[15] == 'yes')
        else:
            if len(fields) < 7:
                continue
            host, _, _, slot, state = fields[:5]
            repo = slot.partition(':')[0] if ':' in slot else slot.rpartition('-')[0] or slot
            result[(host, slot, state)] = dict(agent=fields[7] if len(fields) > 7 else 'claude',
                interface='desktop' if state == 'app' else 'cli', host=host, repo=repo, session=slot)
            if profile in ('list', 'open'):
                result[(host, slot, state)].update(session=slot, turn=fields[9] if len(fields) > 9 else '-')
            if profile == 'open':
                result[(host, slot, state)].update(
                    model=fields[8] if len(fields) > 8 and fields[8] not in ('', '-') else 'unknown',
                    session=slot, context=fields[5] or 'unknown', task=fields[6] or '(no task)',
                    progress='in progress' if re.search(r'#\d+ merged, still in progress', fields[6]) else 'not marked')
    return result



def sort_resume(rows, records, mode='turn'):
    def key(row):
        identity = tuple(row.split('\t')[:3])
        record = records.get(identity, {})
        primary = (-turn_epoch(record.get('closed', '-')),) if mode == 'closed' else () if mode == 'turn' else natural(record.get(mode, 'unknown'))
        return primary, -turn_epoch(record.get('turn', '-')), identity
    return sorted(rows, key=key)


def resume_turns(source, home, codex_home, owners=''):
    """Enrich one snapshot, honoring exact saved paths and local owner identities."""
    rows = [line.split('\t') for line in source.splitlines() if line]
    owner_ids = {}
    for line in owners.splitlines():
        owner = line.split('\t')
        if len(owner) >= 8 and owner[0] == 'local' and owner[1] != '-':
            owner_ids[(owner[2], owner[3])] = owner[1]
    inputs = []
    for row in rows:
        # A remote live owner must not borrow a local transcript's activity.
        local = row[6] in ('-', 'here', 'fg:here', 'app:here')
        sid = row[2]
        if sid == '-' and row[6] in ('here', 'app:here'):
            sid = owner_ids.get((row[3], row[0] + '-' + row[1]), '-')
        inputs.append('\t'.join([sid if local else '-', row[3], row[1],
            'detached', 'unknown', row[5], row[9], row[10] if len(row) > 10 else '-']))
    if os.environ.get('T_RESUME_CACHED') == '1':
        details = ['\t'.join(row[:7] + [row[11] if len(row) > 11 else '-']) for row in rows]
    else:
        details = enrich(inputs, home, codex_home)
    result = []
    for row, detail in zip(rows, details):
        turn = detail.split('\t')[-1]
        if row[7] == 'history' and len(row) > 11 and turn_epoch(row[11]) != float('-inf'):
            turn = row[11]
        row[4] = format_last_turn(turn, '%Y-%m-%d %H:%M')
        pr = row[13] if len(row) > 13 and row[13] in PR_STATES else 'UNKNOWN'
        result.append('\t'.join(row[:10] + [turn, row[12] if len(row) > 12 else '-', pr,
            row[14] if len(row) > 14 and row[6] == '-' else '-', row[15] if len(row) > 15 else '-',
            row[16] if len(row) > 16 else '-']))
    records = metadata('\n'.join(row + '\t' for row in result), 'resume')
    return sort_resume(result, records, 'closed' if any(r['closed'] != '-' for r in records.values()) else 'turn')


def choose(rows, records, filters):
    return [row for row in rows if all(value is None or
            records.get(tuple(row.split('\t')[:3]), {}).get(name, 'unknown') == value
            for name, value in filters.items())]


def pack(parts, width, separator=' · '):
    lines = []
    for part in parts:
        part = clip_columns(part, width)
        if lines and display_width(lines[-1] + separator + part) <= width:
            lines[-1] += separator + part
        else:
            lines.append(part)
    return lines


def state_label(profile, mask):
    if profile == 'open':
        return {0: 'unattached', 1: 'all attachment states', 2: 'attached'}[mask]
    if profile == 'resume':
        return 'live included' if mask else 'saved only'
    return ('attached included' if mask & 1 else 'unattached') + ', ' + (
        'pending included' if mask & 2 else 'clean only')


def header(profile, mask, filters, hidden, columns, width, sort='turn', enabled=True, color=False,
           hide_merged=False, sort_enabled=True, legend='', status=()):
    if profile == 'list':
        return list_header(filters, hidden, columns, width, sort, color)
    short = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    compact = width <= 40 or short
    tight_resume = profile == 'resume' and short and width <= 32
    key = lambda text: key_labels(text, compact=compact)
    style = lambda text, role='hint': paint(text, role) if color else text
    def keys(parts, selected=(), filled=False):
        return control_rows(parts, width, selected, compact, color, 'selected' if filled else 'active')
    resume = profile == 'resume'
    lines = list(status[:2] if tight_resume else status)
    if resume:
        actions = ['Enter/Esc' if compact else 'Enter resume', 'Tab ✓',
                   'Ctrl-/' if compact else 'Ctrl-/ details']
        if not compact:
            actions.insert(1, 'Esc cancel')
    else:
        actions = ['Enter/Esc' if compact else f'Enter {profile}/Esc', 'Tab✓',
                   'Ctrl-K kill' if profile == 'open' else 'Ctrl-/']
        if not compact:
            lines.append(style('HOTKEYS', 'heading'))
        lines += keys(actions)
    if not compact and lines:
        lines.append('')
    lines += keys(['Filters ' + ('ON' if enabled else 'OFF'), 'Ctrl-F' if compact else 'Ctrl-F toggle',
                   'Ctrl-X' if tight_resume else 'Ctrl-X reset'], ['Filters ON'] if enabled else [], filled=resume)
    controls = ([] if compact else ['Ctrl-T state']) + [f'{shortcut} {label}' for name, (shortcut, label) in FACETS.items()
                                  if profile != 'resume' or name != 'interface']
    active = [f'{FACETS[name][0]} {FACETS[name][1]}' for name, value in filters.items()
              if enabled and value is not None and name in FACETS]
    state_active = enabled and mask != (3 if profile == 'kill' else 1)
    if state_active:
        active.append('Ctrl-T state')
    if profile == 'resume':
        controls.append('Ctrl-D merged')
        if enabled and hide_merged:
            active.append('Ctrl-D merged')
    if tight_resume:
        controls = [part.split()[0] for part in controls]
        active = [part.split()[0] for part in active]
    lines += keys(controls, active)
    summary = ', '.join(f'{name}={value}' for name, value in filters.items() if value is not None)
    summary = state_label(profile, mask) + (' · ' + summary if summary else '')
    if compact:
        summary = key('Ctrl-T ') + summary
    suffix = f' · {hidden} hidden'
    summary = clip_columns(summary if enabled else 'Saved filters paused', width - display_width(suffix))
    if not tight_resume or len(lines) < 6:
        lines.append(style(summary, 'active' if compact and state_active else 'heading' if enabled else 'hint')
                     + style(suffix, 'heading' if enabled else 'hint'))
    if not compact:
        lines += [''] if resume else [style('Match highlighted row; repeat key to clear'), '']
    sort_lines = sort_rows(sort, width, sort_enabled, compact, color, profile in ('resume', 'open'),
                           include_closed=resume)
    if resume and short:
        sort_name = 'last closed' if sort == 'closed' else sort
        current = f'Sort: {sort_name}' if sort_enabled else 'Sort OFF'
        sort_lines = keys([current, 'Ctrl-S', 'Ctrl-Y'], [current] if sort_enabled else [], filled=True)
    lines += sort_lines
    if not compact:
        lines += ['']
    if legend:
        if short:
            legend = '↴ merged  ○ open'
        if not compact:
            lines.append(style('LEGEND', 'heading'))
        for line in legend.splitlines():
            line = clip_columns(line, width)
            lines.append(re.sub(r'[↴○◌!×↗?] [a-z]+',
                                lambda match: pr_color(match[0], match[0][0]), line) if color else line)
    if resume:
        lines += keys(actions)
        if columns and not compact:
            lines.append('')
    if columns:
        lines.append(style(clip_columns(columns, width), 'action' if resume else 'hint'))
    return '\n'.join(lines) + '\n'


def list_header(filters, hidden, columns, width, sort, color=False):
    compact = width <= 40 or int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    key = lambda text: key_labels(text, compact=compact)
    style = lambda text, role='hint': paint(text, role) if color else text
    actions = ['Enter/Esc' if compact else 'Enter open/Esc', 'Tab✓' if compact else 'Tab ✓', 'Ctrl-/' if compact else 'Ctrl-/ details']
    facets = [f'{shortcut} {label}' for shortcut, label in FACETS.values()]
    controls = ['Ctrl-A all', 'Ctrl-F reset'] + facets
    sorts = {name: f'{shortcut} {"turn" if compact and label == "last turn" else label}'
             for name, (shortcut, label) in RESUME_SORTS.items()}
    active = [f'{name}={value}' for name, value in filters.items() if value is not None]
    selected = {key(sorts[sort])}
    selected.update(key(f'{FACETS[name][0]} {FACETS[name][1]}')
                    for name, value in filters.items() if value is not None)
    if not active:
        selected.add(key('Ctrl-A all'))
    def keys(parts):
        return [' · '.join(style(part, 'heading' if part in ('FILTERS', 'SORT') else 'active' if part in selected else 'action')
                           for part in line.split(' · '))
                for line in pack([key(part) for part in parts], width)]
    lines = [] if compact else [style('HOTKEYS', 'heading')]
    lines += keys(actions)
    if not compact:
        lines += ['', style('FILTERS', 'heading')]
    lines += keys((['FILTERS'] if compact else []) + controls)
    suffix = f' · {hidden} hidden'
    summary = ' · '.join(active) if active else '0 filters'
    lines.append(style(clip_columns(summary, width - display_width(suffix)) + suffix, 'heading'))
    if not compact:
        lines += ['', style('SORT', 'heading')]
    lines += keys((['SORT'] if compact else []) + list(sorts.values()))
    label = 'turn' if compact and sort == 'turn' else RESUME_SORTS[sort][1]
    lines.append(style('Sort: ' + label, 'heading'))
    if not compact:
        lines.append('')
    if columns:
        lines.append(style(clip_columns(columns, width)))
    return '\n'.join(lines) + '\n'


def pick_menu(directory, choices, label, legend):
    options = directory / 'settings'
    options.write_text(''.join(f'{index}\t{text}\n' for index, text in enumerate(choices)))
    result = subprocess.run(['sh', '-c',
        '. "$1"; _t_fzf_single "choose" "$3" '
        '--delimiter="$(printf "\\t")" --with-nth=2 --prompt="$4 > " '
        '< "$2"', 'sh', str(Path(__file__).resolve().parents[1] / 'ui/fzf.sh'),
        str(options), legend, label], text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise RuntimeError('filter settings picker failed')
    return int(result.stdout.split('\t', 1)[0])


def settings(directory, profile, records, mask, filters, hide_merged=False):
    """Stage nested choices; only Apply commits, Esc keeps the original settings."""
    filters = dict(filters)
    names = list(filters)
    labels = OPEN_FACETS if profile == 'open' else FACETS
    while True:
        choices = ['Apply filters', 'State: ' + state_label(profile, mask)]
        choices += [labels[name][1] + ': ' + (filters[name] or 'all') for name in names]
        choices.append('Reset filters to defaults')
        chosen = pick_menu(directory, choices, 'Filters', 'Apply saves; Esc discards edits')
        if chosen is None:
            return None
        if chosen == 0:
            return mask, filters, hide_merged
        if chosen == len(choices) - 1:
            mask, filters = 0, dict.fromkeys(names)
            hide_merged = False
        elif chosen == 1:
            masks = [0, 2, 1] if profile == 'open' else list(range(4 if profile == 'kill' else 2))
            value = pick_menu(directory, [state_label(profile, value) for value in masks],
                              'State', 'Current: ' + state_label(profile, mask))
            if value is not None:
                mask = masks[value]
        else:
            name = names[chosen - 2]
            value = pick_value(directory, name, records, filters[name])
            if value is not None:
                filters[name] = value[0]


def pick_value(directory, name, records, current):
    """A shared single picker chooses an exact recorded value; Esc leaves it alone."""
    values = [None] + sorted({record[name] for record in records.values()})
    options = directory / 'values'
    options.write_text(''.join(f'{index}\t{value if value is not None else "All (clear filter)"}\n'
                               for index, value in enumerate(values)))
    label = OPEN_FACETS[name][1]
    legend = f'Current {label}: {current if current is not None else "all"}'
    if name == 'progress':
        legend += '\nIn progress = work continued after a merged PR; not marked is not proof of completion.'
    result = subprocess.run(['sh', '-c',
        '. "$1"; _t_fzf_single "apply filter" "$3" '
        '--delimiter="$(printf "\\t")" --with-nth=2 --prompt="$4 > " '
        '< "$2"',
        'sh', str(Path(__file__).resolve().parents[1] / 'ui/fzf.sh'), str(options), legend, label], text=True, stdout=subprocess.PIPE, check=False)
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise RuntimeError('column filter picker failed')
    return (values[int(result.stdout.split('\t', 1)[0])],)


def render(directory, profile, action, width, columns='', facet=None, identity=()):
    directory = Path(directory)
    records = metadata((directory / 'source').read_text(), profile)
    names = list(OPEN_FACETS) if profile == 'open' else [
        name for name in FACETS if profile != 'resume' or name != 'interface']
    state_file = directory / 'facets.json'
    filters = json.loads(state_file.read_text()) if state_file.exists() else dict.fromkeys(names)
    mask_file = directory / 'show-all'
    mask = int(mask_file.read_text().strip() or 0) if mask_file.exists() else 0
    default_sort = 'closed' if profile == 'resume' and any(r.get('closed', '-') != '-' for r in records.values()) else 'turn'
    sort_file = directory / 'sort'
    sort = sort_file.read_text().strip() if sort_file.exists() else (default_sort if profile in ('resume', 'list', 'open') else 'session')
    sort_enabled_file = directory / 'sort-enabled'
    sort_enabled = sort_enabled_file.read_text() == '1' if sort_enabled_file.exists() else profile in ('resume', 'list', 'open')
    enabled_file = directory / 'filters-enabled'
    enabled = enabled_file.read_text() == '1' if enabled_file.exists() else True
    merged_file = directory / 'hide-merged'
    hide_merged = merged_file.exists() and merged_file.read_text() == '1'
    maximum = 3 if profile == 'kill' else 1
    if action == 'toggle-filters':
        enabled = not enabled
    elif action == 'toggle-sort':
        sort_enabled = not sort_enabled
    elif action == 'next-sort':
        modes = [name for name in (SAVED_SORTS if profile == 'resume' else RESUME_SORTS) if profile in ('resume', 'list', 'open') or name != 'turn']
        sort = modes[(modes.index(sort) + 1) % len(modes)]
        sort_enabled = True
    elif action == 'state':
        mask = (mask + 1) % (3 if profile == 'open' else maximum + 1)
        enabled = True
    elif action.startswith('match-') and action[6:] in names:
        name = action[6:]
        if filters[name] is not None:
            filters[name] = None
        elif tuple(identity) in records:
            filters[name] = records[tuple(identity)].get(name, 'unknown')
        enabled = True
    elif action in ('off', 'on'):
        enabled = action == 'on'
    elif action == 'settings':
        chosen = settings(directory, profile, records, mask, filters, hide_merged)
        if chosen is not None:
            mask, filters, hide_merged = chosen
            enabled = True
    elif action in ('all', 'default', 'reset-filters'):
        mask = maximum if action == 'all' else 0
        filters = dict.fromkeys(names)
        hide_merged = False
        if action == 'reset-filters':
            enabled = True
    elif profile == 'resume' and action == 'merged':
        hide_merged = not (enabled and hide_merged)
        enabled = True
    elif action.startswith('sort-') and action[5:] in RESUME_SORTS:
        sort = action[5:]
        sort_enabled = True
    elif action == 'first' and profile == 'open':
        mask = 2  # Attached-only; repeated presses are idempotent.
    elif action == 'unattached' and profile == 'open':
        mask = 0  # Switch attachment without resetting column choices.
    elif action == 'pick' and facet in names:
        chosen = pick_value(directory, facet, records, filters[facet])
        if chosen is not None:
            filters[facet] = chosen[0]
    elif action in ('first', 'second'):
        mask ^= 1 if action == 'first' else 2
    elif action in names:
        if profile == 'list':
            if filters[action] is not None:
                filters[action] = None
            else:
                chosen = pick_value(directory, action, records, None)
                if chosen is not None:
                    filters[action] = chosen[0]
        else:
            values = [None] + sorted({record.get(action, 'unknown') for record in records.values()})
            filters[action] = values[(values.index(filters[action]) + 1) % len(values)]
    elif action != 'current':
        raise ValueError('unknown picker filter: ' + action)
    if mask > (2 if profile == 'open' else maximum):
        raise ValueError('unsupported state filter')
    if not enabled:
        rows = (directory / 'all').read_text().splitlines()
    elif profile == 'open' and mask == 2:
        unattached = set((directory / 'rows-0').read_text().splitlines())
        rows = [row for row in (directory / 'all').read_text().splitlines() if row not in unattached]
    else:
        rows = (directory / f'rows-{mask}').read_text().splitlines()
    rows = [row for row in rows if row]
    selected = choose(rows, records, filters) if enabled else rows
    if profile in ('resume', 'list'):
        if profile == 'resume' and enabled and hide_merged:
            selected = [row for row in selected if records.get(tuple(row.split('\t')[:3]), {}).get('pr') != 'MERGED']
        if action == 'default':
            sort = default_sort
        if sort_enabled:
            selected = sort_resume(selected, records, sort)
        merged_file.write_text('1' if hide_merged else '0')
    if profile not in ('resume', 'list') and sort_enabled:
        selected = sort_resume(selected, records, sort)
    sort_file.write_text(sort)
    sort_enabled_file.write_text('1' if sort_enabled else '0')
    total = sum(bool(row) for row in (directory / 'all').read_text().splitlines())
    if profile == 'resume':
        selected = mark_resume('\n'.join(selected), records).splitlines()
    (directory / 'current').write_text(''.join(row + '\n' for row in selected))
    status = []
    notice = directory / 'discovery-notice'
    color = os.environ.get('T_PICKER_COLOR') == '1'
    health = directory / 'discovery-status'
    freshness = json.loads(health.read_text()) if health.exists() and health.stat().st_size else None
    refresh = bool(os.environ.get('T_PICKER_REFRESH_CALLBACK'))
    if profile == 'resume' and (freshness is not None or refresh):
        status = resume_health(freshness or {}, width, color, refresh)
    elif notice.exists() and notice.read_text().strip():
        status.append(clip_columns(notice.read_text().strip(), width))
    legend = ''
    if profile == 'resume':
        if freshness is not None:
            hosts = freshness.get('auth_hosts', [])
        else:
            hosts = (directory / 'auth-hosts').read_text().splitlines() if (directory / 'auth-hosts').exists() else []
        hosts = [host for host in hosts if host]
        if hosts:
            summary = 'GitHub auth failed: ' + ', '.join(hosts)
            summary = clip_columns(summary, width)
            line = paint(summary, 'warning') if color else summary
            if freshness is None:
                status.insert(0, line)
            else:
                status.append(line)
        if (directory / 'columns').exists():
            columns = (directory / 'columns').read_text().rstrip('\n')
        if not selected:
            status.append('No matching conversations')
        if columns:
            latest, close_gutter, work_gutter = resume_gutters(records)
            columns = (('↶ ' if latest else '  ') if close_gutter else '') + ('  ' if work_gutter else '') + columns
            if os.environ.get('T_PICKER_LEGACY_COLUMNS') == '1':
                columns = '  ' + columns
        legend = resume_legend(width)
    controls = header(profile, mask, filters, total - len(selected), columns, width,
                      sort, enabled, color, hide_merged, sort_enabled, legend,
                      status if profile == 'resume' else ())
    (directory / 'current-header').write_text(('\n'.join(status) + '\n' if status and profile != 'resume' else '') + controls)
    enabled_file.write_text('1' if enabled else '0')
    mask_file.write_text(str(mask))
    state_file.write_text(json.dumps(filters))


def resume_legend(width):
    marks = '↻ WIP' if width < 64 else '↻ work in progress'
    prs = ['↴ merged', '○ open', '◌ draft', '! conflict', '× closed', '↗ preview', '? unknown']
    return '\n'.join([marks + '  ● live  — no PR', *pack(prs, width, '  ')])


def pr_color(text, symbol):
    if 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb':
        return text
    return f'\033[{PR_COLORS[symbol]}m{text}\033[0m'


def color_resume_prs(display):
    def chip(match):
        text, symbol = match[0], match[1]
        if text.endswith(' ↗'):
            return pr_color(text[:-2], symbol) + ' ' + pr_color('↗', '↗')
        return pr_color(text, symbol)
    return re.sub(rf'^(?:{PR_CHIP})(?: {PR_CHIP})?',
                  lambda match: re.sub(r'#\d+ ([↴○◌!×?])(?: ↗)?', chip, match[0]), display)


def resume_health(health, width, color, refresh=False):
    hosts = health.get('hosts', [])
    stamp = max((host.get('observed') or host.get('checked') or 0 for host in hosts), default=0)
    last = datetime.fromtimestamp(stamp) if stamp else None
    date = '%H:%M' if last and last.date() == datetime.now().date() else '%b %d %H:%M'
    timing = 'Last refresh ' + last.strftime(date) if last else 'No completed refresh'
    current = [host['host'] for host in hosts if host.get('status') == 'current']
    pending = [host['host'] for host in hosts if host.get('status') == 'refreshing']
    healthy = [timing] + ([', '.join(current) + ' current'] if current else [])
    if pending:
        healthy = ['Refreshing ' + ', '.join(pending), 'showing retained rows', *healthy]
    warnings = [host['host'] + ' ' + host['status'] for host in hosts
                if host.get('status') not in ('refreshing', 'current')]
    if health.get('unavailable'):
        warnings.append('Discovery unavailable')
    if health.get('cancelled'):
        warnings.append('Refresh cancelled')
    if health.get('failed'):
        warnings.append('Could not update rows')
    if width <= 28:
        time_label = timing.removeprefix('Last refresh ')
        if not pending:
            healthy = [time_label] + ([', '.join(current) + ' current'] if current else [])
        else:
            healthy = healthy[:1]
    if refresh:
        shortcut = key_labels('Ctrl-U' if width <= 28 else 'Ctrl-U refresh now', compact=width <= 40)
        healthy[0] = clip_columns(healthy[0], width - display_width(shortcut) - 2) + '  ' + shortcut
    if warnings:
        line = ' · '.join([*healthy, *warnings])
        if display_width(line) <= width:
            normal = ' · '.join(healthy) + ' · '
            warn = ' · '.join(warnings)
            lines = [normal + (paint(warn, 'warning') if color else warn)]
        else:
            lines = [clip_columns(' · '.join(healthy), width),
                     *(paint(part, 'warning') if color else part for part in pack(warnings, width))]
    else:
        lines = pack(healthy, width)
    if refresh and color:
        lines[0] = lines[0].replace(shortcut, paint(shortcut, 'primary'), 1)
    return lines


def resume_gutters(records):
    return (latest_closed(records), any(record['closed'] != '-' for record in records.values()),
            any(record.get('work') for record in records.values()))


def mark_resume(source, records=None):
    records = metadata(source, 'resume') if records is None else records
    latest, close_gutter, work_gutter = resume_gutters(records)
    color = os.environ.get('T_PICKER_COLOR') == '1'
    if not close_gutter and not work_gutter and not color:
        return source.rstrip('\n')
    result = []
    for line in source.splitlines():
        fields = line.split('\t')
        key = tuple(fields[:3])
        close = '↶' if key == latest else ' '
        work = '↻' if records.get(key, {}).get('work') else ' '
        prefix = ((paint(close, 'heading') if color and close != ' ' else close) + ' ' if close_gutter else '')
        if work_gutter:
            prefix += (paint(work, 'warning') if color and work != ' ' else work) + ' '
        fields[-1] = prefix + (color_resume_prs(fields[-1]) if color else fields[-1])
        result.append('\t'.join(fields))
    return '\n'.join(result)


if __name__ == '__main__':
    if sys.argv[1] == '--resume-mark':
        print(mark_resume(sys.stdin.read()))
        sys.exit(0)
    if sys.argv[1] == '--resume-turns':
        print('\n'.join(resume_turns(sys.stdin.read(), os.environ['HOME'],
            os.environ.get('CODEX_HOME', str(Path.home() / '.codex')),
            os.environ.get('T_RESUME_OWNER_ROWS', ''))))
        sys.exit(0)
    render(os.environ['T_PICKER_VIEWS'], os.environ['T_PICKER_SESSION_FILTERS'], sys.argv[1],
           int(os.environ['T_PICKER_HEADER_WIDTH']), os.environ.get('T_PICKER_COLUMNS', ''),
           sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3:6])
