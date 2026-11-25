"""Air presentation shared by searchable pickers and inline menus.

Only displayed labels change by platform; fzf and raw input bindings stay literal.
"""
import os
import re
import sys

import runpy
from pathlib import Path

# bin/t loads helpers without changing global sys.path.
_DISPLAY = runpy.run_path(str(Path(__file__).with_name("t_session_details.py")))
wrap_columns = _DISPLAY['wrap_columns']
display_width = _DISPLAY['display_width']
_SORT = runpy.run_path(str(Path(__file__).with_name('t_session_sort.py')))


def key_labels(text, platform=None, compact=False):
    mac = (platform or sys.platform) == 'darwin'
    text = re.sub(r'Ctrl-([A-Za-z/])', lambda m: ('⌃' if mac else '^' if compact else 'Ctrl-') + m[1].upper(), text, flags=re.IGNORECASE)
    text = re.sub(r'Alt-([A-Za-z])', lambda m: ('⌥' if mac else 'M-' if compact else 'Alt-') + m[1].upper(), text, flags=re.IGNORECASE)
    if mac:
        for pattern, label in ((r'\b[Ee]nter\b', '↩' if compact else '↩ Return'),
                               (r'\b[Ee]sc\b', '⎋' if compact else '⎋ Esc'),
                               (r'\b[Tt]ab\b', '⇥' if compact else '⇥ Tab')):
            text = re.sub(pattern, label, text)
    return text


def paint(text, role='hint'):
    if 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb':
        return text
    code = {'heading': '36', 'action': '37', 'key': '1;97', 'hint': '90', 'active': '1;4;96',
            'selected': '1;4;97;48;5;23', 'current': '1;36', 'progress': '1;96', 'warning': '33', 'primary': '92',
            'agent-codex': '94', 'agent-claude': '38;5;215'}[role]
    return '\033[' + code + 'm' + text + '\033[0m' if text else ''


def control_rows(parts, width, selected=(), compact=False, color=False, selected_role='active', separator=' · ', platform=None):
    """Pack whole key labels with the same spacing in every session picker."""
    active = {key_labels(part, platform, compact) for part in selected}
    lines = []
    for part in parts:
        part = _DISPLAY['clip_columns'](key_labels(part, platform, compact), width)
        if lines and _DISPLAY['display_width'](separator.join([*lines[-1], part])) <= width:
            lines[-1].append(part)
        else:
            lines.append([part])
    return [separator.join(paint(part, selected_role if part in active else 'action') if color else part
                           for part in line) for line in lines]



def picker_summary(label, values, active, width, compact=False, color=False, platform=None):
    """Align the active label and wrap values using terminal cells."""
    label_width = max(16, display_width(label) + 2) if width >= 60 else 0
    prefix = label.ljust(label_width) if label_width else label + ' '
    text = prefix + '  '.join(key_labels(value, platform, compact) for value in values)
    rows, line = [], ''
    for word in re.findall(r'\S+\s*', text):
        if line and display_width(line + word.rstrip()) > width:
            rows.append(line.rstrip())
            line = ' ' * label_width
        if display_width(line + word.rstrip()) > width:
            pieces = wrap_columns(word.rstrip(), width - label_width)
            rows.extend(line + piece for piece in pieces[:-1])
            line = ' ' * label_width + pieces[-1]
        else:
            line += word
    if line:
        rows.append(line.rstrip())
    if not color:
        return rows
    result = []
    for index, row in enumerate(rows):
        if index == 0 and row.startswith(label):
            row = paint(label, 'selected' if active else 'hint') + row[len(label):]
        row = re.sub(r'(?:Ctrl-[A-Z/]|Alt-[A-Z]|[⌃⌥^][A-Z/]|M-[A-Z])',
                     lambda match: paint(match[0], 'heading'), row)
        result.append(row)
    return result


def picker_filter_summary(values, enabled, width, compact=False, color=False, platform=None, inactive=()):
    active = enabled and bool(values)
    label = ('Active filter' if len(values) == 1 else 'Active filters') if active else (
        'No active filters' if enabled else 'Filters paused')
    return picker_summary(label, values if active else inactive, active, width, compact, color, platform)


def picker_filter_disclosure(expanded, width, compact=False, color=False, platform=None):
    return control_rows(['Alt-F ' + ('Hide filters ▾' if expanded else 'Edit filters ▸')],
                        width, compact=compact, color=color, platform=platform)


def picker_filter_keys(facets, enabled, width, compact=False, color=False, platform=None):
    toggle = ('pause' if enabled else 'enable') + ('' if compact else ' filters')
    return control_rows(['Ctrl-F ' + toggle, *facets, 'Ctrl-X reset' + ('' if compact else ' filters')],
                        width, compact=compact, color=color, separator='  ', platform=platform)


def picker_sort_summary(value, enabled, width, compact=False, color=False, platform=None):
    return picker_summary('Active sort' if enabled else 'Sort paused', [value] if value else (),
                          enabled, width, compact, color, platform)


def picker_sort_keys(enabled, width, compact=False, color=False, platform=None):
    cycle = 'sort' if compact else 'Next sort'
    toggle = ('pause' if enabled else 'resume') if compact else ('Pause sort' if enabled else 'Resume sort')
    return control_rows(['Ctrl-Y ' + cycle, 'Ctrl-S ' + toggle],
                        width, compact=compact, color=color, separator='  ', platform=platform)


# Each break is inserted before the matching section. Remove them in this
# order before taking space from information or controls.
PICKER_BREAK_PRIORITY = ('filter', 'sort', 'legend', 'columns', 'actions')
PICKER_CONTENT_PRIORITY = ('legend', 'status-detail', 'filter', 'sort', 'filter-options',
                           'sort-options', 'status')


def picker_section_rows(sections, *, short=False, budget=None, breaks=PICKER_BREAK_PRIORITY,
                        optional=PICKER_CONTENT_PRIORITY):
    """Stack present sections, giving blank rows back before optional content."""
    groups = {name: lines for name, lines in sections.items() if lines}
    gaps = set() if short else set(breaks).intersection(groups)
    size = sum(map(len, groups.values())) + len(gaps)
    for name in breaks:
        if budget is None or size <= budget:
            break
        if name in gaps:
            gaps.remove(name)
            size -= 1
    if budget is not None:
        for name in optional:
            if size <= budget:
                break
            size -= len(groups.pop(name, ()))
    return [line for name, lines in groups.items() for line in ([''] if name in gaps else []) + lines]


def sort_rows(mode, width, enabled=True, compact=False, color=False, include_turn=True, include_closed=False, selected_role='active'):
    """The open/resume sort block: current choice, then cycling and choices."""
    choices = {name: ('turn' if compact and name == 'turn' else label)
               for name, (_, label) in (_SORT['SAVED_SORTS'] if include_closed else _SORT['RESUME_SORTS']).items()
               if (include_turn or name != 'turn') and (not compact or name in ('turn', 'repo', 'session'))}
    label = 'Sort: ' + (_SORT['sort_label'](mode, compact) if enabled else 'OFF')
    return (control_rows([label, 'Ctrl-S toggle'], width, [label] if enabled else [], compact, color,
                         selected_role='selected' if include_closed else selected_role)
            + control_rows(['Ctrl-Y next'] + ([] if compact else list(choices.values())), width,
                           [choices[mode]] if enabled and mode in choices and not compact else [], compact, color))


def chrome(text, title='', compact=False):
    width = max(16, int(os.environ.get('T_PICKER_HEADER_WIDTH') or '72'))
    lines = text.rstrip('\n').splitlines()
    history = title == 'HISTORY'
    title = 'HOTKEYS' if history else title
    result = [paint(title.upper(), 'heading')] if title and not compact else []
    active_sort = ''
    for line in lines:
        if compact and not line:
            continue
        if not line:
            result.append('')
            continue
        if line.startswith('Sort: '):
            label = line.split(' · ', 1)[0]
            active_sort = label.removeprefix('Sort: ').removesuffix(' ↓').removesuffix(' ↑')
            result += control_rows(line.split(' · '), width, [label] if active_sort != 'OFF' else [], compact, True)
            continue
        if active_sort and any(part in ('last closed', 'last turn', 'turn', 'repo', 'agent', 'host', 'session') for part in line.split(' · ')):
            result += control_rows(line.split(' · '), width, [active_sort], compact, True)
            continue
        role = 'action' if re.match(r'(Enter|↩)', line) else 'heading' if line.startswith(('View:', 'FILTERS')) else 'hint'
        result.extend(paint(part, role) for part in wrap_columns(key_labels(line, compact=compact), width))
    # The final blank header row separates chrome from results. Short screens
    # give this space back before losing any control or focused result.
    if not compact and not history:
        result.append(' ')
    return '\n'.join(result) + '\n'


if __name__ == '__main__':
    compact = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    title = sys.argv[1] if len(sys.argv) > 1 else ''
    sys.stdout.write(chrome(sys.stdin.read(), title, compact))
