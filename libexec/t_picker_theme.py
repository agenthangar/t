"""Air presentation shared by searchable pickers and inline menus.

Only displayed labels change by platform; fzf and raw input bindings stay literal.
"""
import os
import re
import sys

import runpy
from pathlib import Path

# bin/t loads helpers without changing global sys.path.
wrap_columns = runpy.run_path(str(Path(__file__).with_name("t_session_details.py")))["wrap_columns"]


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
    code = {'heading': '36', 'action': '37', 'hint': '90'}[role]
    return '\033[' + code + 'm' + text + '\033[0m' if text else ''


def chrome(text, title='', compact=False):
    width = max(16, int(os.environ.get('T_PICKER_HEADER_WIDTH') or '72'))
    lines = text.rstrip('\n').splitlines()
    result = [paint(title.upper(), 'heading')] if title and not compact else []
    for line in lines:
        if compact and not line:
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
