"""Additive Codex session MCP registration using the client's own config writer."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys


_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]


def status(home):
    """Inspect client configuration without starting servers or changing files."""
    states = {'codex': 'Client not installed', 'claude': 'Not registered'}
    binary = shutil.which('codex')
    if binary:
        try:
            result = subprocess.run([binary, 'mcp', 'list', '--json'], capture_output=True,
                                    text=True, stdin=subprocess.DEVNULL, timeout=10)
            entries = json.loads(result.stdout)
            if result.returncode or not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
                raise ValueError('invalid client response')
            entry = next((e for e in entries if e.get('name') == 'sessions'), None)
            states['codex'] = ('Not registered' if entry is None else
                               'Enabled' if entry.get('enabled') is True and not entry.get('disabled_reason') else
                               'Disabled' if entry.get('enabled') is False or entry.get('disabled_reason') else
                               'Registered; enabled state unknown')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            states['codex'] = 'Status unavailable'
    try:
        data = json.loads((Path(home) / '.claude.json').read_text())
        if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
            raise ValueError('invalid client configuration')
        if 'sessions' in data.get('mcpServers', {}):
            states['claude'] = ('Disabled' if 'sessions' in data.get('disabledMcpServers', [])
                                else 'Registered (user scope)')
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        states['claude'] = 'Status unavailable'
    return states


def opt_out_path(home=None):
    return Path(os.environ.get('XDG_CONFIG_HOME') or str(Path(home or Path.home()) / '.config')) / 't' / 'mcp-disabled'


def _owned(entry, command):
    if not isinstance(entry, dict):
        return False
    transport = entry.get('transport', entry)
    return (isinstance(transport, dict) and transport.get('type', 'stdio') == 'stdio'
            and isinstance(transport.get('command'), str)
            and os.path.realpath(transport['command']) == os.path.realpath(command)
            and transport.get('args') == ['mcp']
            and not any(transport.get(key) for key in ('env', 'env_vars', 'cwd')))


@_spinner("Removing session MCP registrations")
def uninstall(command, home):
    """Remove only our exact stdio registration through the clients' own writers."""
    marker = opt_out_path(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('Automatic sessions MCP registration disabled; t mcp install restores it.\n')
    failed = False
    for client in ('codex', 'claude'):
        binary = shutil.which(client)
        if not binary:
            print(client.title() + ': client not installed; registration unchanged')
            continue
        try:
            if client == 'codex':
                result = subprocess.run([binary, 'mcp', 'list', '--json'], capture_output=True,
                                        text=True, stdin=subprocess.DEVNULL, timeout=20)
                entries = json.loads(result.stdout)
                if result.returncode or not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
                    raise ValueError('could not inspect registration')
                entry = next((e for e in entries if e.get('name') == 'sessions'), None)
            else:
                path = Path(home) / '.claude.json'
                data = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
                    raise ValueError('could not inspect registration')
                entry = data.get('mcpServers', {}).get('sessions')
            if entry is None:
                print(client.title() + ': not registered')
                continue
            if not _owned(entry, command):
                raise ValueError('custom or unrecognized sessions registration preserved; manage it in the client')
            argv = [binary, 'mcp', 'remove', 'sessions']
            if client == 'claude':
                argv += ['--scope', 'user']
            result = subprocess.run(argv, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=20)
            if result.returncode:
                raise ValueError('client could not remove registration')
            print(client.title() + ': sessions registration removed')
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(client.title() + ': ' + str(error), file=sys.stderr)
            failed = True
    print('Automatic registration disabled on this machine; t mcp install restores it.')
    return int(failed)


@_spinner("Registering Codex session tools")
def install(command):
    binary = shutil.which('codex')
    if not binary:
        return 0
    try:
        result = subprocess.run([binary, 'mcp', 'list', '--json'], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=20)
        if result.returncode:
            raise ValueError('could not inspect Codex MCP configuration')
        entries = json.loads(result.stdout)
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError('unexpected Codex MCP configuration response')
        if any(e.get('name') == 'sessions' for e in entries):
            print("Codex MCP server 'sessions' already configured; preserved")
            return 0
        result = subprocess.run([binary, 'mcp', 'add', 'sessions', '--', command, 'mcp'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ValueError('Codex MCP registration failed')
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('t: %s; rerun t mcp install after resolving Codex configuration' % error,
              file=sys.stderr)
        return 1
    print("Registered Codex MCP server 'sessions'")
    return 0


if __name__ == '__main__':
    sys.exit(0 if opt_out_path().exists() else install(os.path.abspath(sys.argv[1])))
