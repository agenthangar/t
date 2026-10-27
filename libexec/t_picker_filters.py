"""Filter a private picker snapshot without rediscovery or session actions."""
import json
import os
from pathlib import Path
import sys

from t_session_details import clip_columns, display_width

FACETS = {'agent': ('Ctrl-G', 'agent'), 'interface': ('Ctrl-D', 'UI'),
          'host': ('Ctrl-O', 'host'), 'repo': ('Ctrl-R', 'repo')}


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
    controls = [f'Enter {profile} · Esc', 'Space/Tab mark ✓']
    if width <= 36 and profile in ('open', 'kill'):
        controls = [f'Enter {profile}/Esc · Space/Tab ✓']
    if profile != 'open' and width > 36:
        controls.append('Ctrl-/ details')
    lines = pack(controls, width)
    state = lambda bit: 'shown' if mask & bit else 'hidden'
    toggles = ([f'Ctrl-L live: {state(1)}'] if profile == 'resume' else
               [f'Ctrl-T attached: {state(1)}'])
    if profile == 'kill':
        toggles.append(f'Ctrl-P pending: {state(2)}')
    lines += pack(toggles, width)
    lines += pack([f'{FACETS[name][0]} {FACETS[name][1]}: {value or "all"}'
                   for name, value in filters.items()], width)
    if width <= 36:
        lines += pack(['Ctrl-A all', 'Ctrl-F reset'], width)
        if columns:
            suffix = f' · {hidden} hidden'
            lines.append(clip_columns(columns, width - display_width(suffix)) + suffix)
        else:
            lines.append(f'{hidden} hidden')
    else:
        lines += pack(['Ctrl-A show all', 'Ctrl-F defaults', f'{hidden} hidden'], width)
        if columns:
            lines.append(clip_columns(columns, width))
    return '\n'.join(lines) + '\n'


def render(directory, profile, action, width, columns=''):
    directory = Path(directory)
    records = metadata((directory / 'source').read_text(), profile)
    names = [name for name in FACETS if profile != 'resume' or name != 'interface']
    state_file = directory / 'facets.json'
    filters = json.loads(state_file.read_text()) if state_file.exists() else dict.fromkeys(names)
    mask_file = directory / 'show-all'
    mask = int(mask_file.read_text().strip() or 0) if mask_file.exists() else 0
    maximum = 3 if profile == 'kill' else 1
    if action in ('all', 'default'):
        mask = maximum if action == 'all' else 0
        filters = dict.fromkeys(names)
    elif action in ('first', 'second'):
        mask ^= 1 if action == 'first' else 2
    elif action in names:
        values = [None] + sorted({record.get(action, 'unknown') for record in records.values()})
        filters[action] = values[(values.index(filters[action]) + 1) % len(values)]
    elif action != 'current':
        raise ValueError('unknown picker filter: ' + action)
    if mask > maximum:
        raise ValueError('unsupported state filter')
    rows = (directory / f'rows-{mask}').read_text().splitlines()
    selected = choose(rows, records, filters)
    total = len((directory / 'all').read_text().splitlines())
    (directory / 'current').write_text(''.join(row + '\n' for row in selected))
    (directory / 'current-header').write_text(header(profile, mask, filters, total - len(selected), columns, width))
    mask_file.write_text(str(mask))
    state_file.write_text(json.dumps(filters))


if __name__ == '__main__':
    render(os.environ['T_PICKER_VIEWS'], os.environ['T_PICKER_SESSION_FILTERS'], sys.argv[1],
           int(os.environ['T_PICKER_HEADER_WIDTH']), os.environ.get('T_PICKER_COLUMNS', ''))
