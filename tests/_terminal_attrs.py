"""Compare terminal restoration without Darwin's kernel-managed PENDIN bit."""
import sys


# XNU tty.c sets PENDIN on tcsetattr. Other terminal flags remain exact.
# https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/tty.c#L1313
_DARWIN_PENDIN = 0x20000000


def terminal_attrs_equal(before, after, *, platform=None):
    before, after = list(before), list(after)
    if (platform or sys.platform) == 'darwin':
        before[3] &= ~_DARWIN_PENDIN
        after[3] &= ~_DARWIN_PENDIN
    return before == after
