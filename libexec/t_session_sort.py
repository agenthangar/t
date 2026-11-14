"""Stable session sorting shared by live and saved conversation pickers."""
import datetime
import re

RESUME_SORTS = {'turn': ('Alt-T', 'last turn'), 'repo': ('Alt-R', 'repo'),
                'agent': ('Alt-G', 'agent'), 'host': ('Alt-O', 'host'),
                'session': ('Alt-V', 'session')}


def natural(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', value))


def turn_epoch(value):
    try:
        stamp = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.timestamp() if stamp.tzinfo else float('-inf')
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return float('-inf')



def sort_label(mode, compact=False):
    label = "turn" if compact and mode == "turn" else RESUME_SORTS[mode][1]
    return label + (" ↓" if mode == "turn" else " ↑")
