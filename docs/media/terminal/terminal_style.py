"""ANSI styling plus editorial semantic accents for monochrome demo output."""
import re

BACKGROUND = '#141516'
PALETTE = {
    'black': BACKGROUND, 'red': '#f38b82', 'green': '#98cb92',
    'brown': '#f2a93b', 'yellow': '#f2a93b', 'blue': '#8faef0',
    'magenta': '#cba2dc', 'cyan': '#80ccd0', 'white': '#ebe7df',
    'brightblack': '#929a9b', 'brightred': '#ffaaa2',
    'brightgreen': '#b8e8ae', 'brightbrown': '#ffd17c',
    'brightyellow': '#ffd17c', 'brightblue': '#b8c9ff',
    'brightmagenta': '#e7baf2', 'brightcyan': '#aceef0', 'brightwhite': '#ffffff',
}

def ansi_color(value, default):
    if value in PALETTE:
        return PALETTE[value]
    if re.fullmatch(r'[0-9a-fA-F]{6}', value):
        return '#' + value
    return default


def line_accent(line):
    text = line.strip()
    if text.startswith(('- ', '• Failed')) or 'FAILED (failures=' in text:
        return PALETTE['red']
    if text.startswith('+ ') and 'lines (ctrl+' not in text:
        return PALETTE['green']
    if text == 'OK' or text.endswith(' ... ok') or 'all 5 tests pass' in text or 'now all 5 pass' in text:
        return PALETTE['green']
    if text.startswith(('$ ', '› ', '• Ran ', 'GPT-6.1-Sol', '[open]', '[done]')):
        return PALETTE['cyan']
    if text.startswith(('• Fixed', '✓ ', '⟳ ', '✂ ', '↑ ')):
        return PALETTE['yellow']
    return PALETTE['white']


def cell_style(cell, line):
    fg = ansi_color(cell.fg, line_accent(line))
    bg = ansi_color(cell.bg, BACKGROUND)
    if cell.reverse:
        fg, bg = bg, fg
    return fg, bg
