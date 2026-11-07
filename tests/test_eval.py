"""Evaluation contracts use isolated state and fake engines; no model downloads."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'CODEX_HOME'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.delenv('DOCKER_CONTEXT', raising=False)
    monkeypatch.delenv('DOCKER_CONFIG', raising=False)
    monkeypatch.delenv('DOCKER_DEFAULT_PLATFORM', raising=False)
    m = load_script(ROOT / 'libexec/t_eval.py', 'evaluation_test')
    cli = load_script(ROOT / 'libexec/t_eval_cli.py', 'evaluation_cli_test')
    monkeypatch.setattr(cli, 'core', lambda: vars(m))
    return m, cli


def test_command_contract(evaluation, t_mod, monkeypatch, capsys):
    m, cli = evaluation
    parser = t_mod.build_parser()
    monkeypatch.setattr(cli, 'choose', lambda *a, **kw: pytest.fail('read-only display opened picker'))
    for words in (['eval'], ['eval', 'config', 'list'], ['eval', 'benchmarks', 'list'], ['eval', 'runs', 'list']):
        assert cli.command(None, parser.parse_args(words), {}) == 0
    assert not m.paths()['state'].exists()
    for group in ('eval', 'eval config', 'eval benchmarks', 'eval runs'):
        assert 'fzf' in t_mod._group_help(group)
    assert t_mod._canonical_argv(['eval', 'runs', 'ls']) == ['eval', 'runs', 'list']
    assert not t_mod._command_readonly(['eval'], terminal=True)
    assert t_mod._command_readonly(['eval'], terminal=False)
    config = m.save_config(m.defaults('example'))
    for words in (['eval', 'config', 'show', 'example', '--json'],
                  ['eval', 'runs', 'start', 'example', '--dry-run', '--json']):
        capsys.readouterr()
        assert cli.command(None, parser.parse_args(words), {}) == 0
        assert isinstance(json.loads(capsys.readouterr().out), dict)
        assert t_mod._command_readonly(words)
    assert not m.paths()['state'].exists()
    launches = []
    monkeypatch.setattr(m, 'launch', lambda *a: launches.append(a))
    capsys.readouterr()
    assert cli.command(None, parser.parse_args(['eval', 'runs', 'start', 'example', '--json']), {}) == 0
    started = json.loads(capsys.readouterr().out)
    assert launches == [(started['id'],)]
    run = m.load_run(started['id'])
    assert run['manifest']['config'] == config
    for action in ('list', 'show'):
        words = ['eval', 'runs', action] + ([started['id']] if action == 'show' else []) + ['--json']
        assert cli.command(None, parser.parse_args(words), {}) == 0
    assert cli.command(None, parser.parse_args(['eval', 'runs', 'cancel', started['id']]), {}) == 0
    assert (m.run_directory(started['id']) / 'cancel.json').exists()
    journal = m.Journal(started['id']); journal.checkpoint(status='blocked')
    assert cli.command(None, parser.parse_args(['eval', 'runs', 'resume', started['id'], '--retry-interrupted']), {}) == 0
    assert launches[-1] == (started['id'], True)
    another = m.create_run(config)
    assert cli.command(None, parser.parse_args(['eval', 'runs', 'compare', started['id'], another]), {}) == 0
    for words in (['eval', 'config', 'show', 'absent'], ['eval', 'runs', 'show', '--json'],
                  ['eval', 'runs', 'start', 'example', '--config', '/missing']):
        assert cli.command(None, parser.parse_args(words), {}) == 1


def test_config_validation_and_transactions(evaluation):
    m, _ = evaluation
    bad = [[], {'unknown': 1}, {'name': '../bad'}, {'benchmark': 'fake'}, {'profile': 'fake'},
           {'frontier_model': ''}, {'local_model': '$bad'}, {'local_provider': 'cloud'},
           {'reasoning_effort': 'fake'}, {'modes': []}, {'modes': ['local-only']*2},
           {'modes': [False]}, {'context_tokens': True}, {'task_seconds': 0},
           {'repetitions': 11}, {'concurrency': 3}, {'spend_limit_usd': float('nan')},
           {'pricing': {}}, {'pricing': []}]
    for value in bad:
        with pytest.raises(ValueError): m.validate(value)
    pricing = {'model': 'gpt-6-astra', 'source': 'https://provider/pricing', 'as_of': '2026-11-06',
               'service_tier': 'default', 'kind': 'api-equivalent', 'input_per_million': 1,
               'cached_input_per_million': .1, 'output_per_million': 5}
    assert m.validate({'pricing': pricing})['pricing'] == pricing
    for changes in ({'model': 'different'}, {'kind': 'invoice'}, {'source': ''}, {'output_per_million': -1}):
        with pytest.raises(ValueError): m.validate({'pricing': {**pricing, **changes}})
    m.save_config(m.defaults('one')); m.save_config(m.defaults('two'))
    with pytest.raises(ValueError): m.save_config(m.defaults('two'), 'one')
    m.save_config(m.defaults('three'), 'one')
    assert set(m.configurations()) == {'two', 'three'}
    m.delete_config('three')
    with pytest.raises(ValueError): m.delete_config('missing')
    m.atomic(m.paths()['config'], {'version': 2})
    with pytest.raises(ValueError): m.configurations()
    m.atomic(m.paths()['config'], {'version': 1, 'configurations': {'different': m.defaults()}})
    with pytest.raises(ValueError): m.configurations()


def test_durable_trials_scores_and_recovery(evaluation):
    m, cli = evaluation
    assert m.list_runs() == []
    run_id = m.create_run({**m.defaults(), 'profile': 'quick', 'modes': ['local-only', 'frontier-only']})
    j = m.Journal(run_id)
    trial = {'id': 'attempt-1', 'task': 'a', 'mode': 'local-only', 'repetition': 0}
    j.start_trial(trial)
    j.recover()
    assert j.state['trials'][0]['status'] == 'interrupted'
    completed = {**trial, 'id': 'attempt-2', 'status': 'completed', 'resolved': True, 'workflow_valid': True,
                 'cost_usd': 0, 'usage': {'local': {'requests': 1, 'output_tokens': 80, 'request_seconds': 2}}}
    j.start_trial(completed)
    m.atomic(j.directory / 'trials/attempt-2/result.json', completed)
    j.recover()
    assert j.state['trials'][-1]['status'] == 'completed'
    j.checkpoint(status='completed', phase='finished')
    summary = m.summarize(m.load_run(run_id))
    local = summary['modes']['local-only']
    assert local['resolved'] == 1 and local['expected'] == 3 and local['provisional']
    assert local['observed_tokens_per_second']['local'] == 40
    assert summary['modes']['frontier-only']['estimated_cost_usd'] is None
    assert summary['finished'] == 1
    assert '33.3%' in '\n'.join(cli.run_lines(m.load_run(run_id), vars(m)))
    assert m.request_cancel(run_id)['status'] == 'completed'
    assert not (j.directory / 'cancel.json').exists()
    same = m.load_run(run_id)
    different = copy.deepcopy(same); different['manifest']['config']['concurrency'] = 2
    with pytest.raises(ValueError, match='concurrency'): m.compare_runs(same, different)
    different = copy.deepcopy(same); different['task_ids'] = ['other']
    with pytest.raises(ValueError, match='task_ids'): m.compare_runs(same, different)
    different = copy.deepcopy(same); different['manifest']['seed'] = 1
    with pytest.raises(ValueError, match='seed'): m.compare_runs(same, different)
    assert m.compare_runs(same, same)['matching_modes'] == ['frontier-only', 'local-only']
    assert m.list_runs()[0]['id'] == run_id
    with pytest.raises(ValueError): m.run_directory('../bad')
    with pytest.raises(ValueError): m.load_run('eval-20261106-000000000000')
    m.atomic(j.directory / 'manifest.json', {})
    with pytest.raises(ValueError): m.load_run(run_id)


def test_picker_direct_launch_and_cancel(evaluation, t_mod, monkeypatch):
    m, cli = evaluation
    m.save_config(m.defaults('pick-me'))
    selected = []
    def choose(rows, title, *args, **kw):
        selected.append(title)
        return rows[0]['key']
    monkeypatch.setattr(cli, 'choose', choose)
    monkeypatch.setattr(cli, 'tty', lambda: True)
    launches = []
    monkeypatch.setattr(m, 'launch', lambda *a: launches.append(a))
    args = t_mod.build_parser().parse_args(['eval', 'runs', 'start'])
    assert cli.command(None, args, {}) == 0
    assert selected == ['Configurations'] and len(launches) == 1
    assert cli.select_run(None, vars(m))['id'] == launches[0][0]
    with pytest.raises(ValueError): cli.select_run(launches[0][0], vars(m), exclude=(launches[0][0],))
    with pytest.raises(ValueError): cli.select_config(None, vars(m), machine=True)
    monkeypatch.setattr(cli, 'choose', lambda *a, **kw: None)
    assert cli.command(None, args, {}) == 130
    assert len(launches) == 1


def test_config_editor_persists_after_review(evaluation, monkeypatch):
    m, cli = evaluation
    monkeypatch.setattr(cli, 'tty', lambda: True)
    class Rail:
        def __init__(self): self.steps = iter(['profile', 'quick', 'concurrency', 2, 'modes', ['local-only'], 'save'])
        def raw(self): pass
        def restore(self): pass
        def cooked_input(self, *a, **kw): return 'smoke'
        def pick(self, *a, **kw): return next(self.steps)
        def page(self, *a, **kw): return 'enter'
    result = cli.edit_config(None, {'_RailUI': Rail}, vars(m))
    assert result['profile'] == 'quick' and result['concurrency'] == 2
    assert result['task_seconds'] == 900 and result['modes'] == ['local-only']
    assert m.configurations()['smoke'] == result
    assert m.paths()['config'].stat().st_mode & 0o077 == 0


def test_ownership_launch_and_worker_recovery(evaluation, monkeypatch):
    m, _ = evaluation
    run_id = m.create_run(m.defaults())
    j = m.Journal(run_id)
    assert not m.owned(j.directory)
    with m.lock(j.directory / 'owner.lock'):
        assert m.owned(j.directory)
        with pytest.raises(BlockingIOError):
            with m.lock(j.directory / 'owner.lock', blocking=False): pass
    assert not m.owned(j.directory)
    state = dict(j.state, status='running', updated_at=0)
    m.atomic(j.directory / 'state.json', state)
    assert m.load_run(run_id)['status'] == 'interrupted'
    assert m.read(j.directory / 'state.json')['status'] == 'running'  # Reads do not mutate state.
    class Process:
        pid = 123456
        def __init__(self, command, **kwargs):
            assert str(j.directory / 'source/libexec/t_eval.py') in command
            assert '--retry-interrupted' in command
            j.checkpoint(owner_pid=self.pid)
        def poll(self): return None
    monkeypatch.setattr(m.subprocess, 'Popen', Process)
    assert m.launch(run_id, True) == 123456
    def execute(journal, retry):
        journal.checkpoint(status='completed')
    monkeypatch.setattr(m, 'helper', lambda name: {'execute': execute})
    m.worker(run_id)
    assert m.load_run(run_id)['status'] == 'completed'
    assert m.load_run(run_id)['owner_pid'] is None
    def failed(*args): raise ValueError('fixture failure')
    monkeypatch.setattr(m, 'helper', lambda name: {'execute': failed})
    m.worker(run_id)
    assert m.load_run(run_id)['diagnostic'] == 'fixture failure'
    (j.directory / 'source/bin/t').write_text('altered')
    with pytest.raises(ValueError, match='source changed'): m.launch(run_id)
    m.atomic(j.directory / 'manifest.json', {'version': 9})
    with pytest.raises(ValueError, match='manifest changed'): m.load_run(run_id)


def test_shared_fzf_opaque_targets_and_errors(evaluation, monkeypatch):
    m, cli = evaluation
    rows = [{'key': 'one', 'label': 'One\x1bunsafe', 'preview': 'Preview'}, {'key': 'two', 'label': 'Two'}]
    with pytest.raises(cli.EmptySelection): cli.choose([], 'Configurations')
    with pytest.raises(cli.EmptySelection): cli.choose([], 'Configurations', multi=True)
    with pytest.raises(ValueError, match='explicit target'): cli.choose(rows, 'Configurations')
    monkeypatch.setattr(cli, 'tty', lambda: True)
    monkeypatch.setattr(cli.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='fzf'): cli.choose(rows, 'Configurations')
    monkeypatch.setattr(cli.shutil, 'which', lambda name: '/fake/fzf')
    def picker(command, **kwargs):
        assert '_t_fzf_single' in command[2] or '_t_fzf_multi' in command[2]
        assert '\x1b' not in kwargs['input']
        assert 'FZF_DEFAULT_COMMAND' not in kwargs['env']
        return SimpleNamespace(returncode=0, stdout='1\tTwo\t/path\n')
    monkeypatch.setattr(cli.subprocess, 'run', picker)
    assert cli.choose(rows, 'Configurations') == 'two'
    assert cli.choose(rows, 'Configurations', multi=True) == ['two']
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=130))
    assert cli.choose(rows, 'Configurations') is None


def test_editor_all_fields_and_discard(evaluation, monkeypatch):
    m, cli = evaluation
    monkeypatch.setattr(cli, 'tty', lambda: True)
    m.save_config(m.defaults('existing'))
    steps = ['name', 'frontier_model', '__custom', 'reasoning_effort', 'high', 'local_model',
             'repetitions', 2, 'spend_limit_usd', 'pricing', 'edit', 'save']
    inputs = ['renamed', 'custom-model', '2', 'https://provider/pricing', '1', '.1', '3']
    class Rail:
        def raw(self): pass
        def restore(self): pass
        def cooked_input(self, *a, **kw): return inputs.pop(0)
        def pick(self, *a, **kw): return steps.pop(0)
        def page(self, *a, **kw): return 'enter'
    original_helper = m.helper
    monkeypatch.setattr(m, 'helper', lambda name: {'select_model': lambda *a: {'model': 'chosen-local', 'provider': 'ollama'}} if name == 't_local_inference' else original_helper(name))
    api = {'_RailUI': Rail, '_workflow_native_catalog': lambda *a: [{'id': 'custom-model', 'label': 'Custom', 'efforts': ['low', 'high']}]}
    value = cli.edit_config(None, api, vars(m), m.defaults('existing'))
    assert value['name'] == 'renamed' and value['frontier_model'] == 'custom-model'
    assert value['reasoning_effort'] == 'high' and value['local_provider'] == 'ollama'
    assert value['pricing']['output_per_million'] == 3 and value['spend_limit_usd'] == 2
    steps[:] = ['pricing', 'clear', 'back']
    assert cli.edit_config(None, api, vars(m), value) is None
    assert m.configurations()['renamed']['pricing'] is not None
    steps[:] = ['delete', 'delete']
    cli.edit_config(None, api, vars(m), value)
    assert not m.configurations()
    monkeypatch.setattr(cli, 'tty', lambda: False)
    with pytest.raises(ValueError, match='terminal'): cli.edit_config(None, api, vars(m))
