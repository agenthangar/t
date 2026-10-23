"""Display-only session metadata. Never upgrade a row's targeting id or activity."""
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata


def display_width(text):
    return sum(0 if unicodedata.category(char) in ('Mn', 'Me', 'Cf') else
               2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
               for char in text)


def clip_columns(text, width):
    if width <= 0:
        return ''
    if display_width(text) <= width:
        return text
    out, used = '', 0
    for char in text:
        size = display_width(char)
        if used + size > width - 1:
            break
        out += char
        used += size
    return out + '…'


def wrap_columns(text, width, continuation=''):
    lines, line, used = [], '', 0
    for char in text:
        size = display_width(char)
        if used + size > width and line:
            lines.append(line.rstrip())
            line, used = continuation, display_width(continuation)
        line += char
        used += size
    if line:
        lines.append(line.rstrip())
    return lines


def recorded_model(record):
    if not isinstance(record, dict):
        return None
    payload = record.get('payload') if record.get('type') == 'turn_context' else (
        record.get('message') if record.get('type') == 'assistant' else None)
    model = payload.get('model') if isinstance(payload, dict) else None
    return model if isinstance(model, str) and model and model != '<synthetic>' else None


def session_cells(row, glyph, remote=False):
    active = row.get('context') == 'active'
    attached = row.get('state') == 'attached' or (row.get('state') == 'app' and active)
    marks = ('●' if attached else '○') + (' ✓' if active else '')
    marks = {'saved': '· saved', 'archived': '□ archived', 'missing': '! missing'}.get(row.get('state'), marks)
    if re.search(r'#\d+ merged, still in progress', row.get('summary') or ''):
        marks += ' ↻'
    interface = row.get('interface') or ('desktop' if row.get('state') == 'app' else 'cli')
    ui = {'desktop': '▣ desktop', 'cli': '› cli', 'foreground': '› foreground', 'unknown': '? unknown'}[interface]
    cells = [marks, ui, f'{glyph} {row.get("model") or "-"}']
    if remote:
        cells.append(row.get('host', 'local'))
    return [*cells, row['slot'], row.get('summary') or '']


def transcript_model(path):
    """Last recorded model, bounded to the transcript tail; no configured guesses."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            offset = max(0, fh.tell() - 1024 * 1024)
            fh.seek(offset)
            data = fh.read()
        if offset:
            data = data.partition(b'\n')[2]
        for line in reversed(data.splitlines()):
            if b'"model"' not in line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    continue
                model = recorded_model(rec)
                if model:
                    return model
            except (ValueError, TypeError):
                continue
    except (OSError, ValueError, TypeError):
        pass
    return '-'


def _reverse_lines(fh):
    """Read newest records first without loading an entire rollout into memory."""
    offset = fh.seek(0, 2)
    pending = b''
    while offset:
        size = min(offset, 64 * 1024)
        offset -= size
        fh.seek(offset)
        lines = (fh.read(size) + pending).split(b'\n')
        pending = lines[0]
        yield from reversed(lines[1:])
    yield pending


def _turn_timestamp(value):
    try:
        stamp = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            return stamp.astimezone(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    return '-'


def transcript_last_turn(path, agent):
    """Actual turn starts, never file mtime, assistant output, or tool replies."""
    if agent not in ('codex', 'claude'):
        return '-'
    fallback = '-'
    try:
        with open(path, 'rb') as fh:
            for line in _reverse_lines(fh):
                if b'"timestamp"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                stamp = _turn_timestamp(record.get('timestamp'))
                if stamp == '-':
                    continue
                if agent == 'codex':
                    payload = record.get('payload')
                    if record.get('type') != 'event_msg' or not isinstance(payload, dict):
                        continue
                    if payload.get('type') == 'task_started':
                        return stamp
                    if payload.get('type') in ('task_complete', 'turn_aborted') and fallback != '-':
                        return fallback
                    if payload.get('type') == 'user_message':
                        # Older rollouts may only have user-message evidence.
                        # Prefer the explicit start immediately preceding it.
                        if fallback != '-':
                            return fallback
                        fallback = stamp
                elif record.get('type') == 'user' and not any(record.get(key) for key in
                        ('isMeta', 'isSidechain', 'isCompactSummary')):
                    message = record.get('message')
                    content = message.get('content') if isinstance(message, dict) else None
                    text, image = '', False
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        blocks = [block for block in content if isinstance(block, dict)]
                        if any(block.get('type') == 'tool_result' for block in blocks):
                            continue
                        text = ' '.join(block['text'] for block in blocks if block.get('type') == 'text'
                                        and isinstance(block.get('text'), str))
                        image = any(block.get('type') == 'image' for block in blocks)
                    if re.match(r'<(?:command-name|local-command-stdout|local-command-stderr|local-command-caveat|system-reminder)(?:>|\s)', text.lstrip()):
                        continue
                    if text.strip() or image:
                        return stamp
    except (OSError, ValueError, TypeError):
        pass
    return fallback


def format_last_turn(value):
    stamp = _turn_timestamp(value)
    if stamp == '-':
        return '-'
    return datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def enrich(lines, home, codex_home):
    db = None
    try:
        try:
            db = sqlite3.connect((Path(codex_home) / 'state_5.sqlite').resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=.2)
            columns = {r[1] for r in db.execute('pragma table_info(threads)')}
        except (OSError, sqlite3.Error):
            columns = set()
        out = []
        for line in lines:
            row = line.rstrip('\n').split('\t')
            if len(row) < 7:
                out.append(line.rstrip('\n'))
                continue
            sid, cwd, slot, state, context, summary, agent = row[:7]
            model = '-'
            # Optional private input from the row producer, using the exact same
            # verified transcript as its title. This is not an ownership claim.
            display_transcript = row[7] if len(row) > 7 and row[7] != '-' else None
            turn_transcript = display_transcript
            if agent == 'codex':
                record = None
                if {'id', 'cwd', 'name', 'title', 'source', 'archived', 'updated_at', 'rollout_path'} <= columns:
                    model_column = 'model' if 'model' in columns else 'NULL'
                    prompt_column = 'first_user_message' if 'first_user_message' in columns else 'NULL'
                    query = (f"select coalesce(nullif(name,''),nullif(title,''),{prompt_column},''), {model_column}, rollout_path "
                             "from threads where cwd in (?,?) and archived=0 "
                             "and instr(source, '\"subagent\"')=0")
                    args = [cwd, os.path.realpath(cwd)]
                    if sid != '-':
                        query += ' and id=?'
                        args.append(sid)
                    elif display_transcript:
                        query += ' and rollout_path=?'
                        args.append(display_transcript)
                    elif state != 'app':
                        query += ' and 0'  # Unknown live CLI ids must not borrow old context.
                    try:
                        record = db.execute(query + ' order by updated_at desc limit 1', args).fetchone()
                    except sqlite3.Error:
                        pass
                if record:
                    title, recorded_model, rollout = record
                    turn_transcript = display_transcript or rollout
                    model = recorded_model if isinstance(recorded_model, str) and recorded_model else transcript_model(rollout)
                    if summary.startswith('(Codex desktop workspace') or summary == '(no active session)':
                        summary = title or 'Untitled conversation'
                elif summary.startswith('(Codex desktop workspace'):
                    summary = 'No conversation recorded'
            elif agent == 'claude' and sid != '-':
                encoded = re.sub(r'[^A-Za-z0-9]', '-', cwd)
                transcript = Path(home) / '.claude/projects' / encoded / (sid + '.jsonl')
                model = transcript_model(transcript)
                turn_transcript = display_transcript or transcript
            if display_transcript:
                recorded = transcript_model(display_transcript)
                if recorded != '-':
                    model = recorded
            # Models and titles are metadata, never terminal control sequences.
            clean = lambda value: ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value)).split())
            row[5] = clean(summary)
            out.append('\t'.join(row[:7] + [clean(model), transcript_last_turn(turn_transcript, agent)]))
        return out
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    home = str(Path.home())
    for line in enrich(sys.stdin, home, os.environ.get('CODEX_HOME') or str(Path(home) / '.codex')):
        print(line)
