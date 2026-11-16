"""Stable session sorting shared by live and saved conversation pickers."""
import datetime
import math
import re

RESUME_SORTS = {'turn': ('Alt-T', 'last turn'), 'repo': ('Alt-R', 'repo'),
                'agent': ('Alt-G', 'agent'), 'host': ('Alt-O', 'host'),
                'session': ('Alt-V', 'session')}

SAVED_SORTS = {'closed': ('', 'last closed'), **RESUME_SORTS}

def closed_epoch(row):
    """Only verified lifecycle closure is evidence; activity/mtime is not."""
    if row.get('owners'):
        return float('-inf')
    events = row.get('associations', [])
    def stamp(event):
        value = event.get('observed', 0)
        return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 253402300799 else float('-inf')
    closed = max((stamp(a) for a in events if a.get('evidence') == 'close'), default=float('-inf'))
    opened = max((stamp(a) for a in events if a.get('evidence') in ('launch', 'resume', 'handoff')), default=float('-inf'))
    return closed if closed > max(opened, turn_epoch(row.get('last_turn', '-'))) else float('-inf')


def closed_time(row):
    value = closed_epoch(row)
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat() if value != float('-inf') else '-'


def latest_closed(records):
    """Return one exact identity; ties never arbitrarily claim the latest."""
    values = [(turn_epoch(record.get('closed', '-')), identity) for identity, record in records.items()]
    newest = max((value for value, _ in values), default=float('-inf'))
    winners = [identity for value, identity in values if value == newest]
    return winners[0] if newest != float('-inf') and len(winners) == 1 else None


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
    label = "turn" if compact and mode == "turn" else SAVED_SORTS[mode][1]
    return label + (" ↓" if mode in ("turn", "closed") else " ↑")
