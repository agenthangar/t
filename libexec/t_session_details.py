"""Display-only session metadata. Never upgrade a row's targeting id or activity."""
import datetime
import json
import os
from pathlib import Path
import re
import runpy
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


def format_last_turn(value, date_format='%Y-%m-%d %H:%M:%S %Z'):
    stamp = _turn_timestamp(value)
    if stamp == '-':
        return '-'
    return datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone().strftime(date_format)


MODEL_STAGE_LABELS = {'planning': 'Plan', 'implementation': 'Build', 'review': 'Review'}


def _stage_spawn(meta):
    source = meta.get('subagent') if isinstance(meta, dict) else None
    spawn = source.get('thread_spawn') if isinstance(source, dict) else None
    return spawn if isinstance(spawn, dict) else {}


def _stage_name(spawn):
    path = spawn.get('agent_path')
    return path.rsplit('/', 1)[-1] if isinstance(path, str) else None


def _stage_candidates(db):
    candidates = {}
    try:
        rows = db.execute(
            "select id, source, rollout_path from threads where instr(source, 'thread_spawn')>0 "
            "order by updated_at desc, id"
        )
        for sid, source, path in rows:
            try:
                spawn = _stage_spawn(json.loads(source))
            except (TypeError, ValueError):
                continue
            parent, stage = spawn.get('parent_thread_id'), _stage_name(spawn)
            if isinstance(parent, str) and parent and stage in MODEL_STAGE_LABELS and path:
                candidates.setdefault(parent, []).append((sid, stage, path))
    except sqlite3.Error:
        pass
    return candidates


def _observed_stages(parent, candidates, usage_module):
    stages = {}
    for sid, stage, path in candidates:
        if stage in stages:
            continue
        meta, requests, _ = usage_module['transcript'](path, 'codex')
        spawn = _stage_spawn(meta.get('source'))
        if meta.get('id') != sid or spawn.get('parent_thread_id') != parent or _stage_name(spawn) != stage:
            continue
        for request in reversed(requests):
            model, usage = request.get('model'), request.get('usage')
            if isinstance(model, str) and model and usage and any(usage.values()):
                stages[stage] = model
                break
    return [{'stage': stage, 'model': stages[stage]} for stage in MODEL_STAGE_LABELS if stage in stages]


def _legacy_rollout(path, sid, cwd, *, read_title=True):
    """Validate an exact legacy rollout and read its bounded opening prompt."""
    try:
        with open(path, 'rb') as stream:
            header = json.loads(stream.readline(64 * 1024))
            payload = header.get('payload') or {}
            if (header.get('type') != 'session_meta' or payload.get('id') != sid or
                    os.path.realpath(payload.get('cwd', '')) != os.path.realpath(cwd) or
                    payload.get('source') not in ('cli', 'vscode') or
                    payload.get('thread_source') == 'subagent'):
                return None
            if not read_title:
                return ''
            for line in stream.read(1024 * 1024).splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                body = record.get('payload') or {}
                text = ''
                if record.get('type') == 'event_msg' and body.get('type') == 'user_message':
                    text = body.get('message') or ''
                elif record.get('type') == 'response_item' and body.get('role') == 'user':
                    content = body.get('content') or []
                    if isinstance(content, list):
                        text = ' '.join(block.get('text', '') for block in content
                                        if isinstance(block, dict) and block.get('type') in ('input_text', 'text'))
                if isinstance(text, str) and text.strip() and not text.lstrip().startswith('<'):
                    return text.strip().splitlines()[0]
            return ''
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def enrich(lines, home, codex_home, *, lightweight=False):
    import runpy
    lines = list(lines)
    identifiers = [parts[0] for line in lines if len(parts := line.rstrip('\n').split('\t')) >= 7]
    helper = runpy.run_path(str(Path(__file__).with_name('t_session_cache.py')))
    rollout_evidence = helper['request']('display-evidence', {'kind': 'rollout', 'sids': identifiers}).get('values', {})
    db = None
    legacy_paths = None
    def legacy_candidates(sid, supplied, indexed):
        nonlocal legacy_paths
        cached = rollout_evidence.get(sid, {}).get('text', '').strip()
        seen = set()
        for candidate in (supplied, indexed, cached):
            if candidate and str(candidate) not in seen:
                seen.add(str(candidate))
                yield Path(candidate)
        if legacy_paths is None:
            legacy_paths = {}
            for path in (Path(codex_home) / 'sessions').glob('*/*/*/rollout-*.jsonl'):
                legacy_paths.setdefault(path.stem[-36:], []).append(path)
        for candidate in legacy_paths.get(sid, []):
            if str(candidate) not in seen:
                yield candidate

    try:
        try:
            db = sqlite3.connect((Path(codex_home) / 'state_5.sqlite').resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=.2)
            columns = {r[1] for r in db.execute('pragma table_info(threads)')}
        except (OSError, sqlite3.Error):
            columns = set()
        stage_candidates = _stage_candidates(db) if not lightweight and {'id', 'source', 'rollout_path', 'updated_at'} <= columns else {}
        usage_module = None
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
                if {'id', 'cwd', 'rollout_path'} <= columns:
                    model_column = 'model' if 'model' in columns else 'NULL'
                    titles = [f"nullif({column}, '')" for column in ('name', 'title', 'first_user_message')
                              if column in columns]
                    title_column = "coalesce(" + ", ".join(titles + ["''"]) + ")" if titles else "''"
                    query = f"select {title_column}, {model_column}, rollout_path from threads where cwd in (?,?)"
                    if 'archived' in columns:
                        query += ' and archived=0'
                    if 'source' in columns:
                        query += """ and instr(coalesce(source,''), '"subagent"')=0"""
                    args = [cwd, os.path.realpath(cwd)]
                    if sid != '-':
                        query += ' and id=?'
                        args.append(sid)
                    elif display_transcript:
                        query += ' and rollout_path=?'
                        args.append(display_transcript)
                    elif state != 'app' or not {'updated_at', 'source', 'archived'} <= columns:
                        query += ' and 0'  # Unknown live CLI ids must not borrow old context.
                    try:
                        order = ' order by updated_at desc' if 'updated_at' in columns else ''
                        record = db.execute(query + order + ' limit 1', args).fetchone()
                    except sqlite3.Error:
                        pass
                if record:
                    title, recorded_model, rollout = record
                    turn_transcript = display_transcript or rollout
                    model = recorded_model if isinstance(recorded_model, str) and recorded_model else ('-' if lightweight else transcript_model(rollout))
                    if summary.startswith('(Codex desktop workspace') or summary == '(no active session)' or (lightweight and summary in ('', '-')):
                        summary = title or 'Untitled conversation'
                elif summary.startswith('(Codex desktop workspace'):
                    summary = 'No conversation recorded'
                if lightweight:
                    supplied = display_transcript
                    display_transcript = turn_transcript = None
                    if sid != '-':
                        for candidate in legacy_candidates(sid, supplied, record[2] if record else None):
                            title = _legacy_rollout(candidate, sid, cwd,
                                                    read_title=summary in ('', '-', 'Untitled conversation'))
                            if title is None:
                                continue
                            turn_transcript = candidate
                            recorded = transcript_model(candidate)
                            if recorded != '-':
                                model = recorded
                            if summary in ('', '-', 'Untitled conversation') and title:
                                summary = title
                            break
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
            usage = {}
            if turn_transcript and not lightweight:
                if usage_module is None:
                    usage_module = runpy.run_path(str(Path(__file__).with_name('t_usage.py')))
                candidates = usage_module['session_rows']([(turn_transcript, agent)])
                if candidates and candidates[0]['requests']:
                    usage = {k: candidates[0][k] for k in ('models', 'lead_model', 'local_cloud_percent', 'coverage')}
                if agent == 'codex' and candidates and sid in ('-', candidates[0]['sid']):
                    parent = candidates[0]['sid']
                    stages = _observed_stages(parent, stage_candidates.get(parent, ()), usage_module)
                    if stages:
                        usage['model_stages'] = stages
            watches = runpy.run_path(str(Path(__file__).with_name('t_watch.py')))['records'](sid)
            out.append('\t'.join(row[:7] + [clean(model), transcript_last_turn(turn_transcript, agent)] +
                                 ([json.dumps(usage)] if usage else []) +
                                 ([json.dumps(watches, separators=(',', ':'))] if watches else [])))
        return out
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    home = str(Path.home())
    for line in enrich(sys.stdin, home, os.environ.get('CODEX_HOME') or str(Path(home) / '.codex'),
                       lightweight='--lightweight' in sys.argv[1:]):
        print(line)


def usage_preview(row, width=100):
    """Compact usage summary for one recorded session, with full models on wrap."""
    width = max(20, width)
    safe = lambda value: ''.join(c if c.isprintable() else ' ' for c in str(value))
    meta = row.get('session_display') or {}
    slot = safe(meta.get('slot') or Path(row.get('cwd') or '').name or row.get('sid', '')[:8] or '—')
    task = safe(meta.get('summary') or 'Task unrecorded')
    marks = session_cells(dict(meta, slot=slot), '')[0] if meta.get('state') not in (None, 'unknown') else '—'
    lead = safe(row.get('lead_model') or '')
    models = list(dict.fromkeys(safe(v['model']) for v in row['models'] if v.get('model')))
    if lead:
        models = ['★ ' + lead] + [v for v in models if v != lead]
    glyph = {'codex': '⬡', 'claude': '✱', 'cursor': '◆'}.get(row.get('agent'), '·')
    share = row.get('local_cloud_percent')
    percent = '/'.join(map(str, share)) if share is not None else '—'
    last = format_last_turn(meta.get('last_turn_started_at'))
    last = last[11:16] if last != '-' else '—'
    def cells(values, widths):
        result = []
        for value, size in zip(values, widths):
            value = clip_columns(value, size)
            result.append(value + ' ' * max(0, size - display_width(value)))
        return '  '.join(result).rstrip()
    first = [max(6, display_width(marks)), min(12, max(7, display_width(slot)))]
    # Small previews retain the first three columns; optional fields stack below.
    first[1] = min(first[1], max(3, width - first[0] - 8))
    first.append(max(1, width - sum(first) - 4))
    lines = ['SESSION · ' + slot]
    if width >= 100:
        widths = [first[0], first[1], 18, 14, 4, max(10, width - first[0] - first[1] - 18 - 14 - 4 - 9 - 12), 9]
        lines += [cells(['STATUS', 'SESSION', 'WORKING ON', 'LOC/CLD TOKEN%', 'TOOL', 'MODELS', 'LAST TURN'], widths),
                  cells([marks, slot, task, percent, glyph, (models or ['—'])[0], last], widths)]
        # Never discard a model suffix or secondary model in a narrow model cell.
        model_column = sum(widths[:5]) + 10
        wrapped = [part for model in models for part in wrap_columns(model, widths[5])]
        lines[2] = cells([marks, slot, task, percent, glyph, (wrapped or ['—'])[0], last], widths)
        lines += [' ' * model_column + part for part in wrapped[1:]]
    else:
        lines += [cells(['STATUS', 'SESSION', 'WORKING ON'], first), cells([marks, slot, task], first)]
        lines += wrap_columns(glyph + ' ' + (', '.join(models) or '—'), width)
        lines += wrap_columns('LOC/CLD TOKEN% ' + percent + ' · Last ' + last, width)
    token = lambda place: f"{row['tokens'][place]:,}" if row['tokens'].get(place) is not None else 'unrecorded'
    lines += wrap_columns('Tokens  local ' + token('local') + ' / cloud ' + token('cloud'), width)
    missing = [p for p in ('local', 'cloud') if row['tokens'].get(p) is None]
    note = ('/'.join(missing).capitalize() + ' tokens unrecorded; percentage unavailable.' if missing else
            'Percentage of measured tokens; history may be partial.' if share is not None else 'Percentage unavailable: incomplete or zero-total usage.')
    lines += wrap_columns(note, width)
    # Exact task and slot remain available in the full report below the overview.
    lines += ['', 'Session: ' + slot, 'Working on: ' + task, 'Last turn: ' + format_last_turn(meta.get('last_turn_started_at'))]
    return [part for line in lines for part in (wrap_columns(line, width) or [''])]
