"""Redact values even when PTY reads split them across event boundaries."""
import re

def redact_events(events, replacements):
    joined = ''.join(row[2] for row in events)
    pattern = re.compile('|'.join(re.escape(value) for value in sorted(replacements, key=len, reverse=True)))
    matches = list(pattern.finditer(joined))
    result = []
    offset = 0
    for timestamp, kind, text in events:
        end = offset + len(text)
        cursor = offset
        pieces = []
        for match in matches:
            if match.end() <= offset or match.start() >= end:
                continue
            pieces.append(joined[cursor:max(cursor, match.start())])
            if match.start() >= offset:
                pieces.append(replacements[match.group()])
            cursor = min(end, match.end())
        pieces.append(joined[cursor:end])
        result.append([timestamp, kind, ''.join(pieces)])
        offset = end
    return result
