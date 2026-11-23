"""Render and filter a private picker snapshot without session actions."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from t_session_details import clip_columns, display_width, enrich, format_last_turn, wrap_columns
from t_picker_theme import (PICKER_BREAK_PRIORITY, control_rows, key_labels, paint,
                            picker_filter_disclosure, picker_filter_keys, picker_filter_summary,
                            picker_section_rows, picker_sort_keys, picker_sort_summary, sort_rows)
from t_session_sort import RESUME_SORTS, SAVED_SORTS, natural, turn_epoch, latest_closed, sort_label

FACETS = {'agent': ('Ctrl-G', 'agent'), 'interface': ('Ctrl-D', 'interface'),
          'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo')}

OPEN_FACETS = {'agent': ('Ctrl-G', 'agent'), 'progress': ('Ctrl-P', 'progress'),
               'interface': ('Ctrl-D', 'interface'), 'model': ('Ctrl-B', 'model'),
               'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo'),
               'session': ('Ctrl-V', 'session'), 'context': ('Ctrl-E', 'context'),
               'task': ('Ctrl-W', 'task')}

PR_STATES = {'MERGED', 'OPEN', 'CLOSED', 'NONE', 'UNKNOWN', 'MIXED'}
PR_COLORS = {'↴': '95', '○': '92', '◌': '37', '!': '33', '×': '91', '↗': '96',
             '?': '37', '—': '37'}
PR_CHIP = r'[↴○◌!×?] #\d+'
RESUME_STATUS_WIDTH = 8
RESUME_MARK_ROLES = {'●': 'primary', '✓': 'primary', '↶': 'heading', '↻': 'warning', '↗': 'progress'}
RESUME_AGENTS = {'codex': ('>_', 'agent-codex'), 'claude': ('*', 'agent-claude'),
                 'cursor': ('<>', 'key')}


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
                work=len(fields) > 16 and fields[15] == 'yes',
                preview=len(fields) > 17 and fields[16] == 'yes')
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
           hide_merged=False, sort_enabled=True, show_legend=False, status=(),
           filters_expanded=False, hidden_info=False, attached_results=False, legend=''):
    if profile == 'list':
        return list_header(filters, hidden, columns, width, sort, color)
    short = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    compact = width <= 40 or short
    if profile == 'resume':
        return resume_header(mask, filters, hidden, columns, width, sort, enabled, color,
                             hide_merged, sort_enabled, show_legend, status, compact, short,
                             filters_expanded, hidden_info, attached_results)
    key = lambda text: key_labels(text, compact=compact)
    style = lambda text, role='hint': paint(text, role) if color else text
    def keys(parts, selected=()):
        return control_rows(parts, width, selected, compact, color, 'selected' if profile == 'open' else 'active')
    actions = ['Enter/Esc' if compact else f'Enter {profile}/Esc', 'Tab ✓',
               'Ctrl-K kill' if profile == 'open' else 'Ctrl-/']
    if profile == 'open' and os.environ.get('T_PICKER_NEW_SESSION') == '1':
        actions.append('Ctrl-N new')
    hotkeys = keys(actions)
    filter_rows = keys(['Filters ' + ('ON' if enabled else 'OFF'), 'Ctrl-F' if compact else 'Ctrl-F toggle',
                   'Ctrl-X reset'], ['Filters ON'] if enabled else [])
    controls = ([] if compact else ['Ctrl-T state']) + [f'{shortcut} {label}' for shortcut, label in FACETS.values()]
    active = [f'{FACETS[name][0]} {FACETS[name][1]}' for name, value in filters.items()
              if enabled and value is not None and name in FACETS]
    state_active = enabled and mask != (3 if profile == 'kill' else 1)
    if state_active:
        active.append('Ctrl-T state')
    filter_rows += keys(controls, active)
    summary = ', '.join(f'{name}={value}' for name, value in filters.items() if value is not None)
    summary = state_label(profile, mask) + (' · ' + summary if summary else '')
    if compact:
        summary = key('Ctrl-T ') + summary
    suffix = f' · {hidden} hidden'
    summary = clip_columns(summary if enabled else 'Saved filters paused', width - display_width(suffix))
    filter_rows.append(style(summary, 'active' if compact and state_active else 'heading' if enabled else 'hint')
                 + style(suffix, 'heading' if enabled else 'hint'))
    if not compact and profile != 'open':
        filter_rows.append(style('Match highlighted row; repeat key to clear'))
    column_width = width + int(os.environ.get('T_PICKER_PAN_HEADER_PAD', '0')) if os.environ.get('T_PICKER_PAN') == '1' else width
    sections = {
        'status': list(status), 'filter': filter_rows,
        'sort': sort_rows(sort, width, sort_enabled, compact, color, profile == 'open',
                          selected_role='selected' if profile == 'open' else 'active'),
        'legend': [style(clip_columns(legend, width))] if legend else [],
        'actions': hotkeys, 'columns': [style(clip_columns(columns, column_width))] if columns else [],
    }
    breaks = PICKER_BREAK_PRIORITY if status else PICKER_BREAK_PRIORITY[1:]
    return '\n'.join(picker_section_rows(sections, short=compact, breaks=breaks)) + '\n'


def resume_controls(mask, filters, width, sort, enabled, color, hide_merged,
                    sort_enabled, compact, filters_expanded=False, platform=None):
    active = [] if mask else ['Ctrl-T Saved only']
    active += [f'{FACETS[name][0]} {name.title()}: {value}' for name, value in filters.items()
               if name in FACETS and value is not None]
    if hide_merged:
        active.append('Ctrl-D Merged PRs hidden')
    filter_rows = picker_filter_summary(active, enabled, width, compact, color, platform, inactive=['Saved + attached'])
    editor = picker_filter_disclosure(filters_expanded, width, compact, color, platform)
    state = 'state' if compact else ('show saved only' if mask else 'include attached')
    merged = 'merged' if compact else 'hide merged'
    facets = [f'Ctrl-T {state}', 'Ctrl-G agent', 'Ctrl-O host', 'Ctrl-R repo', f'Ctrl-D {merged}']
    editor_rows = picker_filter_keys(facets, enabled, width, compact, color, platform) if filters_expanded else []
    sort_rows = picker_sort_summary(sort_label(sort).capitalize() if sort_enabled else 'Snapshot order',
                                    sort_enabled, width, compact, color, platform)
    sort_keys = picker_sort_keys(sort_enabled, width, compact, color, platform)
    return {'filter': filter_rows, 'filter-disclosure': editor, 'filter-options': editor_rows,
            'sort': sort_rows, 'sort-options': sort_keys}


def resume_header(mask, filters, hidden, columns, width, sort, enabled, color,
                  hide_merged, sort_enabled, show_legend, status, compact, short,
                  filters_expanded=False, hidden_info=False, attached_results=False):
    style = lambda text, role='action': paint(text, role) if color else text
    controls = resume_controls(mask, filters, width, sort, enabled, color,
                               hide_merged, sort_enabled, compact, filters_expanded)
    legend = ([style('LEGEND', 'heading')] + resume_legend(width, short, color,
              attached=attached_results)) if show_legend else []
    actions = resume_action_rows(width, compact, color, attached_results)
    columns_rows = [style(clip_columns(columns, width))] if columns else []
    # fzf before 0.75 cannot extend its native result counter. Keep its badge on
    # the first header row, directly beneath the counter, including zero.
    badge = [] if hidden_info else [style(f'[{hidden} hidden]', 'warning' if hidden else 'hint')]
    # Empty-result and authentication/failure notices must survive compact
    # layouts; losing them hides the reason a saved session is unavailable.
    critical, health = [], []
    for line in status:
        plain = re.sub(r'\x1b\[[0-9;]*m', '', line)
        target = critical if (plain == 'No matching conversations' or
            plain.startswith('GitHub auth failed:') or '× ' in plain) else health
        target.append(line)
    # The terminal frame and focused results keep their space during live resize.
    # Blank separators and explanatory chrome yield before the primary controls.
    height = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24')
    frame = int(os.environ.get('T_PICKER_PAN_FRAME_ROWS') or ('2' if short else '8'))
    budget = max(3, height - frame - (1 if filters_expanded or (short and critical) else 3))
    sections = {
        'badge': badge, 'status': health[:1], 'alerts': critical, 'status-detail': health[1:],
        **controls, 'legend': [] if short else legend,
        'actions': actions, 'columns': columns_rows,
    }
    breaks = PICKER_BREAK_PRIORITY if status else PICKER_BREAK_PRIORITY[1:]
    return '\n'.join(picker_section_rows(sections, short=short, budget=budget, breaks=breaks)) + '\n'


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
    if os.environ.get('T_PICKER_PAN') == '1' and (directory / 'pan-size').exists():
        height, width = map(int, (directory / 'pan-size').read_text().split())
        os.environ['T_PICKER_HEADER_HEIGHT'] = str(height)
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
    expanded_file = directory / 'filters-expanded'
    filters_expanded = expanded_file.exists() and expanded_file.read_text() == '1'
    maximum = 3 if profile == 'kill' else 1
    if action == 'toggle-filters':
        enabled = not enabled
    elif profile == 'resume' and action in ('expand-filters', 'edit-filters'):
        filters_expanded = not filters_expanded
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
    elif action == 'search' and os.environ.get('T_PICKER_PAN') == '1':
        (directory / 'pan-query').write_text(facet or '')
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
    hidden = total - len(selected)
    attached_results = profile == 'resume' and any(len(row.split('\t')) > 6 and
        row.split('\t')[6] != '-' for row in selected)
    (directory / 'hidden-count').write_text(str(hidden))
    if os.environ.get('T_PICKER_PAN') == '1':
        from t_picker_viewport import search
        query_file = directory / 'pan-query'
        selected = search(selected, query_file.read_text() if query_file.exists() else '')
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
            if int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14:
                status = status[:1]
            status.append('No matching conversations')
        if columns:
            columns = f'{"STATUS":<{RESUME_STATUS_WIDTH}}' + columns
            if os.environ.get('T_PICKER_LEGACY_COLUMNS') == '1':
                columns = '  ' + columns
    if os.environ.get('T_PICKER_PAN') == '1':
        from t_picker_viewport import project
        pad = int(os.environ.get('T_PICKER_PAN_HEADER_PAD', '0'))
        # Legacy fzf's fixed two-cell header indent stays outside logical STATUS.
        table_columns = columns[pad:] if pad else columns
        selected, columns = project(directory, selected, table_columns, width - pad,
                                    fixed=RESUME_STATUS_WIDTH)
        columns = ' ' * pad + columns
        (directory / 'current').write_text(''.join(row + '\n' for row in selected))
    legend = ''
    if profile == 'open':
        legend = '● attached · ○ unattached · ✓ context · ↻ work in progress'
        if not selected:
            status.append('No matching sessions' + (' · Ctrl-N new' if os.environ.get('T_PICKER_NEW_SESSION') == '1' else ''))
        if os.environ.get('T_PICKER_NATIVE_FOOTER') == '1' and int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') > 14:
            (directory / 'footer').write_text('\n'.join(pack(legend.split(' · '), width)) + '\n')
            legend = ''
        elif int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14:
            legend = ''
    controls = header(profile, mask, filters, hidden, columns, width,
                      sort, enabled, color, hide_merged, sort_enabled, profile == 'resume',
                      status, filters_expanded,
                      (directory / 'info-command').exists() and (directory / 'info-command').read_text() == '1',
                      attached_results, legend)
    prefix = '\n'.join(status) + '\n' if status and profile == 'list' else ''
    (directory / 'current-header').write_text(prefix + controls)
    enabled_file.write_text('1' if enabled else '0')
    mask_file.write_text(str(mask))
    state_file.write_text(json.dumps(filters))
    if profile == 'resume':
        expanded_file.write_text('1' if filters_expanded else '0')


def resume_action_rows(width, compact, color, attached=False, platform=None):
    enter = 'Return' if (platform or sys.platform) == 'darwin' else 'Enter'
    details = key_labels('Ctrl-/', platform, compact)
    labels = [enter, 'Esc', 'Tab', details] if compact else [
        key_labels('Enter', platform), key_labels('Esc', platform), key_labels('Tab', platform), details]
    actions = list(labels) if compact else [f'{key} {action}' for key, action in zip(
        labels, ('resume/attach' if attached else 'resume', 'cancel', 'mark ✓', 'details'))]
    if os.environ.get('T_PICKER_RESUME_LAST') == '1':
        shortcut = key_labels('Ctrl-B', platform, compact)
        actions.append(f'{shortcut} ↶' if compact else f'{shortcut} resume last closed')
        if not compact and display_width('  '.join(actions)) > width:
            actions[-1] = f'{shortcut} last closed'
        labels.append(shortcut)
    lines = pack(actions, width, '  ')
    if not color:
        return lines
    def label(part):
        key = next((value for value in labels if part == value or part.startswith(value + ' ')), part)
        text, check, _ = part[len(key):].partition('✓')
        return paint(key, 'key') + paint(text, 'action') + (resume_mark(check) if check else '')
    return ['  '.join(label(part) for part in line.split('  ')) for line in lines]


def resume_mark(text):
    return paint(text, RESUME_MARK_ROLES[text[0]])


def resume_legend(width, short, color, *, attached=True):
    close = '↶ close' if short else '↶ last closed'
    work = '↻ WIP' if width < 64 or short else '↻ work in progress'
    status = (['● attached'] if attached else []) + [close, work, '↗ preview']
    prs = ['↴ merged', '○ open', '◌ draft', '! conflict', '× closed', '? unknown', '— no PR']
    agents = {glyph: (name.capitalize(), role) for name, (glyph, role) in RESUME_AGENTS.items()}
    if short:
        return [line for line in pack(status, width, '  ')] if not color else [
            '  '.join(resume_mark(part) for part in line.split('  ')) for line in pack(status, width, '  ')]
    groups = [('Status', status, resume_mark), ('PRs', prs, lambda entry: pr_color(entry, entry[0])),
              ('Agent', [f'{glyph} {name}' for glyph, (name, _) in agents.items()],
               lambda entry: paint(entry, agents[entry.split(' ', 1)[0]][1]))]
    lines = []
    for label, entries, style in groups:
        prefix = f'{label:<6}  '
        for i, line in enumerate(pack(entries, width - len(prefix), '  ')):
            formatted = '  '.join(style(part) for part in line.split('  ')) if color else line
            lines.append((prefix if not i else ' ' * len(prefix)) + formatted)
    return lines


def pr_color(text, symbol):
    if 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb':
        return text
    return f'\033[{PR_COLORS[symbol]}m{text}\033[0m' if symbol in PR_COLORS else text


def color_resume_prs(display):
    if display.startswith(('— ', '? ')):
        return pr_color(display[0], display[0]) + display[1:]
    leading = re.match(rf'^(?:{PR_CHIP})(?: +{PR_CHIP})?', display)
    if not leading:
        return display
    return re.sub(PR_CHIP, lambda chip: pr_color(chip[0], chip[0][0]), leading[0]) + display[leading.end():]


def resume_health(health, width, color, refresh=False):
    short = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    hosts = health.get('hosts', [])
    stamp = max((host.get('observed') or host.get('checked') or 0 for host in hosts), default=0)
    last = datetime.fromtimestamp(stamp) if stamp else None
    date = '%H:%M' if last and last.date() == datetime.now().date() else '%b %d %H:%M'
    timing = 'Last refresh ' + last.strftime(date) if last else 'No refresh yet'
    pending = [host['host'] for host in hosts if host.get('status') == 'refreshing']
    warnings = []
    if health.get('unavailable'):
        warnings.append('Discovery unavailable')
    if health.get('cancelled'):
        warnings.append('Refresh cancelled')
    if health.get('failed'):
        warnings.append('Update failed')
    style = lambda text, role: paint(text, role) if color else text
    title = 'Refreshing ' + ', '.join(pending) if pending else timing
    if width <= 28 and not pending:
        title = title.removeprefix('Last refresh ')
    title_role = 'progress' if pending else 'action'
    shortcut = ''
    if refresh:
        shortcut = key_labels('Ctrl-U' if width <= 40 else 'Ctrl-U refresh now', compact=width <= 40)
    title_width = width - (display_width(shortcut) + 2 if shortcut else 0)
    title = clip_columns(title, title_width)
    line = style(title, title_role)
    if shortcut:
        line += '  ' + style(shortcut, 'primary')
    neutral = []
    if pending and last:
        neutral.append(timing)
    status_rows = [style(text, 'action') for text in pack(neutral, width, '  ')]
    outcomes = []
    failed = []
    for host in hosts:
        name, status = host['host'], host.get('status', 'not checked')
        if status == 'current':
            plain = clip_columns('✓ ' + name, width)
            rendered = style('✓', 'primary') + style(plain[1:], 'action')
        elif status == 'refreshing':
            plain = clip_columns('↻ ' + name, width)
            rendered = style('↻', 'progress') + style(plain[1:], 'action')
        else:
            name = clip_columns(name, max(1, width - 4))
            reason = clip_columns(status, width - display_width(name) - 3)
            plain = '× ' + name + (' ' + reason if reason else '')
            rendered = (pr_color('×', '×') if color else '×') + style(' ' + name, 'action')
            rendered += style(' ' + reason, 'warning') if reason else ''
            failed.append((plain, rendered))
        outcomes.append((plain, rendered))
    for warning in warnings:
        plain = clip_columns('× ' + warning, width)
        rendered = (pr_color('×', '×') if color else '×') + style(plain[1:], 'warning')
        outcomes.append((plain, rendered))
        failed.append((plain, rendered))
    def outcome_rows(parts):
        rows, current_plain, current_styled = [], '', ''
        for plain, rendered in parts:
            if current_plain and display_width(current_plain + '  ' + plain) > width:
                rows.append(current_styled)
                current_plain, current_styled = '', ''
            current_plain += ('  ' if current_plain else '') + plain
            current_styled += ('  ' if current_styled else '') + rendered
        return rows + ([current_styled] if current_styled else [])
    host_rows = outcome_rows(outcomes)
    details = outcome_rows(failed or outcomes)[:1] if short else status_rows + host_rows
    return [line, *details]


def mark_resume(source, records=None):
    records = metadata(source, 'resume') if records is None else records
    latest = latest_closed(records)
    color = os.environ.get('T_PICKER_COLOR') == '1'
    result = []
    for line in source.splitlines():
        fields = line.split('\t')
        key = tuple(fields[:3])
        record = records.get(key, {})
        attached = len(fields) >= 10 and fields[6] != '-'
        marks = ('●' if attached else ' ', '↶' if key == latest else ' ',
                 '↻' if record.get('work') else ' ', '↗' if record.get('preview') else ' ')
        prefix = ''.join(resume_mark(mark) if color and mark != ' ' else mark for mark in marks)
        display = fields[-1]
        if color:
            glyph, role = RESUME_AGENTS.get(record.get('agent'), ('?', 'action'))
            if len(fields) >= 18:
                agent_cell = rf'(?<=  ){re.escape(fields[0])} +{re.escape(fields[1])} +({re.escape(glyph)})(?=  )'
                display = re.sub(agent_cell, lambda match: match[0][:-len(glyph)] + paint(glyph, role), display, count=1)
            display = color_resume_prs(display)
        fields[-1] = prefix + ' ' * (RESUME_STATUS_WIDTH - len(marks)) + display
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
