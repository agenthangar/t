"""The design gate cannot silently drop a terminal mode or accept skipped fixtures."""
import json
from pathlib import Path
import pytest
from scripts import design_check as check

ROOT = Path(__file__).resolve().parents[1]


def documents():
    return (json.loads((ROOT / 'ui/command-contract.json').read_text()),
            json.loads((ROOT / 'ui/layout-contract.json').read_text()))


def test_current_inventory_has_real_layout_fixtures():
    assert len(check.validate(*documents())) >= 20


@pytest.mark.parametrize('change', ['command', 'mode', 'fixture', 'nested', 'exclusion'])
def test_missing_layout_coverage_fails(change):
    inventory, contract = documents()
    if change == 'command': del inventory['commands']['session resume']['layout']
    if change == 'mode': del inventory['commands']['session resume']['modes']['repair']['layout']
    if change == 'fixture': contract['profiles']['rail']['tests'] = ['tests/test_tui.py::test_missing']
    if change == 'nested': contract['nested_screens']['future.custom'] = 'absent'
    if change == 'exclusion': inventory['commands']['session resume']['layout'] = 'external'
    with pytest.raises(ValueError): check.validate(inventory, contract)


@pytest.mark.parametrize('outcome', ['skipped', 'failed', 'missing'])
@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_required_frames_cannot_skip_or_disappear(phase, outcome):
    node = 'tests/test_tui.py::test_frame[80-24]'
    reports = {node: dict(setup='passed', call='passed', teardown='passed')}
    check.audit([node.split('[')[0]], [node], reports)
    if outcome == 'missing': del reports[node][phase]
    else: reports[node][phase] = outcome
    with pytest.raises(ValueError): check.audit([node.split('[')[0]], [node], reports)


def test_deselected_fixture_cannot_pass():
    with pytest.raises(ValueError): check.audit(['test_a', 'test_b'], ['test_a'], {})


def test_design_job_is_required_on_both_pinned_fzf_versions():
    source = (ROOT / '.github/workflows/ci.yml').read_text()
    job = source.split('  design-adherence:\n')[1].split('  shell:\n')[0]
    assert "fzf: ['0.44.1', '0.74.5']" in job
    assert 'ref: ${{ needs.scope.outputs.sha }}' in job
    assert 'python3 scripts/design_check.py run' in job
    assert 'node tests/picker_preview.cjs' in job
    script = (ROOT / 'scripts/check_design.sh').read_text()
    assert 'python3 scripts/design_check.py run' in script
    assert 'node tests/picker_preview.cjs' in script
    assert 'if: always()' in job
    assert 'design-adherence' in source.split('  ci:\n')[1]
