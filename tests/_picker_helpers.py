"""Compare picker behavior independently of native key-label spelling."""
import re


def semantic_chrome(text):
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    for native, portable in (('↩ Return', 'Enter'), ('↩', 'Enter'),
                             ('⎋ Esc', 'Esc'), ('⎋', 'Esc'),
                             ('⇥ Tab', 'Tab'), ('⇥', 'Tab')):
        text = text.replace(native, portable)
    return re.sub(r'[⌃^]([A-Z/])', r'Ctrl-\1', text)
