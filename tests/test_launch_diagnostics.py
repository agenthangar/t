"""Host launch evidence remains bounded, aggregate-only and read-only."""
import os
from pathlib import Path
import resource

import pytest


def test_darwin_launch_evidence_distinguishes_limit_and_race(t_mod, monkeypatch):
    calls = []
    def run(argv, timeout):
        calls.append((argv, timeout))
        if argv[0] == 'ps':
            return f'{os.getuid()}\n{os.getuid()}\n999999\n'
        if argv[0] == 'sysctl':
            return '511'
        return ('kernel: ptmx_get_ioctl failed due to ptmx_max limit 511\n'
                'kernel: ptmx_get_ioctl failed because minor number 96 was out of range\n'
                'private unrelated detail must not appear')
    monkeypatch.setattr(t_mod.os, 'listdir', lambda path: ['ttys000', 'ttys001', 'tty', 'ttysx', 'ptmx'])
    monkeypatch.setattr(resource, 'getrlimit', lambda key: (32, resource.RLIM_INFINITY))
    facts = t_mod._doctor_launch_facts(run, 'Darwin')
    assert facts['pty_count'] == 2 and facts['pty_max'] == 511
    assert facts['user_processes'] == 2 and facts['process_limit'] == '32/unlimited'
    output = '\n'.join(t_mod._doctor_launch_lines(facts))
    assert 'limit reached 1 · allocation out of range 1' in output
    assert 'below the configured limit' in output and 'PTY exhaustion' in output
    assert 'private unrelated' not in output
    assert all(timeout <= 5 for _, timeout in calls)
    assert calls[-1][0] == ['/usr/bin/log', 'show', '--last', '15m', '--style', 'compact',
                           '--predicate', 'process == "kernel" AND eventMessage CONTAINS "ptmx_get_ioctl"']
    assert [argv[0] for argv, _ in calls] == ['ps', 'sysctl', '/usr/bin/log']


@pytest.mark.parametrize('events', [None, '', 'Filtering the log data using predicate\n'])
def test_darwin_unknown_evidence_is_not_a_healthy_diagnosis(t_mod, monkeypatch, events):
    def denied(*args):
        raise OSError('denied')
    monkeypatch.setattr(t_mod.os, 'listdir', denied)
    monkeypatch.setattr(resource, 'getrlimit', denied)
    facts = t_mod._doctor_launch_facts(lambda argv, timeout: events if argv[0] == '/usr/bin/log' else None, 'Darwin')
    output = '\n'.join(t_mod._doctor_launch_lines(facts))
    assert 'snapshot): unavailable · configured maximum: unavailable' in output
    assert 'processes unavailable · open files unavailable' in output
    assert ('evidence: unavailable' in output) == (events is None)
    assert ('no matching failures' in output) == (events is not None)
    assert 'does not rule out an earlier transient' in output
    assert '⚠' not in output


@pytest.mark.parametrize('raw,expected', [('510', 510), ('garbled', None), (None, None)])
def test_linux_pty_evidence_and_near_limit(t_mod, monkeypatch, raw, expected):
    def read(path):
        if raw is None:
            raise OSError('not mounted')
        return raw if path.name == 'nr' else '511'
    monkeypatch.setattr(Path, 'read_text', read)
    facts = t_mod._doctor_launch_facts(lambda argv, timeout: 'bad process output', 'Linux')
    assert facts.get('pty_count') == expected
    assert 'user_processes' not in facts
    output = '\n'.join(t_mod._doctor_launch_lines(facts))
    assert ('near the configured maximum' in output) == (expected == 510)
    assert 'kernel PTY' not in output


def test_other_platform_keeps_unknown_values(t_mod):
    facts = t_mod._doctor_launch_facts(lambda argv, timeout: None, 'Other')
    assert 'pty_count' not in facts and 'kernel_readable' not in facts
    assert 'configured maximum: unavailable' in '\n'.join(t_mod._doctor_launch_lines(facts))


def test_launch_findings_are_repeated_in_summary(t_mod):
    facts = {'launch_resources': {'system': 'Linux', 'pty_count': 99, 'pty_max': 100}}
    assert any('see Launch resources' in line for line in t_mod._doctor_findings(facts))
    assert not any('nothing suspicious' in line for line in t_mod._doctor_findings(facts))

