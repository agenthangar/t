"""Shared local configuration selection; no shell execution or configuration writes."""
import os
import shlex


def local_rc(*, config=None, fallback=None, explicit=True):
    home = os.path.expanduser('~')
    root = os.environ.get('XDG_CONFIG_HOME', os.path.join(home, '.config'))
    default = os.path.join(root, 't', 'local.zsh')
    legacy = os.path.join(home, '.zshrc.local')
    if explicit and os.environ.get('T_LOCAL_RC'):
        return os.path.expanduser(os.environ['T_LOCAL_RC'])
    try:
        with open(config or os.path.join(root, 't', 'config.sh'), encoding='utf-8') as stream:
            for line in stream:
                if line.startswith('T_LOCAL_RC='):
                    value = line.partition('=')[2].strip()
                    try:
                        parts = shlex.split(value)
                        value = parts[0] if parts else ''
                    except ValueError:
                        pass
                    if value:
                        return os.path.expanduser(value)
    except OSError:
        pass
    return fallback or (default if os.path.exists(default) or not os.path.exists(legacy) else legacy)
