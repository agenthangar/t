#!/usr/bin/env python3
"""Compare saved-resume ownership scans in an isolated real tmux environment.

Usage: python3 scripts/benchmark_resume.py --baseline-ref COMMIT --samples 10
The native agents are synthetic; these measurements do not measure Codex loading.
No user sessions, configuration, transcripts, or sockets are used.
"""
import argparse
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(args, *, env, cwd, **options):
    return subprocess.run(list(map(str, args)), env=env, cwd=cwd, check=True,
                          capture_output=True, text=True, timeout=120, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', required=True)
    parser.add_argument('--baseline-source', type=Path, default=ROOT)
    parser.add_argument('--samples', type=int, default=10)
    parser.add_argument('--counts', type=int, nargs='+', default=[0, 1, 30])
    args = parser.parse_args()
    real_tmux = shutil.which('tmux')
    if not all(shutil.which(name) for name in ('tmux', 'zsh', 'cc', 'git')):
        parser.error('tmux, zsh, cc and git are required')
    if args.samples < 1 or any(n < 0 for n in args.counts):
        parser.error('positive samples and nonnegative counts required')
    with tempfile.TemporaryDirectory(prefix='t-resume-bench-') as directory:
        base = Path(directory).resolve()
        baseline = base / 'baseline'
        baseline.mkdir()
        archive = subprocess.check_output(['git', '-C', str(args.baseline_source), 'archive', args.baseline_ref])
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(baseline, filter='data')
        home, bins, repo, codex = (base / p for p in ('home', 'bin', 'repo', 'codex'))
        for folder in (home, bins, repo, codex / 'sessions', codex / 'thread-writer-locks'):
            folder.mkdir(parents=True)
        socket = '/tmp/t-resume-bench-' + uuid.uuid4().hex[:12]
        wrapper = bins / 'tmux'
        wrapper.write_text('#!/bin/sh\n[ -z "$T_BENCH_CALLS" ] || printf "tmux\\n" >> "$T_BENCH_CALLS"\nexec ' + shlex.quote(real_tmux) + ' -S ' + shlex.quote(socket) + ' -f /dev/null "$@"\n')
        wrapper.chmod(0o755)
        real_ps = shutil.which('ps')
        allowed = base / 'fixture-pids.json'
        allowed.write_text('[]')
        ps_wrapper = bins / 'ps'
        ps_wrapper.write_text('#!' + sys.executable + '\n' + '''
import json, os, subprocess, sys
if os.environ.get('T_BENCH_CALLS'):
    with open(os.environ['T_BENCH_CALLS'], 'a') as log: log.write('ps\\n')
allowed = set(json.load(open(ALLOWED)))
args = sys.argv[1:]
if '-p' in args and not set(args[args.index('-p') + 1].split(',')) <= allowed:
    sys.exit(1)
p = subprocess.run([REAL_PS, *args], capture_output=True, text=True)
if '-Axo' in args:
    print('\\n'.join(line for line in p.stdout.splitlines() if line.split() and line.split()[0] in allowed))
else:
    sys.stdout.write(p.stdout)
sys.exit(p.returncode)
'''.replace('ALLOWED', repr(str(allowed))).replace('REAL_PS', repr(real_ps)))
        ps_wrapper.chmod(0o755)
        python_wrapper = bins / 'python3'
        python_wrapper.write_text('#!/bin/sh\n[ -z "$T_BENCH_CALLS" ] || printf "python3\\n" >> "$T_BENCH_CALLS"\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        python_wrapper.chmod(0o755)
        # Environment allowlist avoids inherited Codex/runtime/config overrides.
        env = dict(PATH=str(bins) + ':' + str(Path(sys.executable).parent) + ':' + os.environ['PATH'],
                   HOME=str(home), ZDOTDIR=str(home), CODEX_HOME=str(codex),
                   XDG_CACHE_HOME=str(base / 'cache'), XDG_CONFIG_HOME=str(base / 'config'),
                   XDG_STATE_HOME=str(base / 'state'), XDG_DATA_HOME=str(base / 'data'),
                   T_LOCAL_RC=str(home / '.zshrc.local'), T_AUTO_TRUST='0', T_RECOVERY_DISABLE='1',
                   T_NO_AUTORELOAD='1', T_DISCOVERY_COLLECTOR='1', TERM='xterm-256color',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        (home / '.zshenv').write_text('skip_global_compinit=1\n')
        (home / '.zshrc.local').write_text('DEV_REPOS[repo]=' + shlex.quote(str(repo)) + '\n')
        run(['git', 'init', '-q', repo], env=env, cwd=base)
        run(['git', '-C', repo, '-c', 'user.name=Benchmark', '-c', 'user.email=benchmark@example.invalid',
             'commit', '-q', '--allow-empty', '-m', 'disposable baseline'], env=env, cwd=base)
        source = bins / 'agent.c'
        source.write_text(r'''
#include <stdio.h>
#include <unistd.h>
int main(int argc, char **argv) {
  if (argc != 4) return 1;
  printf("\033]2;%s | %s\007GPT · %s · %s\n", argv[1], argv[2], argv[3], argv[1]);
  fflush(stdout);
  for (;;) pause();
}
''')
        run(['cc', source, '-o', bins / 'codex'], env=env, cwd=base)
        db = sqlite3.connect(codex / 'state_5.sqlite')
        db.execute('create table threads (id text, rollout_path text, updated_at int, created_at int, '
                   'archived int, first_user_message text, cwd text, name text, title text, source text)')
        daemon = None
        try:
            for count in args.counts:
                subprocess.run([str(wrapper), 'kill-server'], env=env, capture_output=True)
                db.execute('delete from threads')
                expected = set()
                for n in range(count):
                    work = base / 'worktrees' / ('repo-' + str(n + 1))
                    work.mkdir(parents=True, exist_ok=True)
                    sid = str(uuid.UUID(int=n + 1))
                    expected.add(sid)
                    title = 'Benchmark ' + str(n + 1)
                    rollout = codex / 'sessions' / ('rollout-' + sid + '.jsonl')
                    rollout.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid,
                        cwd=str(work), source='cli', cli_version='0.160.0'))) + '\n' +
                        json.dumps(dict(type='response_item', payload=dict(role='user', content=[dict(text=title)]))) + '\n')
                    updated = int(time.time()) + 60
                    db.execute('insert into threads values (?,?,?,?,?,?,?,?,?,?)',
                        (sid, str(rollout), updated, updated, 0, title, str(work), title, title, 'cli'))
                    run([wrapper, 'new-session', '-d', '-x', '240', '-y', '40', '-s', 'dev-repo-' + str(n + 1), '-c', work,
                         shlex.join([str(bins / 'codex'), title, work.name, str(work)])], env=env, cwd=base)
                db.commit()
                if count:
                    # Verify synthetic output is ready, without touching native UI.
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        pane = run([wrapper, 'capture-pane', '-p', '-t', 'dev-repo-' + str(count)], env=env, cwd=base).stdout
                        if 'GPT · ' in pane:
                            break
                        time.sleep(.02)
                    else:
                        raise RuntimeError('synthetic agent did not render')
                    pids = set(run([wrapper, 'list-panes', '-a', '-F', '#{pane_pid}'], env=env, cwd=base).stdout.split())
                    processes = subprocess.check_output([real_ps, '-Axo', 'pid=,ppid=,comm='], text=True)
                    # Include children if tmux retains a shell above the native fixture.
                    changed = True
                    while changed:
                        prior = set(pids)
                        for line in processes.splitlines():
                            fields = line.split(None, 2)
                            if len(fields) == 3 and fields[1] in pids:
                                pids.add(fields[0])
                        changed = pids != prior
                    allowed.write_text(json.dumps(sorted(pids)))
                else:
                    allowed.write_text('[]')
                result = dict(sessions=count, samples=args.samples, synthetic_agents=True, phases={})
                for label, root, scanner in [('baseline', baseline, '_dev_session_rows --details'),
                                             ('ownership', ROOT, '_dev_session_rows --ownership')]:
                    link = bins / 't'
                    link.unlink(missing_ok=True)
                    link.symlink_to(root / 'bin/t')
                    if label == 'ownership':
                        # An empty cache daemon serves metadata artifacts only; no
                        # discovery request is made, so it starts no collectors.
                        daemon = subprocess.Popen([sys.executable, str(root / 'libexec/t_session_cache.py'), 'serve'],
                            env=env, cwd=base, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    code = 'source ' + shlex.quote(str(root / 't.plugin.zsh')) + '; ' + scanner
                    values = []
                    counts = []
                    for iteration in range(args.samples + 1):
                        call_log = base / 'calls.log'
                        call_log.write_text('')
                        started = time.perf_counter()
                        completed = run(['zsh', '-fc', code], env={**env, 'T_BENCH_CALLS': str(call_log)}, cwd=base)
                        elapsed = time.perf_counter() - started
                        actual = {line.split('\t')[0] for line in completed.stdout.splitlines() if line}
                        if actual != expected:
                            raise RuntimeError(f'{label}: synthetic ownership mismatch; expected {sorted(expected)}, got {sorted(actual)}')
                        if iteration == 0:
                            result['phases'][label + '_first_seconds'] = round(elapsed, 4)
                        else:
                            values.append(elapsed)
                            calls = call_log.read_text().splitlines()
                            counts.append({name: calls.count(name) for name in ('tmux', 'ps', 'python3')})
                    result['phases'][label + '_median_seconds'] = round(statistics.median(values), 4)
                    result['phases'][label + '_samples_seconds'] = [round(x, 4) for x in values]
                    result['phases'][label + '_median_probe_processes'] = {
                        name: statistics.median(sample[name] for sample in counts) for name in ('tmux', 'ps', 'python3')}
                    if daemon:
                        daemon.terminate()
                        daemon.wait(timeout=5)
                        daemon = None
                before = result['phases']['baseline_median_seconds']
                after = result['phases']['ownership_median_seconds']
                result['reduction_percent'] = round(100 * (1 - after / before), 1)
                print(json.dumps(result), flush=True)
        finally:
            db.close()
            if daemon:
                daemon.terminate()
                daemon.wait(timeout=5)
            subprocess.run([str(wrapper), 'kill-server'], env=env, capture_output=True)
            Path(socket).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
