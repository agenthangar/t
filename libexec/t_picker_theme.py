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
    code = {'heading': '36', 'action': '37', 'hint': '90', 'active': '1;4;96'}[role]
    return '\033[' + code + 'm' + text + '\033[0m' if text else ''


def control_rows(parts, width, selected=(), compact=False, color=False):
    """Pack whole key labels with the same spacing in every session picker."""
    active = {key_labels(part, compact=compact) for part in selected}
    lines = []
    for part in parts:
        part = _DISPLAY['clip_columns'](key_labels(part, compact=compact), width)
        if lines and _DISPLAY['display_width'](lines[-1] + ' · ' + part) <= width:
            lines[-1] += ' · ' + part
        else:
            lines.append(part)
    return [' · '.join(paint(part, 'active' if part in active else 'action') if color else part
                      for part in line.split(' · ')) for line in lines]


def sort_rows(mode, width, enabled=True, compact=False, color=False, include_turn=True):
    """The open/resume sort block: current choice, then cycling and choices."""
    choices = {name: ('turn' if compact and name == 'turn' else label)
               for name, (_, label) in _SORT['RESUME_SORTS'].items()
               if (include_turn or name != 'turn') and (not compact or name in ('turn', 'repo', 'session'))}
    label = 'Sort: ' + (_SORT['sort_label'](mode, compact) if enabled else 'OFF')
    return (control_rows([label, 'Ctrl-S toggle'], width, [label] if enabled else [], compact, color)
            + control_rows(['Ctrl-Y next'] + ([] if compact else list(choices.values())), width,
                           [choices[mode]] if enabled and mode in choices and not compact else [], compact, color))


def chrome(text, title='', compact=False):
    width = max(16, int(os.environ.get('T_PICKER_HEADER_WIDTH') or '72'))
    lines = text.rstrip('\n').splitlines()
    title = 'HOTKEYS' if title == 'HISTORY' else title
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
        if active_sort and any(part in ('last turn', 'turn', 'repo', 'agent', 'host', 'session') for part in line.split(' · ')):
            result += control_rows(line.split(' · '), width, [active_sort], compact, True)
            continue
        role = 'action' if re.match(r'(Enter|↩)', line) else 'heading' if line.startswith(('View:', 'FILTERS')) else 'hint'
        result.extend(paint(part, role) for part in wrap_columns(key_labels(line, compact=compact), width))
    # The final blank header row separates chrome from results. Short screens
    # give this space back before losing any control or focused result.
    if not compact:
        result.append(' ')
    return '\n'.join(result) + '\n'


if __name__ == '__main__':
    compact = int(os.environ.get('T_PICKER_HEADER_HEIGHT') or '24') <= 14
    title = sys.argv[1] if len(sys.argv) > 1 else ''
    sys.stdout.write(chrome(sys.stdin.read(), title, compact))
