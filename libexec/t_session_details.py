"""Display-only session metadata. Never upgrade a row's targeting id or activity."""
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
                    model = recorded_model if isinstance(recorded_model, str) and recorded_model else transcript_model(rollout)
                    if summary.startswith('(Codex desktop workspace') or summary == '(no active session)':
                        summary = title or 'Untitled conversation'
                elif summary.startswith('(Codex desktop workspace'):
                    summary = 'No conversation recorded'
            elif agent == 'claude' and sid != '-':
                encoded = re.sub(r'[^A-Za-z0-9]', '-', cwd)
                model = transcript_model(Path(home) / '.claude/projects' / encoded / (sid + '.jsonl'))
            if display_transcript:
                recorded = transcript_model(display_transcript)
                if recorded != '-':
                    model = recorded
            # Models and titles are metadata, never terminal control sequences.
            clean = lambda value: ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value)).split())
            row[5] = clean(summary)
            out.append('\t'.join(row[:7] + [clean(model)]))
        return out
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    home = str(Path.home())
    for line in enrich(sys.stdin, home, os.environ.get('CODEX_HOME') or str(Path(home) / '.codex')):
        print(line)
