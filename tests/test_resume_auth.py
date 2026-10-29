"""Authentication diagnostics use synthetic credentials, hosts and discovery."""
import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from test_history_data import history, store, app
from test_agent_seam import zsh
from test_picker_filters import filters


@pytest.mark.parametrize('error,failed', [
    ('gh: Bad credentials (HTTP 401)', True),
    ('gh: Requires authentication (HTTP 401)', True),
    ('To get started with GitHub CLI, please run: gh auth login', True),
    ('You are not logged into any GitHub hosts.', True),
    ('gh: Not Found (HTTP 404)', False),
    ('gh: Resource not accessible (HTTP 403)', False),
    ('gh: API rate limit exceeded (HTTP 403)', False),
    ('error connecting to api.github.com', False),
    ('HTTP 500', False),
])
def test_effective_auth_classification_once(app, history, monkeypatch, error, failed):
    calls = []
    def probe(argv, **kwargs):
        calls.append(argv)
        assert argv == ['gh', 'api', '--hostname', 'github.com', 'user', '--silent']
        assert kwargs['timeout'] == 3 and kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['env']['GH_PROMPT_DISABLED'] == '1'
        return SimpleNamespace(returncode=1, stderr=error, stdout='')
    monkeypatch.setattr(history.subprocess, 'run', probe)
    app.check_auth()
    assert not calls  # machine callers opt in
    app.diagnostics_enabled = True
    app.check_auth()
    app.check_auth()
    assert len(calls) == 1
    assert bool(app.diagnostics) == failed


@pytest.mark.parametrize('error', [FileNotFoundError(), subprocess.TimeoutExpired('gh', 3)])
def test_unavailable_auth_probe_does_not_warn(app, history, monkeypatch, error):
    app.diagnostics_enabled = True
    def probe(*a, **k):
        raise error
    monkeypatch.setattr(history.subprocess, 'run', probe)
    app.check_auth()
    assert not app.diagnostics


@pytest.mark.parametrize('scope', [['repo'], ['--all']])
def test_empty_and_cached_discovery_preserve_auth_diagnostics(app, history, monkeypatch, scope):
    app.diagnostics_enabled = True
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stderr='HTTP 401', stdout=''))
    monkeypatch.setattr(history, 'run', lambda *a, **k: '')
    monkeypatch.setattr(app, 'discover', lambda *a, **k: None)
    # Already verified repository identities do not hide auth failure.
    app.repository_proofs['org/repo'] = dict(id='R_saved', canonical='org/repo')
    assert list(app.resume_candidates(scope)) == []
    assert app.diagnostic_rows() == [dict(code='github_auth_failed', host='local')]


@pytest.mark.parametrize('fallback', [False, True])
def test_scoped_remote_diagnostics_and_legacy_fallback(app, history, monkeypatch, fallback):
    app.diagnostics_enabled = True
    app.cfg.hosts = {'peer': 'user@host.example', 'duplicate': 'user@host.example'}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv[-1])
        if fallback and '--diagnostics' in argv[-1]:
            raise history.HistoryError('old peer')
        return json.dumps([] if fallback else dict(version=1, conversations=[], diagnostics=[
            dict(code='github_auth_failed', host='forged', message='secret'), dict(code='unknown')]))
    monkeypatch.setattr(history, 'run', run)
    app.refresh('repo')
    app.refresh('repo')
    assert sum('--diagnostics' in command for command in calls) == 1
    assert app.diagnostic_hosts() == ([] if fallback else ['user@host.example'])
    assert 'forged' not in str(app.diagnostics) and 'secret' not in str(app.diagnostics)


def test_catalog_diagnostic_codes_only_and_sanitized_labels(app, monkeypatch):
    app.diagnostics_enabled = True
    app.cfg.hosts = {'peer': 'user@host\x1b.example'}
    payload = dict(version=1, repositories=[], conversations=[], diagnostics=[
        dict(code='github_auth_failed', host='spoofed', message='raw token')])
    app.import_catalog(payload, 'peer')
    app.import_catalog(payload, 'peer')
    assert app.diagnostic_hosts() == ['user@host .example']
    monkeypatch.setenv('_T_RESUME_DESTINATION_HOST', 'selected.example')
    app.diagnostics['local'] = 'github_auth_failed'
    assert app.diagnostic_hosts()[-1] == 'selected.example'


@pytest.mark.parametrize('width', [28, 32, 68])
def test_persistent_auth_header_survives_every_action(filters, tmp_path, width):
    row = 'repo\t4\tsaved\t/tree\tdate\tSaved task\t-\thistory\tpeer\tcodex\t-'
    for name in ['source', 'all', 'rows-0', 'rows-1']:
        (tmp_path / name).write_text(row + '\n')
    (tmp_path / 'auth-hosts').write_text('user@host.example\nsecond.example\n')
    for action in ['current', 'agent', 'host', 'repo', 'first', 'sort-repo', 'all', 'default']:
        filters.render(tmp_path, 'resume', action, width)
        text = (tmp_path / 'current-header').read_text()
        assert text.startswith('GitHub auth failed:')
        assert 'Enter' in text
        assert all(filters.display_width(line) <= width for line in text.splitlines())
        if width <= 36:
            assert len(text.splitlines()) <= 9
    (tmp_path / 'auth-hosts').write_text('\n')
    filters.render(tmp_path, 'resume', 'current', width)
    assert 'auth failed' not in (tmp_path / 'current-header').read_text()


@pytest.mark.parametrize('mode', ['empty', 'automatic', 'picker'])
def test_resume_auth_warning_precedes_empty_or_selection(zsh, tmp_path, mode):
    setup = r'''
      _t_resume_history_candidates() {
        print -r -- user@host.example > "$T_RESUME_DIAGNOSTICS_FILE"
    '''
    if mode != 'empty':
        setup += "printf '10\\tapi\\t4\\tsaved\\t/tree\\tdate\\tSaved task\\tlocal\\tcodex\\t-\\torg/api\\n';"
    setup += '}; _t_resume_history_launch() { print -u2 SELECTED; return 1; }; '
    setup += '_t_fzf_filtered() { print -u2 "PICKER $T_PICKER_AUTH_HOSTS"; return 130; }; '
    result = zsh(setup + ('_T_RESUME_REQUIRE_PICKER=1; ' if mode == 'picker' else '') +
                 '_t_resume api --days all; echo rc=$?', _tty=mode == 'picker')
    output = result.stdout + result.stderr
    assert output.count('GitHub authentication failed on user@host.example') == 1
    assert 'gh auth login --hostname github.com on user@host.example' in output
    boundary = 'Nothing to resume' if mode == 'empty' else 'PICKER user@host.example' if mode == 'picker' else 'SELECTED'
    assert output.index('GitHub authentication failed') < output.index(boundary)


def test_all_host_diagnostics_include_empty_remote_and_deduplicate(app, history, monkeypatch):
    app.diagnostics_enabled = True
    app.cfg.repos = {}
    app.cfg.hosts = {'peer': 'host.example', 'duplicate': 'host.example'}
    calls = []
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stderr='', stdout=''))
    def run(argv, **kwargs):
        calls.append(argv)
        return json.dumps(dict(version=1, repositories=[], conversations=[],
                               diagnostics=[dict(code='github_auth_failed')]))
    monkeypatch.setattr(history, 'run', run)
    assert list(app.resume_candidates(['--all'])) == []
    assert len(calls) == 1 and '__history-catalog --diagnostics' in calls[0][-1]
    assert app.diagnostic_hosts() == ['host.example']


@pytest.mark.parametrize('diagnostic', [False, True])
def test_private_export_retains_list_unless_diagnostics_requested(app, history, t_mod, monkeypatch, capsys, diagnostic):
    app.cfg.repos = {}
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {'HistoryCommand': lambda cfg, api: app})
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stderr='HTTP 401', stdout=''))
    args = ['__history-export', 'org/repo'] + (['--diagnostics'] if diagnostic else [])
    assert t_mod.main(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value == (dict(version=1, conversations=[], diagnostics=[
        dict(code='github_auth_failed', host='local')]) if diagnostic else [])


@pytest.mark.parametrize('slot', [None, '3'])
def test_private_candidate_sidecar_leaves_tsv_unchanged(app, history, t_mod, monkeypatch, capsys, tmp_path, slot):
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {
        'HistoryCommand': lambda cfg, api: app, 'HistoryError': history.HistoryError,
        'HistoryCancelled': history.HistoryCancelled})
    def candidates(aliases, **kwargs):
        assert aliases == ['repo'] and kwargs == {'slot': slot}
        return ['unchanged\tcandidate']
    monkeypatch.setattr(app, 'resume_candidates', candidates)
    app.diagnostics['local'] = 'github_auth_failed'
    monkeypatch.setattr(app, 'diagnostic_hosts', lambda: ['local.example'])
    output = tmp_path / 'diagnostics'
    args = ['__resume-candidates', '--diagnostics-file', str(output), 'repo']
    assert t_mod.main(args + (['--slot', slot] if slot else [])) == 0
    assert capsys.readouterr().out == 'unchanged\tcandidate\n'
    assert output.read_text() == 'local.example\n'
    assert app.diagnostics_enabled


def test_legacy_catalog_retains_host_only_repositories(app, history, monkeypatch):
    app.diagnostics_enabled = True
    app.cfg.repos = {}
    app.cfg.hosts = {'peer': 'host.example'}
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stderr='', stdout=''))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv[-1])
        if '--diagnostics' in argv[-1]:
            raise history.HistoryError('old peer')
        return json.dumps(dict(version=1, repositories=[dict(repo='org/remote', alias='remote', path='/remote')],
                               conversations=[]))
    monkeypatch.setattr(history, 'run', run)
    assert list(app.discover_catalog()) == ['org/remote']
    assert len(calls) == 3  # opt-in request, old catalog, repository proof
    assert '__history-export' not in ' '.join(calls)
    assert not app.diagnostics
