"""Additive Codex session MCP registration using the client's own config writer."""
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys


_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]


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
        print('t: %s; rerun t mcp --install after resolving Codex configuration' % error,
              file=sys.stderr)
        return 1
    print("Registered Codex MCP server 'sessions'")
    return 0


if __name__ == '__main__':
    sys.exit(install(os.path.abspath(sys.argv[1])))
