"""Cell geometry and wire protocol for the resume table viewport."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'libexec'))
import t_picker_viewport as viewport


def test_viewport_preserves_canonical_rows_and_clamps_boundaries(tmp_path):
    rows = ['repo\t2\tsid\t0123456789abcdefghijklmnopqrstuvwxyz']
    shown, heading = viewport.project(tmp_path, rows, 'SLOT      AGENT      TASK', 12)
    assert shown[0].startswith(rows[0] + '\t')
    assert shown[0].rsplit('\t', 1)[1] == ' 0123456789>'
    assert heading == ' SLOT      >'
    (tmp_path / 'pan-offset').write_text('999')
    shown, _ = viewport.project(tmp_path, rows, 'SLOT', 12)
    assert shown[0].endswith('<qrstuvwxyz ')
    (tmp_path / 'pan-offset').write_text('-99')
    shown, _ = viewport.project(tmp_path, [], 'SLOT', 12)
    assert shown == [] and (tmp_path / 'pan-offset').read_text() == '0'


def test_slice_keeps_wide_and_combining_cells_aligned():
    assert viewport.window('a界e\u0301z', 2, 3) == ' e\u0301z'
    assert viewport.window('\x1b[31ma界z\x1b[m', 0, 2) == 'a '
    assert viewport.search(['id\tCafé hidden NEEDLE'], 'cafe hidden needle')


@pytest.mark.parametrize('protocol', ['sgr', 'x10'])
@pytest.mark.parametrize('code,expected', [(64, -1), (65, 1), (66, -1), (67, 1)])
def test_mouse_fragments_map_only_unmodified_wheel_in_table(protocol, code, expected):
    data = f'\x1b[<{code};12;15M'.encode() if protocol == 'sgr' else b'\x1b[M' + bytes([code + 32, 44, 47])
    decoder = viewport.Mouse()
    events = []
    for value in data:
        events += decoder.feed(bytes([value]), lambda x, y: (x, y) == (12, 15))
    assert events == [expected]
    assert viewport.Mouse().feed(data, lambda *_: False) == [data]


def test_mouse_preserves_query_keys_modifiers_paste_and_releases():
    decoder = viewport.Mouse()
    content = b'\x1b[<65;12;15M'
    pasted = b'\x1b[200~' + content + b'\x1b[201~'
    assert b''.join(decoder.feed(pasted, lambda *_: True)) == pasted
    modified = b'\x1b[<69;12;15M'
    assert decoder.feed(modified, lambda *_: True) == [modified]
    assert decoder.feed(b'\x1b[<65;12;15m', lambda *_: True) == []
    assert b''.join(decoder.feed(b'abc\x1b[D', lambda *_: True)) == b'abc\x1b[D'
    assert decoder.feed(b'\x1b', lambda *_: True) == []
    assert decoder.flush() == b'\x1b'


@pytest.mark.fzf_compat
@pytest.mark.parametrize('text,query,matched', [('Café', 'cafe', True), ('Straße', 'strasse', False), ('ΟΣ', 'ος', False)])
def test_search_uses_native_fzf_case_and_accent_semantics(text, query, matched):
    assert bool(viewport.search(['id\t' + text], query)) is matched


def test_search_empty_and_failure(monkeypatch):
    assert viewport.search(['id\ttext'], '') == ['id\ttext']
    class Result:
        returncode = 2
    monkeypatch.setattr(viewport.subprocess, 'run', lambda *a, **kw: Result())
    with pytest.raises(RuntimeError, match='picker search failed'):
        viewport.search([], 'x')


def test_snapshot_controls_pan_extent_and_invalid_mouse_bytes_pass_through(tmp_path):
    (tmp_path / 'all').write_text('id\t' + 'x' * 40 + '\n')
    (tmp_path / 'pan-offset').write_text('20')
    viewport.project(tmp_path, ['id\tshort'], 'TITLE', 12)
    assert (tmp_path / 'pan-offset').read_text() == '20'
    for value in (b'\x1b[<oopsM', b'\x1b[<' + b'1' * 50):
        assert b''.join(viewport.Mouse().feed(value, lambda *_: True)) == value
    assert viewport.window('', 0, 0) == ''
    assert viewport.window('abc', 9, 3) == '   '
