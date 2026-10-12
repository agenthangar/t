"""Private, durable conversation history and guarded exact-conversation resume."""
from contextlib import closing, contextmanager
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
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = 1
PARSER_VERSION = 3
SID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z')
PR_URL = re.compile(r'https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)')


class HistoryError(ValueError):
    pass


class HistoryCancelled(HistoryError):
    """The user left a history selection without choosing work."""


def clean(value):
    return ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', str(value or '')).split())


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
        if not record.get('title') and old.get('title'):
            merged['title'] = old['title']
        for field in ('associations', 'prs', 'mentions', 'sources'):
            values = old.get(field, []) + record.get(field, [])
            key_for = (lambda value: (value['repo'], value['number'])) if field in ('prs', 'mentions') else (lambda value: (value.get('host'), value.get('path'))) if field == 'sources' else (lambda value: json.dumps(value, sort_keys=True))
            unique = {}
            for value in values:
                identity = key_for(value)
                unique[identity] = {**unique.get(identity, {}), **value} if field == 'sources' else value
            merged[field] = list(unique.values())
        merged['updated'] = max(old.get('updated', 0), record.get('updated', 0))
        self.db.execute('insert or replace into conversations values (?,?)', (key, json.dumps(merged)))
        self.db.commit()
        return merged

    def scan(self, path, agent):
        info = path.stat()
        stamp = json.dumps([info.st_ino, info.st_size, info.st_mtime_ns])
        old = self.db.execute('select stamp,record from scans where path=?', (str(path),)).fetchone()
        cached = json.loads(old[1]) if old else None
        if old and old[0] == stamp and cached.get('_scan', {}).get('parser') == PARSER_VERSION:
            return cached
        # Append-only transcripts are rescanned only when changed. Keep the durable
        # record independent of this disposable discovery cache.
        previous = cached if cached and cached.get('_scan', {}).get('parser') == PARSER_VERSION else None
        if previous and previous.get('_scan', {}).get('inode') != info.st_ino:
            previous = None
        value = transcript(path, agent, previous)
        self.db.execute('insert or replace into scans values (?,?,?)', (str(path), stamp, json.dumps(value)))
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


def transcript(path, agent, previous=None):
    record = dict(agent=agent, sid='', title='', updated=0, associations=[], prs=[], mentions=[])
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
                        record['recorded_repo'] = slug(git.get('repository_url', ''))
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
                message = payload if agent == 'codex' else row.get('message', {})
                if not isinstance(message, dict):
                    continue
                role = message.get('role') or row.get('type')
                content = message.get('content', [])
                blocks = content if isinstance(content, list) else [{'text': content}]
                text = ' '.join(str(b.get('text', '')) for b in blocks if isinstance(b, dict))
                if role == 'user' and not record['title'] and text.strip() and not text.lstrip().startswith(('<', '# AGENTS.md instructions')):
                    record['title'] = clean(text)[:200]
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


def format_row(row):
    date = datetime.datetime.fromtimestamp(row.get('updated', 0)).strftime('%Y-%m-%d')
    owners = row.get('owners', [])
    owner = ','.join(clean(r.get('host', 'local') + ':' + r.get('slot', '?')) for r in owners) or 'inactive'
    hosts = ','.join(sorted({a.get('host', 'local') for a in row['associations']})) or 'local'
    branches = ','.join(dict.fromkeys(a.get('branch', '') for a in row['associations'] if a.get('branch'))) or '?'
    prs = ' '.join(f'#{p["number"]}[{p.get("state", "?")}]' for p in row['prs'][:3])
    return clean(f'{date}  {row["title"] or "Untitled conversation"}  {row["agent"]} {row["sid"][:8]}  {hosts}  {owner}  {branches}  {prs}')


def details(row):
    fields = [f'{row["agent"]} {row["sid"]}', f'Repository: {row["repo"]}', f'Title: {row["title"]}',
              f'Current owner: {json.dumps(row.get("owners", [])) or "inactive"}']
    fields.extend(f'Workspace: {a.get("workspace")} · slot {a.get("slot", "?")} · host {a.get("host", "?")} · branch {a.get("branch") or "unknown"} · commit {a.get("commit") or "unverified"} · evidence {a.get("evidence")} · availability {a.get("available", "unknown")}' for a in row['associations'])
    fields.extend(f'Associated PR: {p["url"]} [{p.get("state", "unknown")}]' for p in row['prs'])
    fields.extend(f'Incidental PR mention: {p["url"]}' for p in row['mentions'] if p['url'] not in {r['url'] for r in row['prs']})
    fields.extend(f'Transcript: {s.get("host")}:{s.get("path")} · available {s.get("available", "unknown")}' for s in row['sources'])
    return '\n'.join(clean(field) for field in fields)


class HistoryCommand:
    def __init__(self, cfg, api, store=None):
        self.cfg, self.api = cfg, api
        self.root = api['_t_source_root']()
        self.store = store or Store()
        self.host = socket.gethostname().split('.')[0]
        self.reservation_token = uuid.uuid4().hex
        self.reservation = None

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
        identity = self.identity(alias)
        if value.get('recorded_repo') and value['recorded_repo'] != identity:
            return
        if not identity.startswith('local:'):
            value['prs'] = value.get('prs', []) + [dict(repo=n.get('repo') or identity, number=n['number'], url=f'https://github.com/{n.get("repo") or identity}/pull/{n["number"]}') for n in value.pop('pr_numbers', [])]
        associations = []
        for original in value.get('associations', []):
            association = dict(original)
            slot = association_matches(association, self.cfg, alias)
            if slot is None:
                continue
            association.update(slot=slot, host=host)
            associations.append(association)
        if not associations:
            return
        value.update(repo=self.identity(alias), associations=associations)
        if source:
            value['sources'] = [dict(host=host, path=str(source), available=Path(source).is_file(), updated=value.get('updated') or Path(source).stat().st_mtime, size=Path(source).stat().st_size)]
            value['updated'] = max(value.get('updated', 0), Path(source).stat().st_mtime)
        try:
            self.store.merge(value)
        except HistoryError:
            pass

    def discover(self, alias):
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
                        cwd = first.get('payload', {}).get('cwd', '')
                        if association_matches(dict(workspace=cwd), self.cfg, alias) is None:
                            continue
                    except (OSError, ValueError, AttributeError):
                        continue
                try:
                    value = self.store.scan(path, agent)
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
                        value = dict(agent='codex', sid=row.get('id', ''), title=clean(row.get('name') or row.get('title') or row.get('first_user_message')), updated=timestamp(row.get('updated_at')), archived=bool(row.get('archived')), associations=[dict(workspace=row.get('cwd', ''), branch=clean(row.get('git_branch')), commit=row.get('git_sha') or '', evidence='index', observed=timestamp(row.get('updated_at')))], sources=[dict(host='local', path=row.get('rollout_path') or '', available=Path(row.get('rollout_path') or '').is_file())])
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

    def refresh(self, alias):
        identity = self.identity(alias)
        for host, target in self.cfg.hosts.items():
            try:
                # Metadata only: no transcript transfer or implicit remote calls on browse.
                text = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', target,
                            'zsh -lic ' + shlex.quote('command t __history-export ' + shlex.quote(identity))], timeout=20)
                rows = json.loads(text)
                if not isinstance(rows, list):
                    raise HistoryError('invalid remote history')
                for row in rows:
                    if row.get('repo') != identity:
                        continue
                    row = dict(row)
                    validate(row)
                    row.pop('owners', None)
                    for field in ('associations', 'sources'):
                        row[field] = [{**value, 'host': host} for value in row.get(field, [])]
                    self.store.merge(row)
            except (HistoryError, ValueError, TypeError, AttributeError):
                print(f't history: {host} unavailable; retained cached history', file=sys.stderr)

    def owners(self, all_hosts=False):
        snippet = '_DEV_PS_AT=0; _DEV_PS_COMM=(); ' + ('_dev_rows_all' if all_hosts else "_dev_session_rows | awk 'NF {print \"local\\t\" $0}'")
        text = run(self.shell(snippet))
        return self.api['_parse_rows'](text, host_prefixed=True)

    def records(self, alias, slot='', owners=None):
        identity = self.identity(alias)
        rows = []
        for value in self.store.records():
            if value['repo'] != identity:
                continue
            row = dict(value)
            row.setdefault('title', '')
            row['owners'] = [owner for owner in owners or [] if owner['sid'] == row['sid'] and owner['agent'] == row['agent']]
            row['associations'] = [{**a, 'available': Path(a['workspace']).is_dir() if a.get('host') == 'local' else 'cached'} for a in row['associations']]
            row['sources'] = [{**s, 'available': Path(s['path']).is_file() if s.get('host') == 'local' else 'cached'} for s in row['sources']]
            if not slot or any(a.get('slot') == slot for a in row['associations']):
                rows.append(row)
        return rows

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
            private_write(state, dict(page=page, query=query, limit=limit))
            helper = shlex.join([sys.executable, str(Path(self.root) / 'libexec/t_history.py'), 'picker', str(snapshot), str(state)])
            header = state.with_suffix('.header')
            _, total = page_rows(rows, query, limit, page)
            header.write_text(f'Page {page} · {total} conversations · {limit} per page\n')
            rows_file = state.with_suffix('.rows')
            rows_file.write_text(picker_rows(rows, query, limit, page))
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
            expected = f'{index}\t{format_row(selected)}\t{clean(json.dumps(selected, ensure_ascii=False))}'
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
                run(['rsync', '-a', '--protect-args', '--timeout=10', '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=3', remote, str(destination)])
                if not self.verify_source(self.store.scan(destination, row['agent']), row):
                    raise HistoryError('selected transcript identity changed; no session started')
                return destination
        raise HistoryError('selected transcript is missing; retained history is still available')

    def verify_source(self, value, row):
        if value['sid'] != row['sid'] or value.get('agent') != row['agent']:
            return False
        if value.get('recorded_repo') and value['recorded_repo'] != row['repo']:
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
        directory = self.cfg.repos[alias]
        base = Path(directory).name
        root = Path(self.cfg.worktree_root) / base
        association, recorded = self.revision(row)
        historical_slot = association.get('slot', '')
        historical_path = root / historical_slot if historical_slot else None
        busy = {os.path.realpath(owner['cwd']) for owner in owners}
        latest = []
        if historical_path:
            for conversation in self.store.records():
                for a in conversation.get('associations', []):
                    if a.get('host') == 'local' and os.path.realpath(a['workspace']) == os.path.realpath(historical_path) and a.get('evidence') in ('launch', 'resume', 'close', 'handoff'):
                        latest.append((a.get('observed', 0), conversation['sid'], conversation['agent']))
        current_identity = max(latest, default=(0, '', ''))[1:]
        # Reuse only when both revision and provenance match, never by branch name alone.
        if association.get('generation') and association['generation'] == generation(historical_path) and current_identity == (row['sid'], row['agent']) and recorded == revision and historical_path and association.get('host') == 'local' and os.path.realpath(association.get('workspace', '')) == os.path.realpath(historical_path) and historical_path.is_dir() and os.path.realpath(historical_path) not in busy:
            head = run(['git', '-C', str(historical_path), 'rev-parse', 'HEAD'], optional=True).strip()
            actual = run(['git', '-C', str(historical_path), 'branch', '--show-current'], optional=True).strip()
            if head == revision and actual == association.get('branch'):
                self.reserve(historical_path)
                return str(historical_path), historical_slot, False
        # Fresh allocation skips every existing branch/worktree and every owner.
        for slot in range(1, 10001):
            path, branch = root / str(slot), f'dev/{base}-{slot}'
            if path.exists() or any(owner['slot'] == f'{alias}-{slot}' for owner in owners):
                continue
            if run(['git', '-C', directory, 'rev-parse', '--verify', 'refs/heads/' + branch], optional=True) or run(['git', '-C', directory, 'rev-parse', '--verify', 'refs/remotes/origin/' + branch], optional=True):
                continue
            root.mkdir(parents=True, exist_ok=True)
            marker = root / f'.{slot}.history-reservation'
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

    def codex_released(self, row, source):
        if row['agent'] != 'codex':
            return
        value = transcript(source, 'codex')
        helper = runpy.run_path(str(Path(self.root) / 'libexec/t_app_handoff.py'))
        helper['assert_released']('Codex', row['sid'], value['associations'][0]['workspace'], self.api['_run'],
            codex_home=str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')),
            rollout=str(source), archived=bool(row.get('archived')) and source.resolve().is_relative_to((Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'archived_sessions').resolve()),
            retry='Release this conversation in its owning app or terminal, then reopen history.')

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

    def resume(self, alias, row):
        owners = self.owners(True)
        matches = [o for o in owners if o['sid'] == row['sid'] and o['agent'] == row['agent']]
        if len(matches) > 1:
            raise HistoryError('conversation has multiple reported owners; resolve ownership before resuming')
        if matches:
            return self.attach(matches[0])
        source = self.source(row)
        association, revision = self.revision(row)
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
                    if self.identity(alias) != row['repo']:
                        raise HistoryError('repository identity changed before restore')
                    self.codex_released(row, source)
                    self.install_transcript(row, source, destination)
                    # Python holds the same POSIX cwd/SID locks as the zsh wrapper;
                    # use the claimed helper, never recursively acquire those locks.
                    snippet = 'T_HISTORY_TOKEN=' + shlex.quote(self.reservation_token) + ' _dev_resume_session_claimed ' + shlex.join([f'dev-{alias}-{slot}', destination, row['sid'], row['agent'], 'history'])
                    run(self.shell(snippet))
                self.store.merge({**row, 'associations': [dict(workspace=destination, host='local', slot=slot, commit=revision, branch=run(['git', '-C', destination, 'branch', '--show-current']).strip(), generation=generation(destination), evidence='resume', observed=time.time())]})
            finally:
                if self.reservation:
                    self.reservation.unlink(missing_ok=True)
                    self.reservation = None
        return self.attach(dict(host='local', slot=f'{alias}-{slot}', sid=row['sid'], agent=row['agent'], cwd=destination, state='active'))

    def execute(self, args):
        alias, slot = self.scope(args.target)
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
        selected, total = page_rows(rows, '', args.limit, args.page)
        if args.json:
            print(json.dumps(dict(version=VERSION, repo=self.identity(alias), slot=slot, page=args.page, limit=args.limit, total=total, conversations=selected)))
            return 0
        if not tty:
            if selected:
                cells = []
                for row in selected:
                    owners = ','.join(clean(owner.get('host', 'local') + ':' + owner.get('slot', '?'))
                                      for owner in row.get('owners', [])) or 'inactive'
                    hosts = ','.join(sorted({a.get('host', 'local') for a in row['associations']})) or 'local'
                    branches = ','.join(dict.fromkeys(a.get('branch', '') for a in row['associations']
                                                     if a.get('branch'))) or '?'
                    prs = ' '.join(f'#{pr["number"]}[{pr.get("state", "?")}]' for pr in row['prs'][:3]) or '—'
                    date = datetime.datetime.fromtimestamp(row.get('updated', 0)).strftime('%Y-%m-%d')
                    cells.append((date, row['agent'], row['sid'], row['title'] or 'Untitled conversation',
                                  hosts, owners, branches, prs))
                print(f'history   {alias}' + (f' slot {slot}' if slot else ' · all slots'))
                print('\n'.join(self.api['_table_lines'](
                    ('DATE', 'AGENT', 'SESSION', 'TITLE', 'HOST', 'OWNER', 'BRANCH', 'PRS'), cells)))
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


def picker_rows(rows, query, limit, page):
    selected, total = page_rows(rows, query, limit, page)
    indices = {json.dumps([row['repo'], row['agent'], row['sid']]): i for i, row in enumerate(rows)}
    return ''.join(f'{indices[json.dumps([row["repo"], row["agent"], row["sid"]])]}\t{format_row(row)}\t{clean(json.dumps(row, ensure_ascii=False))}\n' for row in selected)


def picker(argv):
    snapshot, state, action, value = argv
    rows = json.loads(Path(snapshot).read_text())
    settings = json.loads(Path(state).read_text())
    if action == 'details':
        print(details(rows[int(value)]))
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
    Path(state).with_suffix('.header').write_text(f'Page {settings["page"]} · {total} conversations · {settings["limit"]} per page\n')
    output = picker_rows(rows, value, settings['limit'], settings['page'])
    Path(state).with_suffix('.rows').write_text(output)
    print(output, end='')


def record_association(sid, agent, workspace, transcript_path='', event='launch'):
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
    if transcript_path and Path(transcript_path).is_file() and transcript(Path(transcript_path), agent)['sid'] != sid:
        return
    commit = run(['git', '-C', workspace, 'rev-parse', 'HEAD'], optional=True).strip()
    branch = run(['git', '-C', workspace, 'branch', '--show-current'], optional=True).strip()
    slot = Path(workspace).name if Path(workspace).name.isdigit() else ''
    store = Store()
    try:
        value = dict(repo=repo, agent=agent, sid=sid, updated=time.time(), associations=[dict(workspace=workspace, slot=slot, host='local', branch=branch, commit=commit, generation=generation(workspace), evidence=event, observed=time.time())])
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
