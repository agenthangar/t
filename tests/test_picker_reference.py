"""The repository HTML reference uses native geometry, public data and synthetic state."""
import re

import pytest

from scripts import preview_picker as preview

PLAIN = re.compile(r'\x1b\[[0-9;]+m')


@pytest.mark.parametrize('width,height', [(112, 36), (32, 36), (32, 12), (112, 12)])
@pytest.mark.parametrize('expanded', [False, True])
def test_resume_reference_native_section_geometry(monkeypatch, width, height, expanded):
    monkeypatch.setenv('T_PICKER_HEADER_HEIGHT', str(height))
    monkeypatch.setenv('T_PICKER_RESUME_LAST', '1')
    columns, _ = preview.resume_fixture()
    text = preview.resume.header('resume', 0, {}, 1, 'STATUS  ' + columns, width,
                                 sort='closed', status=['Last refresh 09:20'], show_legend=True,
                                 filters_expanded=expanded, hidden_info=True)
    rows = text.splitlines()
    assert rows[-1].startswith('STATUS')
    assert all(preview.theme.display_width(line) <= width for line in rows)
    assert 'Edit filters' in text if not expanded else 'Hide filters' in text
    if height > 14:
        groups = text.strip().split('\n\n')
        assert len(groups) == 6
        for first, group in zip(('Last refresh', 'Active filter', 'Active sort', 'LEGEND', '', 'STATUS'), groups, strict=True):
            assert group.startswith(first)
        assert groups[1].count('\n') >= (2 if expanded else 1)
        assert ('pause' in groups[1]) == expanded
        assert all(not group.startswith('\n') for group in groups)
    else:
        assert '' not in rows and 'LEGEND' not in text


def test_picker_reference_uses_native_rows_and_marker_vocab(monkeypatch):
    monkeypatch.setenv('TERM', 'xterm-256color')
    monkeypatch.setenv('T_PICKER_COLOR', '1')
    monkeypatch.setenv('T_PICKER_RESUME_LAST', '1')
    monkeypatch.delenv('NO_COLOR', raising=False)
    controls = preview.resume_controls()
    resume = preview.resume
    assert controls['filter']['0|0|||']['0'][-1][1] == resume.resume_controls(
        False, {}, 160, 'closed', True, True, False, True, False, platform='darwin')['filter']
    assert controls['order']['closed'][0] == 'web-3'
    assert resume.resume_mark('↶') in controls['rows']['web-3']['status']
    assert '↶' in PLAIN.sub('', controls['rows']['web-3']['status'])
    assert '●' in PLAIN.sub('', controls['rows']['api-9']['status'])
    models = preview.model_controls(preview.sample_catalog())
    assert models['gapPriority'] == ['refresh-gap', 'filter-gap', 'sort-gap', 'results-gap', 'legend-gap']
    assert models['memoryStates']['false'] == {'icon': '✕', 'label': 'Blocked', 'color': 'red'}
    assert 'Sort paused' in PLAIN.sub('', models['sort']['paused']['0'][0][1][0])
    assert 'Snapshot' not in repr(models['sort']['paused'])
    assert set(models['modelStates']) == {'available', 'downloaded', 'verified', 'ready'}
