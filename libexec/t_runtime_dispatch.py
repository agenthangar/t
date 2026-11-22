"""Stable per-user runtime front door; never select a development source by cwd."""
import os
from pathlib import Path
import runpy
import sys

PROTOCOL = 1
COMPONENTS = {'libexec/t_terminal_close.py', 'libexec/t_recovery.py'}


def main(args):
    if len(args) < 2 or args[1] not in COMPONENTS:
        return 2
    source = Path(args[0])
    try:
        if (not source.is_absolute() or (source / '.t-install-version').read_text().strip() != '1'
                or source.stat().st_uid != os.getuid()):
            raise ValueError('unverified runtime source')
        helper = source / args[1]
        compile(helper.read_bytes(), str(helper), 'exec')
        os.environ['T_HOME'] = str(source)
        # Only the explicitly bound source is used. In particular, no t_dev
        # selector may redirect a hook because an agent has changed directory.
        sys.argv = [str(helper), *args[2:]]
        runpy.run_path(str(helper), run_name='__main__')
        return 0
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        print('t runtime: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
