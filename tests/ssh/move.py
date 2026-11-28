"""Run a public session move between two disposable OpenSSH servers."""
from contextlib import ExitStack
from pathlib import Path
import shlex
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
SID = '11111111-1111-4111-8111-111111111111'
WORKSPACE = '/worktrees/repo/repo-1'


def docker(*args, data=None, timeout=90):
    result = subprocess.run(['docker', *args], input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace') + result.stdout.decode(errors='replace'))
    return result.stdout.decode().strip()


def main():
    tag = 't-session-ssh-e2e:local'
    docker('build', '-t', tag, str(ROOT / 'tests/ssh'), timeout=600)
    network = 't-ssh-' + uuid.uuid4().hex[:12]
    with ExitStack() as cleanup, tempfile.TemporaryDirectory() as scratch:
        docker('network', 'create', '--internal', network)
        cleanup.callback(docker, 'network', 'rm', network)
        peers = {}
        archive = Path(scratch) / 'source.tar'
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT)
        # Include new CLI helpers and the test fixture when run before a commit.
        paths += subprocess.check_output(['git', 'ls-files', '-o', '--exclude-standard', '-z', '--',
                                          'libexec/*.py', 'tests/ssh'], cwd=ROOT)
        with archive.open('wb') as stream:
            subprocess.run(['tar', '-cf', '-', '--null', '-T', '-'], cwd=ROOT,
                           input=paths, stdout=stream, check=True)
        for peer in ('first', 'second'):
            name = network + '-' + peer
            docker('run', '-d', '--name', name, '--network', network, '--network-alias', peer, tag)
            cleanup.callback(docker, 'rm', '-f', name)
            peers[peer] = name
            docker('cp', str(archive), name + ':/tmp/source.tar')
            docker('exec', name, 'bash', '-ec', '''
                mkdir -p /opt/t /root/.ssh /root/.config/t /root/.codex/thread-writer-locks /root/.cache/claude-sessions
                chmod 700 /root/.ssh
                tar xf /tmp/source.tar -C /opt/t
                cd /opt/t
                echo v0.6.1 > .t-release-version
                T_NO_TRUST=1 T_NO_MCP=1 T_NO_PERMISSIONS=1 bash install.sh
                cc tests/ssh/session_agent.c -o /usr/local/bin/codex
                ssh-keygen -q -t ed25519 -N '' -f /root/.ssh/id_ed25519
                git config --global user.name Test
                git config --global user.email test@example.invalid
            ''')
        public_keys = [docker('exec', name, 'cat', '/root/.ssh/id_ed25519.pub') for name in peers.values()]
        known_hosts = [peer + ' ' + docker('exec', name, 'cat', '/etc/ssh/ssh_host_ed25519_key.pub')
                       for peer, name in peers.items()]
        for peer, name in peers.items():
            def write(path, content):
                docker('exec', '-i', name, 'sh', '-c', 'cat > "$1"', 'sh', path,
                       data=(content + '\n').encode())
            write('/root/.ssh/known_hosts', '\n'.join(known_hosts))
            write('/root/.ssh/authorized_keys', '\n'.join(public_keys))
            other = 'second' if peer == 'first' else 'first'
            write('/root/.config/t/local.zsh', 'DEV_REPOS[repo]=/repo\nDEV_WORKTREE_ROOT=/worktrees\n'
                  'DEV_AGENT_DEFAULT=codex\nREMOTE_HOSTS[' + other + ']=' + other)
            write('/root/.zshenv', 'export PATH=/root/.local/bin:$PATH CODEX_HOME=/root/.codex '
                  'XDG_CACHE_HOME=/root/.cache T_NO_UPDATE_CHECK=1 T_RECOVERY_DISABLE=1 '
                  'T_TEST_AGENT_LOG=/root/agent.log T_TEST_AGENT_SECONDS=240 T_TEST_FRESH_SID=' + SID)
            docker('exec', name, 'chmod', '600', '/root/.ssh/authorized_keys')
        def shell(host, command):
            return docker('exec', peers[host], 'zsh', '-lic', 'set -e; ' + command, timeout=180)
        shell('first', 'mkdir -p /srv/git && git init -q --bare -b main /srv/git/repo.git && '
              'git clone -q first:/srv/git/repo.git /repo && cd /repo && '
              'echo initial > README && git add README && git commit -qm initial && git push -qu origin HEAD:main')
        shell('second', 'git clone -q first:/srv/git/repo.git /repo')
        command = 'git -C /repo worktree add -qb dev/repo-1 ' + WORKSPACE + ' origin/main'
        shell('first', command)
        shell('first', 'cd ' + WORKSPACE + ' && echo "carry these edits" > unfinished.txt && '
              "printf '\\000\\377uncommitted\\n' > sample.bin && "
              'tmux new-session -d -s dev-repo-1 -c "$PWD" /usr/local/bin/codex && '
              'tmux setenv -t dev-repo-1 DEV_AGENT codex && '
              'tmux setenv -t dev-repo-1 CLAUDE_RESUME_ID ' + SID)
        shell('first', "python3 /opt/t/tests/ssh/seed_thread.py " + shlex.join([WORKSPACE, SID]))
        shell('first', f'_dev_history_record {SID} codex {WORKSPACE} resume')
        assert SID == shell('first', '_dev_session_sid dev-repo-1')
        remote_list = shell('second', 't session list repo --source first -w')
        assert all(value in remote_list for value in ('repo-1', 'SSH test', 'test-model', '2026-11-07')), remote_list
        shell('first', 't session move repo 1 --source local --host second --detach')
        assert shell('first', 'tmux list-sessions -F "#{session_name}" 2>/dev/null || true').splitlines().count('dev-repo-1') == 0
        sessions = shell('second', 'tmux list-sessions -F "#{session_name}|#{session_path}"').splitlines()
        assert len(sessions) == 1, sessions
        label, cwd = sessions[0].split('|', 1)
        assert shell('second', '_dev_session_sid ' + shlex.quote(label)) == SID
        assert shell('second', 'cat ' + shlex.quote(cwd + '/unfinished.txt')) == 'carry these edits'
        assert shell('second', 'od -An -tx1 -v ' + shlex.quote(cwd + '/sample.bin')).split() == [
            f'{byte:02x}' for byte in b'\x00\xffuncommitted\n']
        assert len(shell('second', 'cat /root/agent.log').splitlines()) == 1
        print('Session moved over SSH. The destination owns the original conversation and its uncommitted file.')


if __name__ == '__main__':
    main()
