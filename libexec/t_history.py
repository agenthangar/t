"""Private, durable conversation history and guarded exact-conversation resume."""
from contextlib import closing, contextmanager, nullcontext
import ast
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import runpy
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]
_resolve_repo = runpy.run_path(str(Path(__file__).with_name("t_repos.py")))["resolve_repo"]

VERSION = 1
PARSER_VERSION = 6
_DISPLAY = runpy.run_path(str(Path(__file__).with_name('t_session_details.py')))
_RESUME_SOURCE = runpy.run_path(str(Path(__file__).with_name('t_resume_source.py')))
display_width = _DISPLAY['display_width']
clip_columns = _DISPLAY['clip_columns']
wrap_columns = _DISPLAY['wrap_columns']
recorded_model = _DISPLAY['recorded_model']
AGENT_GLYPHS = {'codex': '⬡', 'claude': '✱', 'slot': '·', 'revision': '·'}

SID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
REPO_IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
REPOSITORY_PROOF_TTL = 86400

PR_URL = re.compile(r'https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)')


class HistoryError(ValueError):
    pass


class HistoryCancelled(HistoryError):
    """The user left a history selection without choosing work."""


def clean(value):
    return ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value or '')).split())


@_spinner('Loading history metadata')
def run(argv, *, cwd=None, timeout=30, optional=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL, start_new_session=True)
        if not result.returncode:
            return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    if optional:
        return ''
    raise HistoryError(f'{argv[0]} failed; history and existing workspaces were retained')


def slug(remote):
    match = re.fullmatch(r'(?:https://github.com/|git@github.com:|ssh://git@github.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?', remote)
    return match[1].lower() if match else None


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def validate(record):
    if not isinstance(record, dict) or record.get('agent') not in ('codex', 'claude') or not isinstance(record.get('sid'), str) or not SID.fullmatch(record['sid']) or not isinstance(record.get('repo'), str) or not record['repo']:
        raise HistoryError('invalid conversation identity')
    if not isinstance(record.get('updated', 0), (int, float)) or not math.isfinite(record.get('updated', 0)) or not 0 <= record.get('updated', 0) <= 253402300799:
        raise HistoryError('invalid history timestamp')
    if 'last_turn' in record:
        record['last_turn'] = _DISPLAY['_turn_timestamp'](record['last_turn'])
    for field in ('model', 'model_source', 'summary', 'summary_source', 'interface'):
        if field in record and not isinstance(record[field], str):
            raise HistoryError('invalid recorded display metadata')
    if record.get('interface', 'unknown') not in ('cli', 'desktop', 'foreground', 'unknown'):
        raise HistoryError('invalid recorded interface')
    for field in ('model_updated', 'summary_updated', 'interface_updated'):
        if field in record and (not isinstance(record[field], (int, float)) or not math.isfinite(record[field]) or not 0 <= record[field] <= 253402300799):
            raise HistoryError('invalid display metadata timestamp')
    for field in ('associations', 'sources', 'prs', 'mentions'):
        values = record.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise HistoryError('invalid history metadata')
        for value in values:
            if field in ('prs', 'mentions'):
                if not isinstance(value.get('repo'), str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', value['repo']) or type(value.get('number')) is not int or value['number'] < 1:
                    raise HistoryError('invalid PR association')
                value['url'] = f'https://github.com/{value["repo"]}/pull/{value["number"]}'
            elif field == 'associations':
                if not isinstance(value.get('workspace'), str) or not value['workspace'].startswith('/') or not isinstance(value.get('observed', 0), (int, float)) or not math.isfinite(value.get('observed', 0)):
                    raise HistoryError('invalid workspace association')
                if value.get('slot') and (not isinstance(value['slot'], str) or not re.fullmatch(r'[1-9][0-9]*', value['slot'])):
                    raise HistoryError('invalid history slot')
                for key in ('host', 'slot', 'branch', 'commit', 'evidence', 'generation'):
                    if key in value and not isinstance(value[key], str):
                        raise HistoryError('invalid workspace metadata')
            elif not isinstance(value.get('path'), str) or not isinstance(value.get('host'), str) or not isinstance(value.get('updated', 0), (int, float)) or not math.isfinite(value.get('updated', 0)) or type(value.get('size', 0)) is not int:
                raise HistoryError('invalid transcript source')
    return record


def generation(workspace):
    gitfile = Path(workspace) / '.git'
    try:
        info = gitfile.stat()
    except OSError:
        return ''
    gitdir = run(['git', '-C', str(workspace), 'rev-parse', '--absolute-git-dir'], optional=True).strip()
    return hashlib.sha256(json.dumps([gitdir, info.st_dev, info.st_ino, info.st_ctime_ns]).encode()).hexdigest() if gitdir else ''


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 't/history/v1')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        path = self.directory / 'history.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute('create table if not exists conversations (key text primary key, record text not null)')
        self.db.execute('create table if not exists scans (path text primary key, stamp text, record text)')
        self.db.execute('create table if not exists prs (key text primary key, checked real, record text)')
        self.db.commit()

    def close(self):
        self.db.close()

    def records(self):
        return [json.loads(row[0]) for row in self.db.execute('select record from conversations')]

    def merge(self, record):
        validate(record)
        key = json.dumps([record['repo'], record['agent'], record['sid']])
        existing = self.db.execute('select record from conversations where key=?', (key,)).fetchone()
        old = json.loads(existing[0]) if existing else {}
        merged = {**old, **record, 'version': VERSION}
        for field in ('title', 'model', 'summary', 'interface'):
            incoming_known = record.get(field) not in (None, '', '-', 'unknown')
            old_known = old.get(field) not in (None, '', '-', 'unknown')
            incoming_time = record.get(field + '_updated', record.get('updated', 0))
            old_time = old.get(field + '_updated', old.get('updated', 0))
            if old_known and (not incoming_known or incoming_time < old_time):
                merged[field] = old[field]
                if field in ('model', 'summary', 'interface'):
                    merged[field + '_updated'] = old_time
                if field + '_source' in old:
                    merged[field + '_source'] = old[field + '_source']
            elif incoming_known and field in ('model', 'summary', 'interface'):
                merged[field + '_updated'] = incoming_time
        for field in ('associations', 'prs', 'mentions', 'sources'):
            values = old.get(field, []) + record.get(field, [])
            key_for = (lambda value: (value['repo'], value['number'])) if field in ('prs', 'mentions') else (lambda value: (value.get('host'), value.get('path'))) if field == 'sources' else (lambda value: json.dumps(value, sort_keys=True))
            unique = {}
            for value in values:
                identity = key_for(value)
                unique[identity] = {**unique.get(identity, {}), **value} if field == 'sources' else value
            merged[field] = list(unique.values())
        merged['last_turn'] = max(old.get('last_turn', '-'), record.get('last_turn', '-'))
        merged['updated'] = max(old.get('updated', 0), record.get('updated', 0))
        self.db.execute('insert or replace into conversations values (?,?)', (key, json.dumps(merged)))
        self.db.commit()
        return merged

    def scan(self, path, agent, *, metadata_only=False):
        info = path.stat()
        stamp = json.dumps([info.st_ino, info.st_size, info.st_mtime_ns])
        key = str(path) + ('#resume' if metadata_only else '')
        old = self.db.execute('select stamp,record from scans where path=?', (key,)).fetchone()
        cached = json.loads(old[1]) if old else None
        if old and old[0] == stamp and cached.get('_scan', {}).get('parser') == PARSER_VERSION:
            return cached
        # Append-only transcripts are rescanned only when changed. Keep the durable
        # record independent of this disposable discovery cache.
        previous = cached if cached and cached.get('_scan', {}).get('parser') == PARSER_VERSION else None
        if previous and previous.get('_scan', {}).get('inode') != info.st_ino:
            previous = None
        value = transcript(path, agent, previous, metadata_only=True) if metadata_only else transcript(path, agent, previous)
        value['last_turn'] = _DISPLAY['transcript_last_turn'](path, agent)
        self.db.execute('insert or replace into scans values (?,?,?)', (key, stamp, json.dumps(value)))
        self.db.commit()
        return value


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else value
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0


def pr_refs(text):
    return [dict(repo=repo.lower(), number=int(number), url=f'https://github.com/{repo}/pull/{number}')
            for repo, number in PR_URL.findall(text)]


JS_STRING = r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"


def js_structure(source):
    masked = re.sub(JS_STRING, lambda match: ' ' * len(match[0]), source, flags=re.S)
    return re.sub(r'//[^\n]*|/\*.*?\*/', lambda match: ' ' * len(match[0]), masked, flags=re.S)


def execution_objects(source):
    masked = js_structure(source)
    objects = []
    for call in re.finditer(r'\btools\.exec_command\s*\(\s*\{', masked):
        start, depth = call.end() - 1, 0
        for index in range(start, len(masked)):
            depth += (masked[index] == '{') - (masked[index] == '}')
            if not depth:
                objects.append((start, index, masked))
                break
    return objects


def literal_commands(source):
    """Decode one actual static exec invocation; skip dynamic or batched wrappers."""
    objects = execution_objects(source)
    if len(objects) != 1:
        return []
    start, end, masked = objects[0]
    commands = []
    pattern = r"\b(?:cmd|command)\s*:\s*(" + JS_STRING + ')'
    for match in re.finditer(pattern, source[start + 1:end], re.S):
        position = start + 1 + match.start()
        if not masked[position:position + 3].strip():
            continue
        if masked[start:position].count('{') - masked[start:position].count('}') != 1:
            continue
        try:
            value = ast.literal_eval(match[1])
            if isinstance(value, str):
                commands.append(value)
        except (ValueError, SyntaxError):
            pass
    return commands


def shell_source(command):
    """Discard heredoc bodies; their quoted example commands are not invocations."""
    lines, delimiters = [], []
    for line in re.sub(r'\\\n', '', command).splitlines():
        if delimiters:
            if line.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        lines.append(line)
        delimiters.extend(left or right for left, right in re.findall(r"(?<!<)<<-?\s*(?:['\"]([^'\"]+)['\"]|([A-Za-z0-9_]+))", line))
    return '\n'.join(lines)


def poll_key(call, args):
    if call.get('type') == 'custom_tool_call' and isinstance(args, str):
        # Only a literal session id on the execution API is lineage evidence.
        match = re.search(r'\btools\.write_stdin\s*\(\s*\{[^}]*\bsession_id\s*:\s*([0-9]+)', js_structure(args), re.S)
        if match:
            return 'session:' + match[1]
    if isinstance(args, dict):
        name = str(call.get('name') or '').split('.')[-1]
        if name == 'write_stdin' and type(args.get('session_id')) is int:
            return 'session:' + str(args['session_id'])
        if name == 'wait' and isinstance(args.get('cell_id'), str):
            return 'cell:' + args['cell_id']
    return None


def running_keys(output):
    sessions = ['session:' + value for value in re.findall(r'"session_id"\s*:\s*([0-9]+)', output)]
    cells = ['cell:' + value for value in re.findall(r'Script running with cell ID ([A-Za-z0-9_-]+)', output)]
    return sessions + cells


def tool_pr_command(command):
    """Extract actual gh invocations, excluding quoted prose/printf and PR bodies."""
    if not isinstance(command, str):
        return []
    try:
        lexer = shlex.shlex(shell_source(command), posix=True, punctuation_chars=';&|()\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    statements, statement = [], []
    for token in tokens + [';']:
        if token and all(char in ';&|()\n' for char in token):
            if statement:
                statements.append(statement)
            statement = []
        else:
            statement.append(token)
    # One successful shell result proves an unbranched sole gh invocation.
    # Mixed/control-flow commands cannot safely attribute arbitrary stdout URLs.
    if len(statements) != 1 or any(token in ('&&', '||') for token in tokens):
        return []
    out = []
    for words in statements:
        while words and (re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0]) or words[0] in ('command', 'env')):
            words = words[1:]
        if len(words) < 3 or Path(words[0]).name != 'gh' or words[1] != 'pr' or words[2] not in ('create', 'checkout', 'edit'):
            continue
        repo = None
        for index, word in enumerate(words):
            if word in ('--repo', '-R') and index + 1 < len(words):
                repo = words[index + 1].lower()
            elif word.startswith('--repo='):
                repo = word.split('=', 1)[1].lower()
        refs, numbers = [], []
        if words[2] != 'create' and len(words) > 3:
            target = words[3]
            refs = pr_refs(target) if PR_URL.fullmatch(target) else []
            if re.fullmatch(r'[1-9][0-9]*', target):
                numbers = [dict(number=int(target), repo=repo)]
        out.append(dict(refs=refs, numbers=numbers, action=words[2]))
    return out


def transcript(path, agent, previous=None, *, metadata_only=False):
    record = dict(agent=agent, sid='', title='', summary='', summary_source='unknown', model='-', model_source='unknown', interface='unknown', updated=0, associations=[], prs=[], mentions=[])
    state = previous.get('_scan', {}) if previous else {}
    if previous and state.get('offset', 0) <= path.stat().st_size:
        record = json.loads(json.dumps(previous))
    else:
        state = {}
    pending, polls, executions = state.get('pending', {}), state.get('polls', {}), state.get('executions', {})
    tools, cwd, branch, commit = state.get('tools', {}), state.get('cwd', ''), state.get('branch', ''), state.get('commit', '')
    offset = state.get('offset', 0)
    try:
        with path.open('rb') as stream:
            stream.seek(offset)
            for raw in stream:
                if not raw.endswith(b'\n'):
                    break
                offset += len(raw)
                line = raw.decode('utf-8', 'replace')
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                except ValueError:
                    continue
                record['updated'] = max(record['updated'], timestamp(row.get('timestamp')))
                model = recorded_model(row)
                if model:
                    record.update(model=clean(model), model_source='transcript', model_updated=record['updated'])
                payload = row.get('payload', {}) if agent == 'codex' else row
                if not isinstance(payload, dict):
                    continue
                if row.get('type') == 'session_meta':
                    incoming_sid = payload.get('id', '')
                    if record['sid'] and record['sid'] != incoming_sid:
                        raise HistoryError('transcript contains conflicting conversation identities')
                    record['sid'] = incoming_sid
                    cwd = payload.get('cwd', '')
                    git = payload.get('git') or {}
                    if isinstance(git, dict):
                        branch, commit = git.get('branch', ''), git.get('commit_hash', '')
                        repository = slug(git.get('repository_url', ''))
                        if record.get('recorded_repo') and repository and record['recorded_repo'] != repository:
                            raise HistoryError('transcript contains conflicting repository identities')
                        record['recorded_repo'] = repository or record.get('recorded_repo')
                elif agent == 'claude':
                    incoming_sid = row.get('sessionId') or record['sid']
                    if record['sid'] and incoming_sid != record['sid']:
                        raise HistoryError('transcript contains conflicting conversation identities')
                    record['sid'] = incoming_sid
                    cwd = row.get('cwd') or cwd
                    branch = row.get('gitBranch') or branch
                if cwd:
                    association = dict(workspace=cwd, branch=clean(branch), commit=commit if re.fullmatch(r'[a-fA-F0-9]{40,64}', str(commit)) else '', evidence='transcript', observed=record['updated'])
                    if not record['associations'] or any(record['associations'][-1].get(key) != association.get(key) for key in ('workspace', 'branch', 'commit')):
                        record['associations'].append(association)
                if row.get('type') in ('custom-title', 'ai-title'):
                    record['title'] = clean(row.get('customTitle') or row.get('aiTitle'))
                    if record['title']:
                        record.update(summary=record['title'], summary_source='transcript title', summary_updated=record['updated'])
                message = payload if agent == 'codex' else row.get('message', {})
                if not isinstance(message, dict):
                    continue
                role = message.get('role') or row.get('type')
                content = message.get('content', [])
                blocks = content if isinstance(content, list) else [{'text': content}]
                text = ' '.join(str(b.get('text', '')) for b in blocks if isinstance(b, dict))
                if role == 'user' and not record['title'] and text.strip() and not text.lstrip().startswith(('<', '# AGENTS.md instructions')):
                    record['title'] = clean(text)[:200]
                if role == 'user' and text.strip() and not text.lstrip().startswith(('<', '# AGENTS.md instructions')):
                    record.update(summary=record['title'] or clean(text), summary_source='transcript', summary_updated=record['updated'])
                if metadata_only:
                    continue
                if role in ('user', 'assistant'):
                    record['mentions'].extend(pr_refs(text))
                # Structured tool invocations establish association; conversational
                # links remain explicitly incidental. Pair command output by tool id.
                calls = blocks if agent == 'claude' else [payload]
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    if call.get('type') in ('function_call', 'tool_use', 'custom_tool_call'):
                        args = call.get('arguments') or call.get('input') or {}
                        if isinstance(args, str) and call.get('type') != 'custom_tool_call':
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        key = poll_key(call, args)
                        call_id = call.get('call_id') or call.get('id') or 'unknown'
                        if key and key in pending:
                            polls[call_id] = key
                        if call.get('type') == 'custom_tool_call' and isinstance(args, str):
                            executions[call_id] = bool(execution_objects(args)) or str(call.get('name') or '').split('.')[-1] == 'exec'
                            commands = literal_commands(args)
                        else:
                            executions[call_id] = str(call.get('name') or '').split('.')[-1] in ('exec_command', 'shell_command', 'Bash')
                            commands = [args.get('command') or args.get('cmd') or ''] if isinstance(args, dict) else []
                        evidence = [item for command in commands for item in tool_pr_command(command)]
                        if evidence:
                            tools[call_id] = evidence
                    elif call.get('type') in ('function_call_output', 'custom_tool_call_output', 'tool_result'):
                        call_id = call.get('call_id') or call.get('tool_use_id')
                        key = polls.get(call_id)
                        inherited = pending.get(key, {})
                        evidence = tools.get(call_id) or inherited.get('evidence')
                        output = str(call.get('output') or call.get('content') or '')
                        statuses = [int(status) for status in re.findall(r'(?:"exit_code"\s*:\s*|Exit code:\s*|Process exited with code\s+)(-?[0-9]+)', output)]
                        failed = call.get('is_error') or any(status != 0 for status in statuses)
                        running = running_keys(output)
                        if executions.get(call_id):
                            for identifier in running:
                                pending.pop(identifier, None)
                        if evidence and failed:
                            if key:
                                pending.pop(key, None)
                        elif evidence and running:
                            item = dict(evidence=evidence, refs=inherited.get('refs', []) + pr_refs(output))
                            for identifier in running:
                                pending[identifier] = item
                        elif evidence and (not key or statuses and all(status == 0 for status in statuses)):
                            for item in evidence:
                                record['prs'].extend(item['refs'])
                                record.setdefault('pr_numbers', []).extend(item['numbers'])
                                if item['action'] == 'create':
                                    record['prs'].extend(inherited.get('refs', []) + pr_refs(output))
                            if key:
                                pending.pop(key, None)
    except OSError:
        return record
    if not record['sid'] and agent == 'claude':
        record['sid'] = path.stem
    if cwd:
        association = dict(workspace=cwd, branch=clean(branch), commit=commit if re.fullmatch(r'[a-fA-F0-9]{40,64}', str(commit)) else '', evidence='transcript', observed=record['updated'])
        if not record['associations']:
            record['associations'].append(association)
    record['_scan'] = dict(offset=offset, inode=path.stat().st_ino, cwd=cwd, branch=branch, commit=commit, tools=tools, parser=PARSER_VERSION, pending=pending, polls=polls, executions=executions)
    return record


def page_rows(rows, query='', limit=50, page=1):
    if limit < 1 or page < 1:
        raise HistoryError('--limit and --page must be positive integers')
    terms = clean(query).casefold().split()
    matches = [row for row in rows if all(term in json.dumps(row, ensure_ascii=False).casefold() for term in terms)]
    matches.sort(key=lambda row: (-row.get('updated', 0), row['agent'], row['sid']))
    return matches[(page - 1) * limit:page * limit], len(matches)


def association_matches(association, cfg, alias):
    repo, slot = cfg.repo_of_dir(association.get('workspace', ''))
    return (slot or '') if repo and os.path.realpath(cfg.repos[repo]) == os.path.realpath(cfg.repos[alias]) else None


def recorded_slot(workspace):
    """A historical path can identify a slot, but never a current destination."""
    path = Path(workspace)
    match = re.fullmatch(re.escape(path.parent.name) + r'-([1-9][0-9]*)', path.name)
    if match:
        return match[1]
    return path.name if re.fullmatch(r'[1-9][0-9]*', path.name) else ''


def history_display(row, alias=''):
    """Exact-conversation display facts; presentation never grants ownership."""
    owners = row.get('owners', [])
    owner = owners[0] if len(owners) == 1 else {}
    latest = max(row.get('associations', []), key=lambda value: value.get('observed', 0), default={})
    state = owner.get('state') or ('archived' if row.get('archived') else 'saved')
    sources = row.get('sources', [])
    if not owner and sources and all(source.get('available') is False for source in sources):
        state = 'missing'
    interface = ('desktop' if state == 'app' else 'foreground' if ':' in owner.get('slot', '') else 'cli') if owner else row.get('interface', 'unknown')
    slot = owner.get('slot')
    if not slot:
        name = alias or row.get('display', {}).get('slot', '').rsplit('-', 1)[0] or row['repo'].rsplit('/', 1)[-1]
        slot = name + '-' + latest['slot'] if latest.get('slot') else 'unknown'
    summary = clean(row.get('summary') or row.get('title')) or 'Untitled conversation'
    return dict(state=state, context=owner.get('context') or 'unknown', interface=interface,
                agent=row['agent'], model=clean(row.get('model')) or '-', host=owner.get('host') or latest.get('host') or 'unknown',
                slot=clean(slot), summary=summary,
                status={'attached': 'Attached', 'detached': 'Detached', 'app': 'Desktop', 'saved': 'Saved',
                        'archived': 'Archived', 'missing': 'Missing transcript'}.get(state, 'Live'), prs=row.get('prs', []))


def pr_summary(refs, compact=False):
    states = {'OPEN': 'O', 'MERGED': 'M', 'CLOSED': 'C'}
    parts = [f'#{pr["number"]}{states.get(pr.get("state"), "?")}' if compact else
             f'#{pr["number"]}[{pr.get("state") or "unknown"}]' for pr in refs[:3]]
    if len(refs) > 3:
        parts.append(f'+{len(refs) - 3}')
    return ' '.join(parts)


def picker_columns(width):
    if width >= 60:
        return [('ST', 3), ('UI', 2), ('MODEL', 24 if width >= 100 else 13), ('HOST', 10 if width >= 100 else 5), ('SESSION', 12 if width >= 100 else 7)]
    if width >= 32:
        return [('ST', 2), ('U', 1), ('MODEL', 9), ('#', 2)]
    return [('S', 1), ('A', 1), ('#', 2)]


def compact_model(model, width):
    if display_width(model) <= width:
        return model
    for prefix in ('claude-', 'gpt-'):
        if model.startswith(prefix):
            model = model[len(prefix):]
            break
    return clip_columns(model, width)


def format_row(row, width=80):
    display = row.get('display') or history_display(row)
    glyph = AGENT_GLYPHS.get(display['agent'], '?')
    state, context = display['state'], display['context']
    status = '●' if state == 'attached' or (state == 'app' and context == 'active') else '○' if state in ('detached', 'app') else '□' if state == 'archived' else '!' if state == 'missing' else '·'
    if context == 'active':
        status += '✓'
    if re.search(r'#\d+ merged, still in progress', display['summary']):
        status += '↻'
    ui = {'cli': '›', 'desktop': '▣', 'foreground': '›', 'unknown': '?'}[display['interface']]
    values = [status, ui, glyph + ' ' + display['model'], display['host'], display['slot']]
    columns = picker_columns(width)
    if width < 60:
        slot = display['slot'].rsplit('-', 1)[-1]
        values = [status, ui, glyph + ' ' + display['model'], slot] if width >= 32 else [status[:1], glyph, slot]
    if width >= 32:
        values[2] = glyph + ' ' + compact_model(display['model'], columns[2][1] - 2)
    prefix = ' '.join(clip_columns(value, cap) + ' ' * (cap - display_width(clip_columns(value, cap))) for value, (_, cap) in zip(values, columns))
    remaining = max(1, width - display_width(prefix) - 1)
    prs = pr_summary(row.get('prs', []), compact=True)
    title = display['summary']
    if prs and remaining >= 24:
        budget = min(display_width(prs), max(0, remaining - 15))
        prs = clip_columns(prs, budget)
        title = clip_columns(title, remaining - display_width(prs) - 1)
        summary = title + ' ' + prs
    elif prs:
        hint = f' +{len(row["prs"])}PR'
        summary = clip_columns(title, max(1, remaining - len(hint))) + hint if remaining >= 17 else clip_columns(title, remaining)
    else:
        summary = clip_columns(title, remaining)
    return prefix + ' ' + summary


def picker_header(rows, query, limit, page, width=80):
    _, total = page_rows(rows, query, limit, page)
    pages = max(1, (total + limit - 1) // limit)
    page_label = f'Page {page} · {total} conversations · {limit} per page' if width >= 60 else f'Page {page}/{pages} · {total} conversations'
    columns = picker_columns(width)
    heading = ' '.join(clip_columns(label, cap).ljust(cap) for label, cap in columns) + ' WORKING ON'
    lines = [clip_columns(page_label, width), clip_columns(heading, width)]
    if width >= 60:
        lines.insert(1, 'PR: O open · M merged · C closed · ? unknown')
    return '\n'.join(lines) + '\n'


def history_table(rows, api, alias=''):
    cells = []
    for row in rows:
        display = history_display(row, alias)
        base = _DISPLAY['session_cells'](display, AGENT_GLYPHS.get(display['agent'], '?'), remote=True)
        date = datetime.datetime.fromtimestamp(row.get('updated', 0)).strftime('%Y-%m-%d')
        branches = ', '.join(dict.fromkeys(a.get('branch') for a in row.get('associations', []) if a.get('branch'))) or 'unknown'
        cells.append([*base, date, branches, pr_summary(row.get('prs', [])) or 'none recorded', row['sid']])
    return api['_table_lines'](('STATUS', 'UI', 'MODEL', 'HOST', 'SESSION', 'WORKING ON', 'DATE', 'BRANCH', 'PRS', 'ID'), cells)


def details(row, alias='', width=None, compact=False):
    display = row.get('display') or history_display(row, alias)
    model = f'{AGENT_GLYPHS.get(display["agent"], "?")} {display["model"]} ({display["agent"]})'
    date = datetime.datetime.fromtimestamp(row.get('updated', 0)).strftime('%Y-%m-%d %H:%M')
    fields = [f'Working on: {display["summary"]}',
              f'Status: {display["status"]} · Context: {display["context"]}',
              f'UI: {display["interface"]} · Model: {model}',
              f'Host: {display["host"]} · Session: {display["slot"]}',
              f'Date: {date} · PRs: {pr_summary(row.get("prs", [])) or "none recorded"}',
              f'ID: {row["sid"]}', f'Repository: {row["repo"]}', f'Title: {row.get("title") or "Untitled conversation"}',
              f'Model evidence: {row.get("model_source") or "unknown"}',
              f'Summary evidence: {row.get("summary_source") or "unknown"}']
    if compact:
        fields = [f'Model: {display["model"]}', f'UI: {display["interface"]}', f'Status: {display["status"]} · {display["context"]}', *fields]
    fields += ['', 'Workspace history']
    for association in row.get('associations', []):
        fields.extend([f'  Workspace: {association.get("workspace")}',
                       f'  Slot: {association.get("slot") or "unknown"} · Host: {association.get("host") or "unknown"}',
                       f'  Branch: {association.get("branch") or "unknown"}',
                       f'  Revision: {association.get("commit") or "unverified"}',
                       f'  Evidence: {association.get("evidence") or "unknown"} · Available: {association.get("available", "unknown")}'])
    for pr in row.get('prs', []):
        fields.extend(['', f'Associated PR: {pr["url"]}', f'  State: {pr.get("state") or "unknown"}'])
    linked = {pr['url'] for pr in row.get('prs', [])}
    for mention in row.get('mentions', []):
        if mention['url'] not in linked:
            fields.extend(['', f'Incidental PR mention: {mention["url"]}'])
    for source in row.get('sources', []):
        fields.extend(['', f'Transcript: {source.get("path")}', f'  Host: {source.get("host") or "unknown"} · Available: {source.get("available", "unknown")}'])
    if width:
        fields = [part for field in fields for part in wrap_columns(clean(field), max(8, width), '  ')]
    return '\n'.join(fields)


class HistoryCommand:
    def __init__(self, cfg, api, store=None):
        self.cfg, self.api = cfg, api
        self.root = api['_t_source_root']()
        self.store = store or Store()
        self.host = socket.gethostname().split('.')[0]
        self.reservation_token = uuid.uuid4().hex
        self.reservation = None
        self.discovery_identities = {}
        self.repository_proofs = {}
        self.fresh_repository_proofs = set()
        self.strict_identity = False
        self.error, self.run, self.spinner = HistoryError, run, _spinner
        self.validate, self.generation, self.slug = validate, generation, slug
        self.catalog_repositories = {}
        self.local_identities = {}

    def repository_proof(self, identity):
        """Only authenticated GitHub evidence may equate different repository names."""
        if not REPO_IDENTITY.fullmatch(identity):
            return None
        identity = identity.lower()
        if identity in self.repository_proofs and (not self.strict_identity or identity in self.fresh_repository_proofs):
            return self.repository_proofs[identity]
        path = self.store.directory / 'repository-proofs.json'
        try:
            saved = json.loads(path.read_text())
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            saved = {}
        proof = saved.get(identity)
        now = time.time()
        def valid(value):
            return (isinstance(value, dict) and isinstance(value.get('id'), str)
                    and bool(re.fullmatch(r'[A-Za-z0-9_=-]+', value['id']))
                    and isinstance(value.get('canonical'), str)
                    and REPO_IDENTITY.fullmatch(value['canonical'])
                    and type(value.get('verified')) in (int, float)
                    and 0 <= now - value['verified'] < REPOSITORY_PROOF_TTL)
        if self.strict_identity or not valid(proof):
            proof = None
            if self.strict_identity:
                self.fresh_repository_proofs.add(identity)
            try:
                value = json.loads(run(['gh', 'repo', 'view', identity, '--json', 'id,nameWithOwner'], timeout=8, optional=True))
                candidate = dict(id=value.get('id'), canonical=value.get('nameWithOwner', '').lower(), verified=now)
                if valid(candidate):
                    proof = candidate
                    saved[identity] = proof
                    private_write(path, saved)
            except (ValueError, TypeError, AttributeError):
                pass
        self.repository_proofs[identity] = proof
        return proof

    def canonical_identity(self, identity):
        proof = self.repository_proof(identity)
        return proof['canonical'] if proof else identity

    def same_repository(self, left, right):
        if left == right:
            return True
        one, two = self.repository_proof(left), self.repository_proof(right)
        return bool(one and two and one['id'] == two['id'])

    def record_identity(self, alias):
        return self.discovery_identities.get(alias) or self.identity(alias)

    def repository_alias(self, identity):
        for alias in self.cfg.repos:
            if alias not in self.local_identities:
                self.local_identities[alias] = self.identity(alias)
        aliases = [alias for alias, repo in self.local_identities.items() if alias in self.cfg.repos and self.same_repository(repo, identity)]
        return min(aliases, key=lambda alias: (alias != Path(self.cfg.repos[alias]).name, len(alias), alias)) if aliases else None

    def local_catalog(self):
        """Export host-local metadata, never evidence imported from another host."""
        repositories = {}
        for alias, path in self.cfg.repos.items():
            identity = self.canonical_identity(self.identity(alias))
            self.discovery_identities[alias] = identity
            self.discover(alias, metadata_only=True)
            repositories.setdefault(identity, dict(repo=identity, alias=alias, path=path))
        codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        for folder in ('sessions', 'archived_sessions'):
            for path in (codex / folder).glob('**/rollout-*.jsonl'):
                try:
                    with path.open() as stream:
                        first = json.loads(stream.readline())
                    payload = first.get('payload', {})
                    if payload.get('thread_source') == 'subagent' or isinstance(payload.get('source'), dict) and 'subagent' in payload['source']:
                        continue
                    raw_identity = slug((payload.get('git') or {}).get('repository_url', ''))
                    if not raw_identity:
                        continue
                    identity = self.canonical_identity(raw_identity)
                    value = self.store.scan(path, 'codex', metadata_only=True)
                    if not value.get('sid') or not self.same_repository(value.get('recorded_repo', ''), identity):
                        continue
                    value.pop('_scan', None)
                    value.update(repo=identity, archived=folder == 'archived_sessions')
                    value['associations'] = [{**a, 'host': 'local', 'slot': recorded_slot(a['workspace'])} for a in value['associations']]
                    value['sources'] = [dict(host='local', path=str(path), updated=path.stat().st_mtime, size=path.stat().st_size)]
                    value['updated'] = max(value.get('updated', 0), path.stat().st_mtime)
                    self.store.merge(value)
                    repositories.setdefault(identity, dict(repo=identity, alias=identity, path=''))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
        rows = []
        for row in self.store.records():
            row = dict(row)
            row['associations'] = [a for a in row.get('associations', []) if a.get('host') == 'local']
            row['sources'] = [source for source in row.get('sources', []) if source.get('host') == 'local']
            row.pop('owners', None)
            row.pop('display', None)
            if row['associations'] and row['sources']:
                identity = self.canonical_identity(row['repo'])
                row['repo'] = identity
                repositories.setdefault(identity, dict(repo=identity, alias=identity, path=''))
                rows.append(row)
        return dict(version=1, repositories=list(repositories.values()), conversations=rows)

    def import_catalog(self, payload, host):
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise HistoryError('invalid remote repository catalog')
        repositories, rows = payload.get('repositories'), payload.get('conversations')
        if not isinstance(repositories, list) or not isinstance(rows, list):
            raise HistoryError('invalid remote repository catalog')
        allowed = {}
        for repository in repositories:
            if not isinstance(repository, dict) or not isinstance(repository.get('repo'), str):
                raise HistoryError('invalid remote repository identity')
            raw = repository['repo']
            if not REPO_IDENTITY.fullmatch(raw):
                # Machine-local identities cannot establish cross-host equivalence.
                continue
            if not all(isinstance(repository.get(key), str) and clean(repository[key]) == repository[key] for key in ('alias', 'path')):
                raise HistoryError('invalid remote repository metadata')
            identity = self.canonical_identity(raw)
            allowed[raw] = identity
            self.catalog_repositories.setdefault(identity, dict(repo=identity, alias=repository['alias'], path=repository['path'], host=host))
        pending = []
        for row in rows:
            row = dict(row)
            validate(row)
            if row['repo'] not in allowed:
                continue
            row['repo'] = allowed[row['repo']]
            row.pop('owners', None)
            row.pop('display', None)
            for field in ('associations', 'sources'):
                if any(value.get('host') != 'local' for value in row.get(field, [])):
                    raise HistoryError('remote catalog contains imported evidence')
                key = 'workspace' if field == 'associations' else 'path'
                if any(not value.get(key, '').startswith('/') or clean(value[key]) != value[key] for value in row.get(field, [])):
                    raise HistoryError('invalid remote catalog path')
                row[field] = [{**value, 'host': host} for value in row.get(field, [])]
            pending.append(row)
        for row in pending:
            self.store.merge(row)

    def discover_catalog(self):
        local = self.local_catalog()
        self.catalog_repositories = {r['repo']: {**r, 'host': 'local'} for r in local['repositories']}
        targets = set()
        for host, target in self.cfg.hosts.items():
            if target in targets:
                continue
            targets.add(target)
            try:
                output = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', target,
                              'zsh -lic ' + shlex.quote('command t __history-catalog')], timeout=20)
                self.import_catalog(json.loads(output), host)
            except (HistoryError, ValueError, TypeError, AttributeError):
                # Older hosts retain the scoped protocol for locally known repos.
                for alias in self.cfg.repos:
                    self.refresh(alias, hosts={host: target})
        for row in self.store.records():
            identity = self.canonical_identity(row['repo'])
            if row.get('sources') and (REPO_IDENTITY.fullmatch(identity) or self.repository_alias(identity)):
                self.catalog_repositories.setdefault(identity, dict(repo=identity, alias=identity, path='', host='cached'))
        private_write(self.store.directory / 'repository-catalog.json', self.catalog_repositories)
        return self.catalog_repositories

    def worktree_enabled(self, alias=None):
        value = getattr(self.cfg, 'worktree', {}).get(alias) or getattr(self.cfg, 'worktree_default', '1')
        return value not in ('0', 'no', 'off', 'false')

    def materialize_repository(self, identity):
        alias = self.repository_alias(identity)
        if not self.worktree_enabled(alias):
            raise HistoryError('repository opts out of worktree-per-session; no checkout was created')
        if alias:
            return alias
        if not REPO_IDENTITY.fullmatch(identity):
            raise HistoryError('selected repository has no verified GitHub clone identity')
        # Reuse checkout's collision checks and registration primitives, with no
        # onboarding, instruction-seeding, trust, or unrelated repository actions.
        owner, name = identity.split('/')
        path = self.api['_new_path'](name)
        alias = name
        with (self.store.directory / 'repository-checkout.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                local_text = Path(self.api['_current_local_rc']()).read_text()
            except OSError:
                local_text = ''
            registered, _, _ = self.api['_local_entries'](local_text)
            whence = self.api['_new_sh_whence'](alias)
            registration = self.api['_new_alias_check'](alias, path, registered, self.cfg.repos, whence)
            state = self.api['_new_sh_state'](path)
            # A matching existing canonical checkout is safe to register; linked
            # worktrees and symlink destinations are never adopted.
            if os.path.islink(path) or state['is_repo'] and not Path(path, '.git').is_dir():
                raise HistoryError('repository destination is a symlink or linked worktree')
            url = self.api['_new_clone_url'](owner, name)
            if state['is_repo'] and self.same_repository(slug(state.get('origin_url') or '') or '', identity):
                state = {**state, 'origin_url': url}
            steps = self.api['_new_land_plan'](name, alias, url, path, state, registration)
            errors = [step['label'] for step in steps if step['do'] == 'error']
            if errors:
                raise HistoryError('; '.join(errors))
            for step in steps:
                if step['do'] == 'run' and step['step'] == 'clone':
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    run(step['cmd'], timeout=600)
            actual = slug(run(['git', '-C', path, 'remote', 'get-url', 'origin']).strip())
            if not actual or not self.same_repository(actual, identity):
                raise HistoryError('repository identity changed before registration')
            # Clone may take minutes. Revalidate both filesystem and registration
            # state before publishing a local alias; never overwrite a new mapping.
            if (os.path.islink(path) or not Path(path, '.git').is_dir()
                    or Path(path, '.git').is_symlink()):
                raise HistoryError('repository destination changed before registration')
            local_rc = self.api['_current_local_rc']()
            before = self.api['_config_read'](local_rc)
            registered, _, _ = self.api['_local_entries'](before)
            registration = self.api['_new_alias_check'](
                alias, path, registered, self.cfg.repos, self.api['_new_sh_whence'](alias))
            if registration[0] == 'error':
                raise HistoryError(registration[1])
            if registration[0] == 'run':
                block = self.api['_setup_block']({alias: path}, {}, None,
                    time.strftime('%Y-%m-%d'), tool='t session resume')
                after = before + ('\n' if before and not before.endswith('\n') else '') + '\n' + block
                try:
                    self.api['_config_write'](local_rc, before, after)
                except (OSError, ValueError) as error:
                    raise HistoryError('repository registration changed; retry resume') from error
                self.api['zsh_capture']('_t_sync_config')
            self.cfg.repos[alias] = path
            self.local_identities[alias] = identity
        return alias

    def shell(self, snippet):
        return ['zsh', '-lic', 'setopt no_monitor no_notify; source ' + shlex.quote(str(Path(self.root) / 't.plugin.zsh')) + '; ' + snippet]

    def scope(self, target):
        if len(target) > 2:
            raise HistoryError('usage: t history [repo] [slot]')
        inferred_repo, inferred_slot = self.cfg.repo_of_dir(os.getcwd())
        repo = target[0] if target else inferred_repo
        slot = target[1] if len(target) == 2 else inferred_slot if not target else ''
        if len(target) == 1 and target[0].isdigit():
            repo, slot = inferred_repo, target[0]
        try:
            repo = _resolve_repo(self.cfg.repos, repo) or repo
        except ValueError as error:
            raise HistoryError(str(error)) from error
        if repo not in self.cfg.repos:
            raise HistoryError('cannot infer repository; specify a registered repository alias')
        if slot and not re.fullmatch(r'[1-9][0-9]*', slot):
            raise HistoryError('slot must be a positive integer')
        return repo, slot or ''

    def identity(self, alias):
        remote = run(['git', '-C', self.cfg.repos[alias], 'remote', 'get-url', 'origin'], optional=True).strip()
        return slug(remote) or 'local:' + os.path.realpath(self.cfg.repos[alias])

    def ingest(self, value, alias, source=None, host='local'):
        value = dict(value)
        value.pop('_scan', None)
        identity = self.discovery_identities.get(alias) or self.identity(alias)
        if value.get('recorded_repo') and not self.same_repository(value['recorded_repo'], identity):
            return
        if not identity.startswith('local:'):
            value['prs'] = value.get('prs', []) + [dict(repo=n.get('repo') or identity, number=n['number'], url=f'https://github.com/{n.get("repo") or identity}/pull/{n["number"]}') for n in value.pop('pr_numbers', [])]
        associations = []
        for original in value.get('associations', []):
            association = dict(original)
            slot = association_matches(association, self.cfg, alias)
            if slot is None:
                if value.get('agent') != 'codex' or not self.same_repository(value.get('recorded_repo', ''), identity):
                    continue
                slot = recorded_slot(association.get('workspace', ''))
            association.update(slot=slot, host=host)
            associations.append(association)
        if not associations:
            return
        value.update(repo=identity, associations=associations)
        if source:
            value['sources'] = [dict(host=host, path=str(source), available=Path(source).is_file(), updated=value.get('updated') or Path(source).stat().st_mtime, size=Path(source).stat().st_size)]
            value['updated'] = max(value.get('updated', 0), Path(source).stat().st_mtime)
        try:
            self.store.merge(value)
        except HistoryError:
            pass

    def discover(self, alias, *, metadata_only=False):
        identity = self.canonical_identity(self.identity(alias))
        self.discovery_identities[alias] = identity
        home = Path.home()
        codex = Path(os.environ.get('CODEX_HOME') or home / '.codex')
        for directory, pattern, agent in [(home / '.claude/projects', '*/*.jsonl', 'claude'),
                                           (codex / 'sessions', '**/rollout-*.jsonl', 'codex'),
                                           (codex / 'archived_sessions', '**/rollout-*.jsonl', 'codex')]:
            for path in directory.glob(pattern):
                if agent == 'claude':
                    encoded_roots = [re.sub(r'[^A-Za-z0-9]', '-', self.cfg.repos[alias]), re.sub(r'[^A-Za-z0-9]', '-', str(Path(self.cfg.worktree_root) / Path(self.cfg.repos[alias]).name))]
                    if not any(path.parent.name == root or path.parent.name.startswith(root + '-') for root in encoded_roots):
                        continue
                else:
                    try:
                        with path.open() as stream:
                            first = json.loads(stream.readline())
                        payload = first.get('payload', {})
                        if payload.get('thread_source') == 'subagent' or isinstance(payload.get('source'), dict) and 'subagent' in payload['source']:
                            continue
                        cwd = payload.get('cwd', '')
                        git = first.get('payload', {}).get('git') or {}
                        recorded_repo = slug(git.get('repository_url', '')) if isinstance(git, dict) else None
                        if association_matches(dict(workspace=cwd), self.cfg, alias) is None and not self.same_repository(recorded_repo or '', identity):
                            continue
                    except (OSError, ValueError, AttributeError):
                        continue
                try:
                    value = self.store.scan(path, agent, metadata_only=True) if metadata_only else self.store.scan(path, agent)
                except (HistoryError, OSError):
                    continue
                if value.get('sid'):
                    if directory == codex / 'archived_sessions':
                        value['archived'] = True
                    self.ingest(value, alias, path)
        for database in sorted(codex.glob('state_*.sqlite'), reverse=True):
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                    db.row_factory = sqlite3.Row
                    for row in db.execute('select * from threads'):
                        row = dict(row)
                        if '"subagent"' in str(row.get('source', '')):
                            continue
                        value = dict(agent='codex', sid=row.get('id', ''), title=clean(row.get('name') or row.get('title') or row.get('first_user_message')), summary=clean(row.get('name') or row.get('title') or row.get('first_user_message')), summary_source='index', model=clean(row.get('model')) or '-', model_source='index' if row.get('model') else 'unknown', updated=timestamp(row.get('updated_at')), archived=bool(row.get('archived')), associations=[dict(workspace=row.get('cwd', ''), branch=clean(row.get('git_branch')), commit=row.get('git_sha') or '', evidence='index', observed=timestamp(row.get('updated_at')))], sources=[dict(host='local', path=row.get('rollout_path') or '', available=Path(row.get('rollout_path') or '').is_file())])
                        self.ingest(value, alias)
                break
            except sqlite3.Error:
                continue

    def export(self, alias):
        rows = []
        for row in self.records(alias):
            row['associations'] = [a for a in row['associations'] if a.get('host') == 'local']
            row['sources'] = [source for source in row['sources'] if source.get('host') == 'local']
            if row['associations']:
                rows.append(row)
        return rows

    def refresh(self, alias, hosts=None):
        identity = self.record_identity(alias)
        targets = set()
        for host, target in (self.cfg.hosts if hosts is None else hosts).items():
            if target in targets:
                continue
            targets.add(target)
            try:
                # Metadata only: no transcript transfer or implicit remote calls on browse.
                text = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', target,
                            'zsh -lic ' + shlex.quote('command t __history-export ' + shlex.quote(identity))], timeout=20)
                rows = json.loads(text)
                if not isinstance(rows, list):
                    raise HistoryError('invalid remote history')
                for row in rows:
                    if not self.same_repository(row.get('repo', ''), identity):
                        continue
                    row = dict(row)
                    validate(row)
                    row['repo'] = identity
                    row.pop('owners', None)
                    for field in ('associations', 'sources'):
                        row[field] = [{**value, 'host': host} for value in row.get(field, [])]
                    self.store.merge(row)
            except (HistoryError, ValueError, TypeError, AttributeError):
                print(f't history: {host} unavailable; retained cached history', file=sys.stderr)

    def owners(self, all_hosts=False):
        snippet = '_DEV_PS_AT=0; _DEV_PS_COMM=(); ' + ('_dev_rows_all --best-effort' if all_hosts else "_dev_session_rows --details | awk 'NF {print \"local\\t\" $0}'")
        text = run(self.shell(snippet))
        return self.api['_parse_rows'](text, host_prefixed=True)

    def records(self, alias, slot='', owners=None):
        identity = self.record_identity(alias) if alias in self.cfg.repos else alias
        rows = []
        for value in self.store.records():
            if not self.same_repository(value['repo'], identity):
                continue
            row = dict(value)
            row['repo'] = identity
            row.setdefault('title', '')
            row['owners'] = [owner for owner in owners or [] if owner['sid'] == row['sid'] and owner['agent'] == row['agent'] and (alias in self.cfg.repos and association_matches(dict(workspace=owner.get('cwd', '')), self.cfg, alias) is not None or any(a.get('host') == owner.get('host') and os.path.normpath(a['workspace']) == os.path.normpath(owner.get('cwd') or '') for a in row['associations']))]
            if len(row['owners']) == 1:
                owner = row['owners'][0]
                metadata = {}
                if owner.get('model') not in (None, '', '-'):
                    metadata.update(model=clean(owner['model']), model_source='exact owner', model_updated=time.time())
                if owner.get('summary'):
                    metadata.update(summary=clean(owner['summary']), summary_source='exact owner', summary_updated=time.time())
                metadata['interface_updated'] = time.time()
                metadata['interface'] = 'desktop' if owner.get('state') == 'app' else 'foreground' if ':' in owner.get('slot', '') else 'cli'
                row.update(metadata)
                self.store.merge({**value, **metadata})
            row['associations'] = [{**a, 'available': Path(a['workspace']).is_dir() if a.get('host') == 'local' else 'cached'} for a in row['associations']]
            row['sources'] = [{**s, 'available': Path(s['path']).is_file() if s.get('host') == 'local' else 'cached'} for s in row['sources']]
            row['display'] = history_display(row, alias)
            if not slot or any(a.get('slot') == slot for a in row['associations']):
                rows.append(row)
        merged = {}
        for row in rows:
            key = row['agent'], row['sid']
            if key in merged:
                previous = merged[key]
                newer = row if row.get('updated', 0) >= previous.get('updated', 0) else previous
                combined = {**previous, **newer}
                for field in ('associations', 'sources', 'prs', 'mentions'):
                    combined[field] = list({json.dumps(value, sort_keys=True): value for value in previous.get(field, []) + row.get(field, [])}.values())
                merged[key] = combined
            else:
                merged[key] = row
        return list(merged.values())

    def branch_prs(self, rows, refresh=False):
        grouped = {}
        for row in rows:
            for association in row['associations']:
                branch, commit = association.get('branch'), association.get('commit')
                if branch and re.fullmatch(r'[a-fA-F0-9]{40,64}', str(commit)) and not row['repo'].startswith('local:'):
                    grouped.setdefault(row['repo'], set()).add(branch)
        for repo, branches in grouped.items():
            key = 'branch-pr:' + repo + ':' + hashlib.sha256(json.dumps(sorted(branches)).encode()).hexdigest()
            existing = self.store.db.execute('select checked,record from prs where key=?', (key,)).fetchone()
            prs = json.loads(existing[1]) if existing else []
            if refresh:
                owner, name = repo.split('/', 1)
                fields = ' '.join(f'b{i}:pullRequests(headRefName:{json.dumps(branch)},first:100,states:[OPEN,CLOSED,MERGED]){{nodes{{number url headRefName headRefOid state}}}}' for i, branch in enumerate(sorted(branches)))
                query = 'query{repository(owner:' + json.dumps(owner) + ',name:' + json.dumps(name) + '){' + fields + '}}'
                try:
                    data = json.loads(run(['gh', 'api', 'graphql', '-f', 'query=' + query], timeout=8))['data']['repository']
                    prs = [dict(repo=repo, **pr) for batch in data.values() for pr in batch['nodes']]
                    self.store.db.execute('insert or replace into prs values (?,?,?)', (key, time.time(), json.dumps(prs)))
                    self.store.db.commit()
                except (HistoryError, ValueError, KeyError, TypeError):
                    pass
            for row in rows:
                evidence = {(a.get('branch'), a.get('commit')) for a in row['associations']}
                associated = [dict(repo=pr['repo'], number=pr['number'], url=pr['url'], state=pr['state'], evidence='verified branch and commit') for pr in prs if pr['repo'] == row['repo'] and (pr['headRefName'], pr['headRefOid']) in evidence]
                if associated:
                    row.update(self.store.merge({**row, 'prs': row['prs'] + associated}))

    def pr_states(self, rows, refresh=False):
        refs = {(p['repo'], p['number']) for row in rows for p in row['prs']}
        pending, cached = {}, {}
        for repo, number in refs:
            key = f'{repo}#{number}'
            found = self.store.db.execute('select checked,record from prs where key=?', (key,)).fetchone()
            if found:
                cached[(repo, number)] = json.loads(found[1])
            if refresh:
                pending.setdefault(repo, []).append(number)
        for repo, numbers in pending.items():
            # One GraphQL request per repository, bounded batches, all selected PRs.
            owner, name = repo.split('/', 1)
            for start in range(0, len(numbers), 50):
                batch = numbers[start:start + 50]
                fields = ' '.join(f'p{n}:pullRequest(number:{n}){{state mergedAt}}' for n in batch)
                query = 'query{repository(owner:' + json.dumps(owner) + ',name:' + json.dumps(name) + '){' + fields + '}}'
                try:
                    data = json.loads(run(['gh', 'api', 'graphql', '-f', 'query=' + query], timeout=8))['data']['repository']
                    for number in batch:
                        value = data.get(f'p{number}')
                        if not value:
                            continue
                        status = dict(state='MERGED' if value.get('mergedAt') else value['state'])
                        cached[(repo, number)] = status
                        self.store.db.execute('insert or replace into prs values (?,?,?)', (f'{repo}#{number}', time.time(), json.dumps(status)))
                    self.store.db.commit()
                except (HistoryError, ValueError, TypeError, KeyError):
                    pass
        for row in rows:
            row['prs'] = [{**p, **cached.get((p['repo'], p['number']), {})} for p in row['prs']]

    def pick(self, rows, alias, limit=50, page=1, query=''):
        with tempfile.TemporaryDirectory(prefix='t-history-') as directory:
            snapshot, state = Path(directory) / 'records.json', Path(directory) / 'page.json'
            private_write(snapshot, rows)
            terminal = shutil.get_terminal_size((80, 24))
            width = max(12, terminal.columns - 12)
            private_write(state, dict(page=page, query=query, limit=limit, width=width))
            helper = shlex.join([sys.executable, str(Path(self.root) / 'libexec/t_history.py'), 'picker', str(snapshot), str(state)])
            header = state.with_suffix('.header')
            _, total = page_rows(rows, query, limit, page)
            header.write_text(picker_header(rows, query, limit, page, width))
            rows_file = state.with_suffix('.rows')
            rows_file.write_text(picker_rows(rows, query, limit, page, width))
            snippet = '. "$1"; shift; _t_fzf_paginated "$@"'
            result = subprocess.run(['sh', '-c', snippet, 't-history', str(Path(self.root) / 'ui/fzf.sh'),
                    str(header), str(rows_file), helper, helper + ' details {1}', query,
                    'history ' + alias + '> '],
                    stdout=subprocess.PIPE, text=True,
                    env={k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')})
            if result.returncode in (1, 130):
                return None, 'cancel', query
            if result.returncode:
                raise HistoryError('history picker unavailable; install fzf')
            lines = result.stdout.splitlines()
            query, key = lines[:2] if len(lines) >= 2 else ('', '')
            if key == 'ctrl-r':
                return None, key, query
            if len(lines) < 3:
                return None, 'cancel', query
            index = int(lines[2].split('\t')[0])
            if not 0 <= index < len(rows):
                raise HistoryError('invalid selection')
            settings = json.loads(state.read_text())
            matching, _ = page_rows(rows, query, limit, settings['page'])
            selected = rows[index]
            expected = f'{index}\t{format_row(selected, settings.get("width", 80))}\t{clean(json.dumps(selected, ensure_ascii=False))}'
            if settings['query'] != query or selected not in matching or lines[2] != expected:
                return None, 'retry', query
            return selected, key, query

    def choose_slot(self, rows, alias):
        slots = sorted({a.get('slot') for row in rows for a in row['associations'] if a.get('slot')}, key=int)
        if not slots:
            return ''
        if len(slots) == 1:
            return slots[0]
        options = [dict(repo=self.identity(alias), sid=slot, agent='slot', title=f'Slot {slot}', updated=max((r['updated'] for r in rows if any(a.get('slot') == slot for a in r['associations'])), default=0), associations=[], sources=[], prs=[], mentions=[]) for slot in slots]
        selected, _, _ = self.pick(options, alias + ' · choose slot')
        return selected['sid'] if selected else None

    def revision(self, row):
        # Only commit evidence recorded with this conversation is safe across slot reuse.
        associations = sorted(row['associations'], key=lambda a: a.get('observed', 0), reverse=True)
        for association in associations:
            commit = association.get('commit', '')
            if re.fullmatch(r'[a-fA-F0-9]{40,64}', str(commit)):
                return association, commit
        return associations[0] if associations else {}, ''

    def source(self, row):
        for source in sorted(row['sources'], key=lambda source: (source.get('updated', 0), source.get('size', 0)), reverse=True):
            if source.get('host') == 'local' and Path(source.get('path', '')).is_file():
                value = self.store.scan(Path(source['path']), row['agent'])
                if self.verify_source(value, row):
                    return Path(source['path'])
            if source.get('host') in self.cfg.hosts and source.get('path', '').startswith('/'):
                basename = Path(source['path']).name
                if row['agent'] == 'codex' and (not basename.startswith('rollout-') or row['sid'] not in basename):
                    raise HistoryError('remote Codex rollout filename does not match its identity')
                destination = self.store.directory / 'transcripts' / row['agent'] / basename
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                remote = self.cfg.hosts[source['host']] + ':' + source['path']
                try:
                    run(['rsync', '-a', '--protect-args', '--timeout=10', '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=3', remote, str(destination)])
                except HistoryError:
                    # A cached validated copy remains usable while its host is offline.
                    if destination.is_file():
                        if not self.verify_source(self.store.scan(destination, row['agent']), row):
                            raise HistoryError('cached transcript identity changed; no session started')
                        return destination
                    continue
                if not self.verify_source(self.store.scan(destination, row['agent']), row):
                    raise HistoryError('selected transcript identity changed; no session started')
                return destination
        raise HistoryError('selected transcript is missing; retained history is still available')

    def verify_source(self, value, row):
        if value['sid'] != row['sid'] or value.get('agent') != row['agent']:
            return False
        if value.get('recorded_repo') and not self.same_repository(value['recorded_repo'], row['repo']):
            return False
        workspaces = {a['workspace'] for a in row['associations']}
        return any(a['workspace'] in workspaces for a in value.get('associations', []))

    def attach(self, owner):
        if owner.get('state') == 'app':
            raise HistoryError('conversation is owned by the desktop app on ' + owner['host'] + '; close and release it there, then use t app pull for the verified handoff')
        if ':' in owner['slot']:
            raise HistoryError('conversation is already running in foreground ' + owner['slot'] + ' on ' + owner['host'] + '; return to that terminal or use t session push there for an explicit handoff')
        # Attach-only helper rechecks exact ID/path/agent and has no launch fallback.
        snippet = '_t_history_attach ' + shlex.join([owner['slot'], owner['sid'], owner['cwd'], owner['agent']])
        if owner['host'] == 'local':
            return subprocess.call(self.shell(snippet))
        return subprocess.call(['ssh', '-t', self.cfg.hosts[owner['host']], 'zsh -lic ' + shlex.quote(snippet)])

    def fallback(self, alias):
        options = [dict(repo=self.identity(alias), sid=name, agent='revision', title=title, updated=0, associations=[], sources=[], prs=[], mentions=[]) for name, title in [('main', 'Historical revision unavailable: start from current main'), ('cancel', 'Cancel and keep history available')]]
        selected, _, _ = self.pick(options, alias + ' · revision unavailable')
        if not selected or selected['sid'] != 'main':
            raise HistoryCancelled('resume cancelled; history and workspaces retained')
        directory = self.cfg.repos[alias]
        run(['git', '-C', directory, 'fetch', 'origin', 'main'])
        return run(['git', '-C', directory, 'rev-parse', 'origin/main^{commit}']).strip()

    def destination(self, alias, row, revision, owners):
        if not self.worktree_enabled(alias):
            raise HistoryError('repository opts out of worktree-per-session; no workspace was created')
        directory = self.cfg.repos[alias]
        base = Path(directory).name
        root = Path(self.cfg.worktree_root) / base
        association, recorded = self.revision(row)
        historical_slot = association.get('slot', '')
        # A slot number does not identify its layout. Reuse only its recorded path.
        workspace = Path(association.get('workspace') or '/')
        candidates = (root / historical_slot, root / f'{base}-{historical_slot}') if historical_slot else ()
        historical_path = workspace if workspace in candidates else None
        if any(os.path.lexists(p) for p in candidates if p != workspace):
            historical_path = None
        busy = {os.path.realpath(owner['cwd']) for owner in owners}
        latest = []
        if historical_path:
            for conversation in self.store.records():
                for a in conversation.get('associations', []):
                    if a.get('host') == 'local' and os.path.realpath(a['workspace']) == os.path.realpath(historical_path) and a.get('evidence') in ('launch', 'resume', 'close', 'handoff'):
                        latest.append((a.get('observed', 0), conversation['sid'], conversation['agent']))
        current_identity = max(latest, default=(0, '', ''))[1:]
        # Reuse only when both revision and provenance match, never by branch name alone.
        if historical_path and association.get('generation') and association['generation'] == generation(historical_path) and current_identity == (row['sid'], row['agent']) and recorded == revision and association.get('host') == 'local' and os.path.realpath(association.get('workspace', '')) == os.path.realpath(historical_path) and historical_path.is_dir() and os.path.realpath(historical_path) not in busy:
            head = run(['git', '-C', str(historical_path), 'rev-parse', 'HEAD'], optional=True).strip()
            actual = run(['git', '-C', str(historical_path), 'branch', '--show-current'], optional=True).strip()
            if head == revision and actual == association.get('branch'):
                self.reserve(historical_path)
                return str(historical_path), historical_slot, False
        # Fresh allocation skips every existing branch/worktree and every owner.
        for slot in range(1, 10001):
            path, branch = root / f'{base}-{slot}', f'dev/{base}-{slot}'
            legacy = root / str(slot)
            if any(os.path.lexists(p) for p in (path, legacy, root / f'.{slot}.history-reservation')) or any(owner['slot'] == f'{alias}-{slot}' for owner in owners):
                continue
            if run(['git', '-C', directory, 'rev-parse', '--verify', 'refs/heads/' + branch], optional=True) or run(['git', '-C', directory, 'rev-parse', '--verify', 'refs/remotes/origin/' + branch], optional=True):
                continue
            root.mkdir(parents=True, exist_ok=True)
            marker = root / f'.{path.name}.history-reservation'
            try:
                with marker.open('x') as stream:
                    os.chmod(marker, 0o600)
                    stream.write(self.reservation_token)
            except FileExistsError:
                continue
            self.reservation = marker
            run(['git', '-C', directory, 'worktree', 'add', '-b', branch, str(path), revision])
            return str(path), str(slot), True
        raise HistoryError('no free slot is available')

    @contextmanager
    def launch_claims(self, destination, sid):
        """Speak zsystem flock's POSIX protocol across restore and claimed launch."""
        cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'claude-sessions'
        held = []
        try:
            for directory, identity in [('app-handoffs', os.path.realpath(destination)), ('launch-sids', sid)]:
                path = cache / directory / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                stream = path.open('a')
                os.chmod(path, 0o600)
                held.append(stream)
                deadline = time.monotonic() + 15
                with _spinner("Waiting for conversation ownership"):
                    while True:
                        try:
                            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise HistoryError('timed out waiting for conversation/workspace ownership claim')
                            time.sleep(.05)
            yield
        finally:
            for stream in reversed(held):
                stream.close()

    def reserve(self, path):
        marker = Path(path).parent / f'.{Path(path).name}.history-reservation'
        try:
            with marker.open('x') as stream:
                os.chmod(marker, 0o600)
                stream.write(self.reservation_token)
        except FileExistsError as error:
            raise HistoryError('workspace is being prepared by another history action') from error
        self.reservation = marker

    def codex_released(self, row, source, *, workspace=None):
        if row['agent'] != 'codex':
            return
        value = transcript(source, 'codex')
        helper = runpy.run_path(str(Path(self.root) / 'libexec/t_app_handoff.py'))
        try:
            helper['assert_released']('Codex', row['sid'], value['associations'][0]['workspace'], self.api['_run'],
                ownership_cwd=workspace,
                codex_home=str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')),
                rollout=str(source), archived=bool(row.get('archived')) and source.resolve().is_relative_to((Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'archived_sessions').resolve()),
                retry='Retry: t session history, then select the conversation again.')
        except ValueError as error:
            raise HistoryError(str(error)) from error

    def codex_target(self, row, default):
        codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        for database in sorted(codex.glob('state_*.sqlite'), reverse=True):
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                    record = db.execute('select rollout_path from threads where id=?', (row['sid'],)).fetchone()
                if record and record[0] and Path(record[0]).is_file():
                    value = transcript(Path(record[0]), 'codex')
                    if self.verify_source(value, row):
                        return Path(record[0])
            except sqlite3.Error:
                continue
        for source in row['sources']:
            path = Path(source.get('path', ''))
            if source.get('host') == 'local' and path.is_file() and self.verify_source(transcript(path, 'codex'), row):
                return path
        return default

    @contextmanager
    def native_writer_claim(self, sid):
        folder = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'thread-writer-locks'
        if not folder.is_dir() or folder.is_symlink():
            raise HistoryError('cannot safely extend a canonical Codex rollout without its native writer-lock directory')
        path = folder / (sid + '.lock')
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise HistoryError('Codex claimed the selected writer before restore; no transcript changed') from error
            inode, current = os.fstat(descriptor), path.lstat()
            if (inode.st_dev, inode.st_ino) != (current.st_dev, current.st_ino):
                raise HistoryError('Codex writer lock changed before restore')
            yield
        finally:
            os.close(descriptor)

    def publish_transcript(self, target, data, row):
        if row['agent'] == 'codex' and target.exists() and target.read_bytes() != data:
            with self.native_writer_claim(row['sid']):
                self._publish_transcript(target, data, row)
        else:
            self._publish_transcript(target, data, row)

    def _publish_transcript(self, target, data, row):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            before = target.lstat()
            if target.is_symlink():
                raise HistoryError('destination transcript is a symlink')
            original = target.read_bytes()
            if original == data:
                return
            if row['agent'] != 'codex' or not data.startswith(original):
                raise HistoryError('destination transcript differs; no transcript overwritten')
            # Verified append-only remote updates keep the exact local rollout path
            # used by Codex's index, and retain its original bytes privately.
            backup = self.store.directory / 'transcript-backups' / (row['sid'] + '.' + hashlib.sha256(original).hexdigest() + '.jsonl')
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not backup.exists():
                with backup.open('xb') as stream:
                    os.chmod(backup, 0o600)
                    stream.write(original)
            current = target.lstat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (current.st_ino, current.st_size, current.st_mtime_ns):
                raise HistoryError('destination transcript changed during restore')
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                pending = Path(stream.name)
                try:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.replace(pending, target)
                finally:
                    pending.unlink(missing_ok=True)
        else:
            with target.open('xb') as stream:
                os.chmod(target, 0o600)
                stream.write(data)

    def install_transcript(self, row, source, destination):
        before = source.lstat()
        data = source.read_bytes()
        if source.is_symlink() or not self.verify_source(transcript(source, row['agent']), row):
            raise HistoryError('selected transcript changed or is a symlink; no session started')
        after = source.lstat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or source.read_bytes() != data:
            raise HistoryError('selected transcript changed during verification; no session started')
        if row['agent'] == 'codex':
            codex_home = str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
            if row.get('archived'):
                helper = runpy.run_path(str(Path(self.root) / 'libexec/t_app_handoff.py'))
                value = self.store.scan(source, 'codex')
                cwd = value['associations'][0]['workspace']
                # The validated stdio unarchive contract requires its original cwd.
                # A removed workspace may be materialized separately, never rewritten.
                archive = Path(codex_home) / 'archived_sessions'
                archived_target = self.codex_target(row, archive / source.name)
                if not archived_target.resolve().is_relative_to(archive.resolve()):
                    archived_target = archive / source.name
                self.publish_transcript(archived_target, data, row)
                source = archived_target
                helper['unarchive_thread'](row['sid'], cwd, codex_home=codex_home, rollout=str(source), process_cwd=destination)
                return source
            target = self.codex_target(row, Path(codex_home) / 'sessions/history' / source.name)
        else:
            encoded = re.sub(r'[^A-Za-z0-9]', '-', destination)
            target = Path.home() / '.claude/projects' / encoded / (row['sid'] + '.jsonl')
        if source.resolve() != target.resolve():
            self.publish_transcript(target, data, row)
        return target

    def resume_source(self, request):
        return _RESUME_SOURCE['export'](self, request)

    def reconcile_source(self, alias, row):
        return _RESUME_SOURCE['reconcile'](self, alias, row)

    def resume(self, alias, row, *, detached=False, prepare_only=False):
        owners = self.owners(True)
        matches = [o for o in owners if o['sid'] == row['sid'] and o['agent'] == row['agent']]
        if len(matches) > 1:
            raise HistoryError('conversation has multiple reported owners; resolve ownership before resuming')
        if matches and detached:
            raise HistoryError('conversation became live; select its current owner again')
        if matches:
            return self.attach(matches[0])
        destination_alias = alias if alias in self.cfg.repos else self.repository_alias(row['repo'])
        if not self.worktree_enabled(destination_alias):
            raise HistoryError('repository opts out of worktree-per-session; no saved conversation resumed')
        self.strict_identity = True
        source = self.source(row)
        alias = self.materialize_repository(row['repo']) if alias not in self.cfg.repos else alias
        if not self.same_repository(self.identity(alias), row['repo']):
            raise HistoryError('repository identity changed before resume')
        association, revision = self.revision(row)
        revision = self.reconcile_source(alias, row) or revision
        if not revision or run(['git', '-C', self.cfg.repos[alias], 'rev-parse', '--verify', revision + '^{commit}'], optional=True).strip() != revision:
            revision = self.fallback(alias)
        common = run(['git', '-C', self.cfg.repos[alias], 'rev-parse', '--git-common-dir']).strip()
        common = Path(self.cfg.repos[alias]) / common
        with (common / 't-issue.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            owners = self.owners(True)
            matches = [o for o in owners if o['sid'] == row['sid'] and o['agent'] == row['agent']]
            if matches:
                raise HistoryError('conversation became live while selecting; rerun history to attach to its owner')
            try:
                destination, slot, created = self.destination(alias, row, revision, owners)
                print(f'Resume {row["agent"]} {row["sid"]} in {destination} at {revision}', file=sys.stderr)
                with self.launch_claims(destination, row['sid']):
                    final_owners = self.owners(True)
                    if any(owner['sid'] == row['sid'] or os.path.realpath(owner['cwd']) == os.path.realpath(destination) for owner in final_owners):
                        raise HistoryError('conversation or workspace became live before restore; reopen history')
                    if not created and association.get('generation') != generation(destination):
                        raise HistoryError('workspace generation changed before restore')
                    actual = run(['git', '-C', destination, 'rev-parse', 'HEAD']).strip()
                    if actual != revision:
                        raise HistoryError('destination revision changed before restore; no conversation launched')
                    if not self.same_repository(self.identity(alias), row['repo']):
                        raise HistoryError('repository identity changed before restore')
                    self.codex_released(row, source)
                    self.install_transcript(row, source, destination)
                    # Python holds the same POSIX cwd/SID locks as the zsh wrapper;
                    # use the claimed helper, never recursively acquire those locks.
                    snippet = '_T_RESUME_ALLOW_UNAVAILABLE=1 T_HISTORY_TOKEN=' + shlex.quote(self.reservation_token) + ' _dev_resume_session_claimed ' + shlex.join([f'dev-{alias}-{slot}', destination, row['sid'], row['agent'], 'history'])
                    if not prepare_only:
                        run(self.shell(snippet))
                self.store.merge({**row, 'associations': [dict(workspace=destination, host='local', slot=slot, commit=revision, branch=run(['git', '-C', destination, 'branch', '--show-current']).strip(), generation=generation(destination), evidence='resume', observed=time.time())]})
            finally:
                if self.reservation:
                    self.reservation.unlink(missing_ok=True)
                    self.reservation = None
        if prepare_only:
            return f'dev-{alias}-{slot}\t{destination}'
        if detached:
            return f'dev-{alias}-{slot}'
        return self.attach(dict(host='local', slot=f'{alias}-{slot}', sid=row['sid'], agent=row['agent'], cwd=destination, state='active'))

    def resume_candidates(self, aliases):
        """Private adapter: display aliases and immutable selection keys differ."""
        all_repositories = aliases == ['--all'] or not aliases
        if all_repositories:
            identities = list(self.discover_catalog())
        else:
            identities = []
            catalog = None
            for alias in aliases:
                if alias in self.cfg.repos:
                    self.discover(alias, metadata_only=True)
                    self.refresh(alias)
                    identities.append(self.record_identity(alias))
                elif REPO_IDENTITY.fullmatch(alias):
                    if catalog is None:
                        catalog = self.discover_catalog()
                    identities.extend(identity for identity in catalog if self.same_repository(identity, alias))
                else:
                    raise HistoryError('unknown repository: ' + alias)
        seen = set()
        for identity in identities:
            local_alias = self.repository_alias(identity)
            if not self.worktree_enabled(local_alias):
                continue
            alias = local_alias or identity
            for row in self.records(alias):
                key = (identity, row['agent'], row['sid'])
                if key in seen or not row.get('title') or not row.get('sources') or not row.get('associations'):
                    continue
                seen.add(key)
                association = max(row['associations'], key=lambda a: a.get('observed', 0))
                source = max(row['sources'], key=lambda s: (s.get('updated', 0), s.get('size', 0)))
                updated = int(row.get('updated', 0))
                when = datetime.datetime.fromtimestamp(updated).strftime('%b %d %H:%M')
                yield '\t'.join(map(str, [updated, alias, association.get('slot') or '-', row['sid'],
                    association['workspace'], when, clean(row['title']), source.get('host') or '-', row['agent'], row.get('last_turn', '-'), identity]))

    def execute(self, args):
        alias, slot = self.scope(args.target)
        with self.api.get("_spinner", lambda *a, **kw: nullcontext())("Loading conversation history", machine=args.json):
            self.discover(alias)
            if args.refresh:
                self.refresh(alias)
            try:
                owners = self.owners()
            except HistoryError:
                owners = []
        rows = self.records(alias, slot, owners)
        tty = sys.stdin.isatty() and sys.stdout.isatty()
        if not slot and tty and not args.json:
            slot = self.choose_slot(rows, alias)
            if slot is None:
                return 130
            rows = self.records(alias, slot, owners)
        self.branch_prs(rows, refresh=args.refresh)
        self.pr_states(rows, refresh=args.refresh)
        for row in rows:
            row['display'] = history_display(row, alias)
        selected, total = page_rows(rows, '', args.limit, args.page)
        if args.json:
            print(json.dumps(dict(version=VERSION, repo=self.identity(alias), slot=slot, page=args.page, limit=args.limit, total=total, conversations=selected)))
            return 0
        if not tty:
            if selected:
                print(f'history   {alias}' + (f' slot {slot}' if slot else ' · all slots'))
                print('\n'.join(history_table(selected, self.api, alias)))
            else:
                print('No retained conversations for this slot.')
            print(f'Page {args.page} · {total} conversations · {args.limit} per page')
            return 0
        query = ''
        while rows:
            row, key, query = self.pick(rows, alias + (f' {slot}' if slot else ''), args.limit, args.page, query)
            if key == 'retry':
                continue
            if key == 'ctrl-r':
                self.refresh(alias)
                rows = self.records(alias, slot, owners)
                self.branch_prs(rows, refresh=True)
                self.pr_states(rows, refresh=True)
                continue
            if not row:
                return 130
            try:
                return self.resume(alias, row)
            except HistoryCancelled:
                return 130
            except (HistoryError, OSError, ValueError) as error:
                print(f't history: {error}', file=sys.stderr)
                rows = self.records(alias, slot, owners)
                self.pr_states(rows)
        print('No retained conversations for this slot.')
        return 0


def picker_rows(rows, query, limit, page, width=80):
    selected, total = page_rows(rows, query, limit, page)
    indices = {json.dumps([row['repo'], row['agent'], row['sid']]): i for i, row in enumerate(rows)}
    return ''.join(f'{indices[json.dumps([row["repo"], row["agent"], row["sid"]])]}\t{format_row(row, width)}\t{clean(json.dumps(row, ensure_ascii=False))}\n' for row in selected)


def picker(argv):
    snapshot, state, action, value = argv
    rows = json.loads(Path(snapshot).read_text())
    settings = json.loads(Path(state).read_text())
    if action == 'details':
        print('Details · Shift ↑↓')
        print(details(rows[int(value)], width=max(8, int(os.environ.get('FZF_PREVIEW_COLUMNS') or 80) - 3), compact=0 < int(os.environ.get('FZF_PREVIEW_LINES') or 0) <= 3))
        return
    if settings['query'] != value:
        settings.update(query=value, page=1)
    _, total = page_rows(rows, value, settings['limit'], 1)
    maximum = max(1, (total + settings['limit'] - 1) // settings['limit'])
    if action == 'next':
        settings['page'] = min(maximum, settings['page'] + 1)
    elif action == 'previous':
        settings['page'] = max(1, settings['page'] - 1)
    private_write(state, settings)
    Path(state).with_suffix('.header').write_text(picker_header(rows, value, settings['limit'], settings['page'], settings.get('width', 80)))
    output = picker_rows(rows, value, settings['limit'], settings['page'], settings.get('width', 80))
    Path(state).with_suffix('.rows').write_text(output)
    print(output, end='')


def record_association(sid, agent, workspace, transcript_path='', event='launch', interface='unknown'):
    """Hook and lifecycle seam: capture immutable revision evidence when it occurs."""
    if not SID.fullmatch(sid) or agent not in ('claude', 'codex'):
        return
    remote = run(['git', '-C', workspace, 'remote', 'get-url', 'origin'], optional=True).strip()
    repo = slug(remote)
    if not repo:
        common = run(['git', '-C', workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir'], optional=True).strip()
        if not common:
            return
        repo = 'local:' + str(Path(common).parent.resolve())
    store = Store()
    try:
        saved = store.scan(Path(transcript_path), agent) if transcript_path and Path(transcript_path).is_file() else None
        if saved and saved['sid'] != sid:
            return
        commit = run(['git', '-C', workspace, 'rev-parse', 'HEAD'], optional=True).strip()
        branch = run(['git', '-C', workspace, 'branch', '--show-current'], optional=True).strip()
        leaf = Path(workspace).name.removeprefix(Path(workspace).parent.name + '-')
        slot = leaf if re.fullmatch(r'[0-9]+', leaf) else ''
        value = dict(repo=repo, agent=agent, sid=sid, interface=interface, interface_updated=time.time(), updated=time.time(), associations=[dict(workspace=workspace, slot=slot, host='local', branch=branch, commit=commit, generation=generation(workspace), evidence=event, observed=time.time())])
        if saved:
            value.update({key: saved[key] for key in ('title', 'summary', 'summary_source', 'model', 'model_source', 'model_updated', 'summary_updated') if key in saved})
        if transcript_path:
            value['sources'] = [dict(host='local', path=transcript_path, updated=timestamp(time.time()))]
        store.merge(value)
    finally:
        store.close()


def main(argv):
    if argv[0] == 'picker':
        picker(argv[1:])
    elif argv[0] == 'record':
        record_association(*argv[1:])


if __name__ == '__main__':
    main(sys.argv[1:])
