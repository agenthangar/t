"""Filter a private picker snapshot without rediscovery or session actions."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from t_session_details import clip_columns, display_width
from t_picker_theme import key_labels, paint

FACETS = {'agent': ('Ctrl-G', 'agent'), 'interface': ('Ctrl-D', 'UI'),
          'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo')}

OPEN_FACETS = {'agent': ('Ctrl-G', 'agent'), 'progress': ('Ctrl-P', 'progress'),
               'interface': ('Ctrl-D', 'UI'), 'model': ('Ctrl-B', 'model'),
               'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo'),
               'session': ('Ctrl-V', 'session'), 'context': ('Ctrl-E', 'context'),
               'task': ('Ctrl-W', 'task')}


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
                host='local' if host == 'here' else host, repo=repo)
        else:
            if len(fields) < 7:
                continue
            host, _, _, slot, state = fields[:5]
            repo = slot.partition(':')[0] if ':' in slot else slot.rpartition('-')[0] or slot
            result[(host, slot, state)] = dict(agent=fields[7] if len(fields) > 7 else 'claude',
                interface='desktop' if state == 'app' else 'cli', host=host, repo=repo)
            if profile == 'open':
                result[(host, slot, state)].update(
                    model=fields[8] if len(fields) > 8 and fields[8] not in ('', '-') else 'unknown',
                    session=slot, context=fields[5] or 'unknown', task=fields[6] or '(no task)',
                    progress='in progress' if re.search(r'#\d+ merged, still in progress', fields[6]) else 'not marked')
    return result


def choose(rows, records, filters):
    return [row for row in rows if all(value is None or
            records.get(tuple(row.split('\t')[:3]), {}).get(name, 'unknown') == value
            for name, value in filters.items())]


def pack(parts, width):
    lines = []
    for part in parts:
        part = clip_columns(part, width)
        if lines and display_width(lines[-1] + ' · ' + part) <= width:
            lines[-1] += ' · ' + part
        else:
            lines.append(part)
    return lines


def header(profile, mask, filters, hidden, columns, width):
    if profile == 'open':
        return open_header(mask, filters, hidden, columns, width)
    compact = width <= 40 or int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    controls = [f'Enter {profile} · Esc', 'Space/Tab ✓' if compact else 'Space/Tab mark ✓', 'Ctrl-/ details']
    state = lambda bit: 'shown' if mask & bit else 'hidden'
    toggles = ([f'Ctrl-L live: {state(1)}'] if profile == 'resume' else
               [f'Ctrl-T attached: {state(1)}'])
    if profile == 'kill':
        toggles.append(f'Ctrl-P pending: {state(2)}')
    facets = [f'{FACETS[name][0]} {FACETS[name][1]}' + (('*' if value is not None else '') if compact else f': {value or "all"}')
              for name, value in filters.items()]
    return air_header(controls, toggles + facets + ['Ctrl-A all', 'Ctrl-F defaults'],
                      f'{hidden} hidden', columns, width, compact, sum(value is not None for value in filters.values()))


def air_header(actions, controls, state, columns, width, compact, active=0):
    labels = lambda parts: [key_labels(part, compact=compact) for part in parts]
    lines = [paint(line, 'action') for line in pack(labels(actions), width)]
    if not compact:
        lines += ['', paint('FILTERS', 'heading')]
    lines += [paint(line) for line in pack(labels(controls), width)]
    state_parts = state.split(' · ')
    if compact:
        # Put the hidden count beside columns to keep view + active-filter count
        # readable without spending another row on the smallest terminals.
        if state_parts[0].startswith('View:'):
            hidden = state_parts[1]
            suffix = ' · ' + hidden
            columns = clip_columns(columns, max(1, width - display_width(suffix))) + suffix if columns else hidden
        state_parts = ([state_parts[0] + f' {active} filters'] if state_parts[0].startswith('View:') else
                       [state_parts[0], f'{active} filters'])
    lines += [paint(line, 'heading') for line in pack(state_parts, width)]
    if not compact:
        lines.append('')
    if columns:
        lines.append(paint(clip_columns(columns, width)))
    return '\n'.join(lines) + '\n'


def open_header(mask, filters, hidden, columns, width):
    compact = width <= 40 or int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    actions = ['Enter open/Esc · Space/Tab ✓'] if compact else ['Enter open · Esc', 'Space/Tab mark ✓']
    controls = ['Ctrl-T attached', 'Ctrl-U unattached', 'Ctrl-F defaults', 'Ctrl-A all']
    controls += [f'{key} {label}' for key, label in OPEN_FACETS.values()]
    view = {0: 'unattached', 1: 'all', 2: 'attached'}[mask]
    active = [f'{OPEN_FACETS[name][1]}={value}' for name, value in filters.items() if value is not None]
    state = f'View: {view} · {hidden} hidden'
    if active:
        state += (' · ' + str(len(active)) + ' filters') if compact else ' · ' + ' · '.join(active)
    return air_header(actions, controls, state, columns, width, compact, len(active))


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


def render(directory, profile, action, width, columns='', facet=None):
    directory = Path(directory)
    records = metadata((directory / 'source').read_text(), profile)
    names = list(OPEN_FACETS) if profile == 'open' else [
        name for name in FACETS if profile != 'resume' or name != 'interface']
    state_file = directory / 'facets.json'
    filters = json.loads(state_file.read_text()) if state_file.exists() else dict.fromkeys(names)
    mask_file = directory / 'show-all'
    mask = int(mask_file.read_text().strip() or 0) if mask_file.exists() else 0
    maximum = 3 if profile == 'kill' else 1
    if action in ('all', 'default'):
        mask = maximum if action == 'all' else 0
        filters = dict.fromkeys(names)
    elif action == 'first' and profile == 'open':
        mask = 2  # Attached-only; repeated presses are idempotent.
    elif action == 'unattached' and profile == 'open':
        mask = 0  # Switch attachment without resetting column choices.
    elif action == 'pick' and profile == 'open' and facet in names:
        chosen = pick_value(directory, facet, records, filters[facet])
        if chosen is not None:
            filters[facet] = chosen[0]
    elif action in ('first', 'second'):
        mask ^= 1 if action == 'first' else 2
    elif action in names:
        values = [None] + sorted({record.get(action, 'unknown') for record in records.values()})
        filters[action] = values[(values.index(filters[action]) + 1) % len(values)]
    elif action != 'current':
        raise ValueError('unknown picker filter: ' + action)
    if mask > (2 if profile == 'open' else maximum):
        raise ValueError('unsupported state filter')
    if profile == 'open' and mask == 2:
        unattached = set((directory / 'rows-0').read_text().splitlines())
        rows = [row for row in (directory / 'all').read_text().splitlines() if row not in unattached]
    else:
        rows = (directory / f'rows-{mask}').read_text().splitlines()
    selected = choose(rows, records, filters)
    total = len((directory / 'all').read_text().splitlines())
    (directory / 'current').write_text(''.join(row + '\n' for row in selected))
    (directory / 'current-header').write_text(header(profile, mask, filters, total - len(selected), columns, width))
    mask_file.write_text(str(mask))
    state_file.write_text(json.dumps(filters))


if __name__ == '__main__':
    render(os.environ['T_PICKER_VIEWS'], os.environ['T_PICKER_SESSION_FILTERS'], sys.argv[1],
           int(os.environ['T_PICKER_HEADER_WIDTH']), os.environ.get('T_PICKER_COLUMNS', ''),
           sys.argv[2] if len(sys.argv) > 2 else None)
