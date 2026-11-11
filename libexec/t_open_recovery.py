"""Repair a failed open, then explicitly reset only the selected session's t state."""
import os
from pathlib import Path
import re
import runpy
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HISTORY = runpy.run_path(str(ROOT / 'libexec/t_history.py'))
LIFECYCLE = HISTORY['_LIFECYCLE']
CACHE = HISTORY['_CACHE_REQUEST']
_spinner = HISTORY['_spinner']


def matching(record, workspace, sid=''):
    return bool(sid and record.get('sid') == sid) or any(
        a.get('host') == 'local' and a.get('workspace') == workspace
        for a in record.get('associations', []))


def select_context(records, workspace, sid=''):
    rows = [r for r in records if matching(r, workspace, sid)
            and (not sid or r.get('sid') == sid)]
    if not rows:
        return None
    latest = max(r.get('updated', 0) for r in rows)
    rows = [r for r in rows if r.get('updated', 0) == latest]
    if len({(r['agent'], r['sid']) for r in rows}) != 1:
        raise ValueError('saved conversations are ambiguous; select the conversation with t session resume')
    return rows[0]


def ask(api, title, note, action):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    ui_type = runpy.run_path(str(ROOT / 'libexec/t_share_ui.py'))['ShareUI']
    with ui_type(api) as ui:
        ui.intro(title, note)
        return ui.choose('Recovery', [dict(key='go', label=action),
                                      dict(key='cancel', label='Cancel')]) == 'go'


class Recovery:
    def __init__(self, api, cfg, repo, slot):
        if repo not in cfg.repos or not re.fullmatch(r'[0-9]+', slot):
            raise ValueError('force open requires one repository and numeric slot')
        self.api, self.cfg, self.repo, self.slot = api, cfg, repo, slot
        self.session = f'dev-{repo}-{slot}'
        self.shell('_dev_worktree_enabled', repo)
        self.directory = self.shell('_dev_worktree_path', repo, slot).strip()
        if not self.directory or not cfg.worktree_root:
            raise ValueError('recovery requires an isolated worktree')
        self.workspace = Path(self.directory)
        self.branch = self.shell('_dev_worktree_branch', repo, slot).strip()
        self.backup = None

    @_spinner('Recovering session')
    def shell(self, *args, optional=False, timeout=90):
        env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
        env.update(T_OPEN_RECOVERY_ACTIVE='1', T_MCP_DETACHED='1', T_NO_UPDATE_CHECK='1')
        result = subprocess.run(['zsh', '-lic', self.api['ZSH_PRELUDE'] + shlex.join(args)],
                                env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode and not optional:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or 'session recovery failed')
        return result.stdout if not result.returncode else ''

    @_spinner('Restoring session source')
    def git(self, *args, optional=False, directory=None):
        result = subprocess.run(['git', '-C', directory or self.cfg.repos[self.repo], *args],
                                capture_output=True, text=True, timeout=60)
        if result.returncode and not optional:
            raise ValueError(result.stderr.strip() or 'could not recover source')
        return result.stdout.strip() if not result.returncode else ''

    def owner(self):
        raw = self.shell('_t_open_recovery_owner', self.session, self.directory)
        if not raw:
            return None
        values = raw.rstrip('\0').split('\0')
        if len(values) != 7:
            raise ValueError('could not verify the selected owner')
        return dict(zip(('identity', 'pane', 'dead', 'pid', 'sid', 'agent', 'dead_identity'), values))

    def repair(self):
        CACHE('event', {'targets': ['local']})
        owner = self.owner()
        if owner and owner['dead'] == '1':
            if not owner['sid']:
                raise ValueError('exited pane has no saved identity')
            self.shell('_t_restart_slot', self.session, self.directory, owner['sid'], owner['agent'],
                       'restart-dead', '', owner['dead_identity'])
        else:
            self.shell('_t_dev', self.repo, self.slot)

    def source(self, record, confirm):
        if self.workspace.is_symlink():
            raise ValueError('worktree path is a symlink; nothing reset')
        if self.workspace.exists():
            top = self.git('rev-parse', '--show-toplevel', directory=self.directory)
            common = self.git('rev-parse', '--path-format=absolute', '--git-common-dir', directory=self.directory)
            expected = self.git('rev-parse', '--path-format=absolute', '--git-common-dir')
            if Path(top).resolve() != self.workspace.resolve() or Path(common).resolve() != Path(expected).resolve():
                raise ValueError('worktree does not belong to the selected repository')
            return None  # Never freshen, reset or checkout an existing worktree.
        associations = [a for a in (record or {}).get('associations', [])
                        if a.get('host') == 'local' and a.get('workspace') == self.directory and a.get('branch')]
        branch = max(associations, key=lambda a: a.get('observed', 0))['branch'] if associations else self.branch
        if not self.git('check-ref-format', '--branch', branch, optional=True):
            raise ValueError('saved branch name is invalid')
        if self.git('rev-parse', '--verify', 'refs/heads/' + branch, optional=True):
            return ('local', branch, branch)
        self.git('fetch', '--no-write-fetch-head', 'origin',
                 f'refs/heads/{branch}:refs/remotes/origin/{branch}', optional=True)
        revision = self.git('rev-parse', '--verify', f'refs/remotes/origin/{branch}^{{commit}}', optional=True)
        if revision:
            return ('branch', branch, revision)
        prs = {(p.get('repo'), p.get('number')) for p in (record or {}).get('prs', [])
               if p.get('repo') == (record or {}).get('repo') and type(p.get('number')) is int}
        if len(prs) == 1:
            _, number = next(iter(prs))
            ref = 'refs/t/open-recovery/' + uuid.uuid4().hex
            self.git('fetch', '--no-write-fetch-head', 'origin', f'refs/pull/{number}/head:{ref}', optional=True)
            revision = self.git('rev-parse', '--verify', ref + '^{commit}', optional=True)
            if revision:
                return ('branch', branch, revision)
        remote = self.git('ls-remote', '--symref', 'origin', 'HEAD')
        head = re.search(r'^ref: refs/heads/([^\s]+)\s+HEAD$', remote, re.M)
        if not head:
            raise ValueError('published default branch could not be determined')
        default = head[1]
        if not confirm('Saved source unavailable', 'Worktree and saved branch were not found.', f'Start from {default}'):
            raise InterruptedError('source recovery cancelled')
        self.git('fetch', '--no-write-fetch-head', 'origin', f'refs/heads/{default}:refs/remotes/origin/{default}')
        return ('branch', branch, self.git('rev-parse', '--verify', f'refs/remotes/origin/{default}^{{commit}}'))

    def materialize(self, source):
        if source is None:
            return
        kind, branch, revision = source
        # --force repairs a missing registration, never a second checkout of a live branch.
        listing = self.git('worktree', 'list', '--porcelain')
        for block in listing.split('\n\n'):
            lines = block.splitlines()
            if 'branch refs/heads/' + branch in lines:
                location = next((line[9:] for line in lines if line.startswith('worktree ')), '')
                if location and Path(location).exists():
                    raise ValueError('saved branch is already checked out; no worktree replaced')
        self.workspace.parent.mkdir(parents=True, exist_ok=True)
        args = ['worktree', 'add', '--force']
        if kind != 'local':
            args += ['-b', branch]
        self.git(*args, self.directory, revision)

    def reservations(self):
        candidates = [self.workspace.parent / ('.' + self.workspace.name + '.history-reservation')]
        if self.workspace.exists():
            gitdir = self.git('rev-parse', '--absolute-git-dir', directory=self.directory)
            candidates.append(Path(gitdir) / 't-app-slot')
        result = []
        for path in candidates:
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise ValueError('unsafe session reservation; nothing reset')
            info = path.stat()
            result.append(dict(path=str(path), device=info.st_dev, inode=info.st_ino, data=path.read_bytes().hex()))
        return result

    def clear_reservations(self, reservations):
        if self.reservations() != reservations:
            raise ValueError('session reservation changed; recovery stopped')
        for record in reservations:
            Path(record['path']).unlink()

    def force(self, confirmed=False):
        history = HISTORY['HistoryCommand'](self.cfg, self.api)
        try:
            history.discover(self.repo, metadata_only=True)
            owner = self.owner()
            record = select_context(history.store.records(), self.directory, (owner or {}).get('sid', ''))
            source_path = history.source(record, required=False) if record else None
            sid = record['sid'] if source_path else ''
            agent = (owner or {}).get('agent') or (record or {}).get('agent') or self.shell(
                '_dev_agent_for', self.repo, os.environ.get('T_OPEN_RECOVERY_AGENT', '')).strip()
            if agent not in ('codex', 'claude'):
                raise ValueError('unsupported recovery agent')
            note = f'Reset t’s records for {self.repo} {self.slot}, reopen its worktree, and resume context if available.'
            if owner and owner['pid']:
                note += f' Stops {agent} in {self.repo} {self.slot} on this host.'
            if not confirmed and not ask(self.api, 'Force open', note, 'Force open'):
                raise InterruptedError('force open cancelled')
            with LIFECYCLE['launch_claims'](self.directory, sid):
                if self.owner() != owner:
                    raise ValueError('selected owner changed; retry open')
                # Resolve source before resetting history needed to find it.
                source = self.source(record, lambda *a: ask(self.api, *a))
                self.backup = LIFECYCLE['state_home']() / 'open-recovery' / (uuid.uuid4().hex + '.json')
                evidence = dict(workspace=self.directory, session=self.session, owner=owner,
                                source=source, context=record, reservations=self.reservations(),
                                phase='prepared', started=time.time())
                LIFECYCLE['write_private'](self.backup, evidence)
                if any(Path(item['path']).name == 't-app-slot' for item in evidence['reservations']) and (agent != 'codex' or not sid):
                    raise ValueError('retired integration ownership is unknown; release its conversation before resetting this slot')
                if agent != 'codex':
                    # A lost t agent label must not hide a native Codex writer.
                    LIFECYCLE['native_preflight']('codex', '', self.directory)
                if owner:
                    pane = self.shell('_t_open_recovery_capture', self.session, self.directory, *owner.values())
                    evidence['pane'] = pane
                    LIFECYCLE['write_private'](self.backup, evidence)
                    self.shell('_t_open_recovery_stop', self.session, self.directory, *owner.values())
                    LIFECYCLE['wait_released'](agent, owner['sid'])
                LIFECYCLE['native_preflight'](agent, sid, self.directory, rollout=source_path)
                # Before clearing anything, verify other local/remote owners and issue reservations.
                self.shell('_t_open_recovery_preflight', self.session, self.directory, sid, self.repo, self.slot)
                self.materialize(source)
                if sid:
                    source_path = history.install_transcript(record, source_path, self.directory)
                evidence['records'] = history.store.forget_workspace(self.directory, sid, backup=self.backup,
                                                                    evidence=evidence)
                self.clear_reservations(evidence['reservations'])
                evidence['phase'] = 'reset'
                LIFECYCLE['write_private'](self.backup, evidence)
                if sid:
                    self.shell('_dev_resume_session_claimed', self.session, self.directory, sid, agent, 'history')
                else:
                    self.shell('_dev_new_session_claimed', self.session, self.directory, self.branch, '1', agent)
                evidence['phase'] = 'complete'
                LIFECYCLE['write_private'](self.backup, evidence)
                print('Resumed saved context.' if sid else 'Opened a fresh conversation; saved context was unavailable.')
                print('Recovery backup: ' + str(self.backup))
        finally:
            history.store.close()


def main(argv):
    api = runpy.run_path(str(ROOT / 'bin/t'), run_name='open_recovery')
    mode, repo, slot, yes = argv
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    if mode == 'force' and not terminal and yes != '1':
        print(f't open: --force requires confirmation; use t session open {shlex.quote(repo)} {slot} --force --yes', file=sys.stderr)
        return 2
    if mode != 'force' and not terminal:
        print(f'Retry: t session open {shlex.quote(repo)} {slot} --force (reset this session’s t records).', file=sys.stderr)
        return 1
    recovery = None
    try:
        recovery = Recovery(api, api['Config'](), repo, slot)
        if mode != 'force':
            if not ask(api, f'Could not open {repo} {slot}', 'Try repairing this session.', 'Repair'):
                return 130
            try:
                recovery.repair()
                return 0
            except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                print('Repair failed: ' + LIFECYCLE['diagnostic_error'](error, limit=240), file=sys.stderr)
        recovery.force(confirmed=mode == 'force' and yes == '1')
        return 0
    except (KeyboardInterrupt, InterruptedError):
        return 130
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print('t open: ' + LIFECYCLE['diagnostic_error'](error), file=sys.stderr)
        if recovery and recovery.backup:
            print('Work and recovery evidence retained: ' + str(recovery.backup), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
