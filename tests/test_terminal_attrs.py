"""Keep the picker restoration assertion strict beyond Darwin's PENDIN."""
import copy
import termios

import pytest

from _terminal_attrs import terminal_attrs_equal


def attrs():
    return [1, 2, 3, termios.ECHO | termios.ICANON | termios.ISIG, 9600, 9600, [3, 4, 5]]


def test_only_darwin_pending_input_bit_is_ignored():
    before = attrs()
    after = copy.deepcopy(before)
    after[3] |= 0x20000000
    assert terminal_attrs_equal(before, after, platform='darwin')
    assert terminal_attrs_equal(after, before, platform='darwin')
    assert not terminal_attrs_equal(before, after, platform='linux')
    assert before == attrs()  # Normalization never mutates the saved attributes.


@pytest.mark.parametrize('flag', [termios.ECHO, termios.ICANON, termios.ISIG])
@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_terminal_mode_changes_are_not_ignored(flag, platform):
    before = attrs()
    after = copy.deepcopy(before)
    after[3] ^= flag
    if platform == 'darwin':
        after[3] |= 0x20000000
    assert not terminal_attrs_equal(before, after, platform=platform)


@pytest.mark.parametrize('field', [0, 1, 2, 4, 5, 6])
@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_other_flags_speeds_and_control_characters_remain_exact(field, platform):
    before = attrs()
    after = copy.deepcopy(before)
    if field == 6:
        after[field][0] += 1
    else:
        after[field] += 1
    assert not terminal_attrs_equal(before, after, platform=platform)
