"""Additive zsh startup wiring, shared by install and explicit doctor repair."""
import fcntl
import os
from pathlib import Path
import shlex
import sys

BEGIN = '# >>> t shell integration >>>'
END = '# <<< t shell integration <<<'
BLOCK = '''# >>> t shell integration >>>
export PATH="$HOME/bin:$PATH"
if [[ -f "${${:-$HOME/bin/t}:A:h:h}/t.plugin.zsh" ]]; then
  source "${${:-$HOME/bin/t}:A:h:h}/t.plugin.zsh"
fi
# <<< t shell integration <<<
'''

# Exact startup forms documented before managed blocks were introduced. Do not
# append another source: local.zsh is arbitrary user code and must run once.
LEGACY = (
    'export PATH="$HOME/bin:$PATH"\nsource "${${:-$HOME/bin/t}:A:h:h}/t.plugin.zsh"',
    '''export PATH="$HOME/bin:$PATH"
if [[ -L "$HOME/bin/t" ]]; then
  _t_source="$(dirname "$(dirname "$(readlink "$HOME/bin/t")")")/t.plugin.zsh"
  [[ -f "$_t_source" ]] && source "$_t_source"
  unset _t_source
fi''',
)


def configured(text):
    return BLOCK in text or any(snippet in text for snippet in LEGACY)


def rc_path(home, env):
    return Path(env.get('ZDOTDIR') or home) / '.zshrc'


def state(home, env):
    path = rc_path(home, env)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return path, 'missing'
    except (OSError, UnicodeError):
        return path, 'unreadable'
    if configured(text):
        return path, 'configured'
    if BEGIN in text or END in text:
        return path, 'modified managed block'
    return path, 'not configured'


def repair(home, env):
    """Append once; retain all existing bytes, permissions, and symlink identity.

    Never overwrite a partial/edited managed block. Do not execute startup code
    to diagnose it: a user's rc can open menus or perform arbitrary writes.
    """
    path = rc_path(home, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A dangling rc symlink is usually a broken dotfiles installation. Do not
    # silently create its target (which may be outside the selected shell home).
    if path.is_symlink() and not path.exists():
        raise ValueError(f'{path}: dangling symlink; restore its target first')
    with path.open('a+', encoding='utf-8', newline='') as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        text = file.read()
        if configured(text):
            return path, False
        if BEGIN in text or END in text:
            raise ValueError(f'{path}: modified managed block; review it before repairing')
        file.write(('\n' if text and not text.endswith('\n') else '') + BLOCK)
    return path, True


def main():
    try:
        path, changed = repair(os.path.expanduser('~'), os.environ)
    except (OSError, ValueError, UnicodeError) as error:
        print(f't shell setup: {error}', file=sys.stderr)
        return 1
    if changed:
        print(f'Configured zsh integration in {path}')
    print("Load t in this zsh (or open a new interactive zsh):")
    print(f"  source {shlex.quote(str(path))}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
