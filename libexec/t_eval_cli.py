"""Public eval command UI; all persistent state lives in t_eval."""
import copy
import datetime
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[1]


class EmptySelection(Exception):
    """A completed empty result, distinct from cancelling a populated picker."""


def core():
    return runpy.run_path(str(ROOT / 'libexec/t_eval.py'))


def clean(value):
    return ''.join(c if ord(c) >= 32 and not 127 <= ord(c) <= 159 else ' ' for c in str(value))


def tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose(rows, title, action='select', multi=False):
    """Opaque numeric row identities; shell previews never interpolate user text."""
    if not rows:
        print('  No eligible ' + title.lower() + '.')
        raise EmptySelection()
    if not tty():
        raise ValueError('specify an explicit target outside an interactive terminal')
    if not shutil.which('fzf'):
        raise ValueError('fzf is required for selection; install fzf or specify an explicit target')
    with tempfile.TemporaryDirectory(prefix='t-eval-picker-') as directory:
        lines = []
        for index, row in enumerate(rows):
            preview = Path(directory) / (str(index) + '.txt')
            preview.write_text('\n'.join(clean(s) for s in row.get('preview', '').splitlines()) + '\n')
            lines.append('\t'.join((str(index), clean(row['label']), str(preview))))
        command = ['sh', '-c', '. "$1"; shift; ' + ('_t_fzf_multi' if multi else '_t_fzf_single') + ' "$@"',
                   't-eval-picker', str(ROOT / 'ui/fzf.sh'), action, title,
                   '--prompt=eval> ', '--delimiter=\t', '--with-nth=2', '--no-hscroll',
                   '--preview=cat -- {-1}', '--preview-window=down:40%:wrap']
        env = {k: v for k, v in os.environ.items() if k not in ('FZF_DEFAULT_OPTS', 'FZF_DEFAULT_COMMAND')}
        result = subprocess.run(command, input='\n'.join(lines), text=True, stdout=subprocess.PIPE, env=env)
        if result.returncode:
            return [] if multi else None
        selected = [rows[int(line.split('\t', 1)[0])]['key'] for line in result.stdout.splitlines()]
        return selected if multi else (selected[0] if selected else None)


def config_lines(config, m):
    p = m['plan'](config)
    return [config['name'], 'Benchmark: SWE-bench Pro V2 · ' + m['PROFILES'][config['profile']]['label'],
            'Frontier: ' + config['frontier_model'] + ' · effort ' + config['reasoning_effort'],
            'Local: ' + config['local_model'] + ' · ' + config['local_provider'],
            'Modes: ' + ', '.join(m['MODES'][s][0] for s in config['modes']),
            f"Attempts: {p['attempts']} · budget: {config['task_seconds']//60} minutes/task · concurrency {config['concurrency']}",
            f"Maximum agent time: {p['maximum_agent_seconds']/3600:.1f} hours, plus preparation/grading",
            'Estimated cost: unknown without comparable measurements',
            'Spend guardrail: ' + ('$' + str(config['spend_limit_usd']) if config['spend_limit_usd'] is not None else 'not set')]


def run_lines(run, m):
    result = m['summarize'](run)
    config = run['manifest']['config']
    total, finished = result['total'], result['finished']
    lines = [f"{run['id']} · {config['name']} · {run['status']}",
             f"Progress: {finished}/{total} ({100*finished/total:.0f}%) · {run['phase']}",
             f"Recorded task time: {sum(t.get('elapsed_seconds', 0) for t in run['trials'])/60:.1f} minutes",
             'Frontier: ' + config['frontier_model'] + ' · Local: ' + config['local_model'],
             'SWE-bench Pro V2 · ' + m['PROFILES'][config['profile']]['label'],
             '  Workflow                       Resolved    Score     Graded']
    for score in result['modes'].values():
        lines.append(f"  {score['label']:<30} {score['resolved']:>3}/{score['expected']:<4} {score['score_percent']:>6.1f}%    {score['graded']}/{score['expected']}" + (' provisional' if score['provisional'] else ''))
        speed = score['observed_tokens_per_second']
        format_speed = lambda value: f'{value:.1f}' if value is not None else 'unavailable'
        cost = score['estimated_cost_usd']
        lines.append('    Observed tok/s: frontier ' + format_speed(speed['frontier']) + ' · local ' + format_speed(speed['local']) +
                     ' · est. cloud ' + (f'${cost:.4f}' if cost is not None else 'unavailable'))
        if score['workflow_failures']:
            lines.append(f"    Workflow checks failed: {score['workflow_failures']} attempts (separate from benchmark correctness)")
    if any(t.get('cost_kind') == 'api-equivalent' for t in run['trials']):
        lines.append('Cost basis: API-equivalent estimate for subscription usage; not an invoice.')
    if result['diagnostic']:
        lines.append('Status: ' + clean(result['diagnostic']))
    return lines


def select_config(name, m, machine=False, action='run'):
    saved = m['configurations']()
    if name:
        if name not in saved:
            raise ValueError('unknown configuration: ' + name)
        return saved[name]
    if machine:
        raise ValueError('specify CONFIG_NAME with --json')
    selected = choose([{'key': n, 'label': n + ' · ' + v['local_model'] + ' · ' + v['profile'],
                        'preview': '\n'.join(config_lines(v, m))} for n, v in saved.items()], 'Configurations', action)
    return saved[selected] if selected else None


def select_run(name, m, machine=False, eligible=lambda _: True, exclude=()):
    if name:
        result = m['load_run'](name)
        if not eligible(result) or name in exclude:
            raise ValueError('run is not eligible for this action')
        return result
    if machine:
        raise ValueError('specify run ID with --json')
    available = [r for r in m['list_runs']() if eligible(r) and r['id'] not in exclude]
    chosen = choose([{'key': r['id'], 'label': r['id'] + ' · ' + r['manifest']['config']['name'] + ' · ' + r['status'],
                      'preview': '\n'.join(run_lines(r, m))} for r in available], 'Runs')
    return next((r for r in available if r['id'] == chosen), None)


def edit_config(cfg, api, m, value=None):
    if not tty():
        raise ValueError('configuration editing requires a terminal')
    original = value['name'] if value else None
    value = copy.deepcopy(value or m['defaults']())
    ui = api['_RailUI']()
    ui.raw()
    try:
        if original is None:
            value['name'] = ui.cooked_input('Configuration name: ', default='standard', prefill='standard')
            if value['name'] in m['configurations']():
                raise ValueError('configuration name already exists')
        while True:
            rows = [('name', 'Name'), ('profile', 'Benchmark profile ›'), ('frontier_model', 'Frontier model ›'),
                    ('reasoning_effort', 'Reasoning effort ›'), ('local_model', 'Local model ›'),
                    ('modes', 'Workflow modes ›'), ('task_seconds', 'Time limit ›'), ('spend_limit_usd', 'Estimated-spend limit'),
                    ('repetitions', 'Repetitions ›'), ('concurrency', 'Concurrency ›'), ('pricing', 'Pricing snapshot ›'), ('save', 'Save configuration'),
                    ('delete', 'Delete configuration'), ('back', 'Back')]
            values = {k: str(v) for k, v in value.items()}
            values.update(profile=m['PROFILES'][value['profile']]['label'],
                          reasoning_effort='Automatic · model default' if value['reasoning_effort'] == 'automatic' else value['reasoning_effort'],
                          modes=str(len(value['modes'])) + ' selected', task_seconds=str(value['task_seconds']//60) + ' minutes',
                          spend_limit_usd='Not set' if value['spend_limit_usd'] is None else '$' + str(value['spend_limit_usd']))
            selected = ui.pick('Evaluation · ' + value['name'], rows, values=values)
            if selected in (None, 'back'):
                return None
            if selected == 'save':
                m['validate'](value)
                if ui.page('Review configuration', config_lines(value, m), accept=('enter', 'q', 'esc', 'abort'), keys='enter save · q back') == 'enter':
                    return m['save_config'](value, original)
            if selected == 'delete':
                if original and ui.pick('Delete ' + original + '?', [('back', 'Keep configuration'), ('delete', 'Delete')]) == 'delete':
                    m['delete_config'](original)
                    return None
            elif selected == 'name':
                value['name'] = ui.cooked_input('Name: ', default=value['name'], prefill=value['name'])
            elif selected == 'profile':
                picked = ui.pick('SWE-bench Pro V2', [(k, v['label']) for k, v in m['PROFILES'].items()],
                                 values={k: f"{v['tasks']} tasks · {v['seconds']//60} min/task" for k, v in m['PROFILES'].items()})
                if picked:
                    value.update(profile=picked, task_seconds=m['PROFILES'][picked]['seconds'])
            elif selected in ('frontier_model', 'reasoning_effort'):
                catalog = api['_workflow_native_catalog'](cfg, 'codex')
                if selected == 'frontier_model':
                    picked = ui.pick('Frontier model', [(r['id'], r['label']) for r in catalog] + [('__custom', 'Enter exact model ID'), ('__back', 'Back')])
                    if picked == '__custom':
                        picked = ui.cooked_input('Model ID: ', default=value['frontier_model'])
                    if picked and picked != '__back':
                        value['frontier_model'] = picked
                        value['reasoning_effort'] = 'automatic'
                        value['pricing'] = None
                else:
                    model = next((r for r in catalog if r['id'] == value['frontier_model']), {})
                    picked = ui.pick('Reasoning effort', [('automatic', 'Automatic · model default')] + [(v, v) for v in model.get('efforts', [])])
                    if picked:
                        value[selected] = picked
            elif selected == 'local_model':
                local = m['helper']('t_local_inference')
                picked = local['select_model'](ui, value['local_model'], value['context_tokens'])
                if picked:
                    value['local_model'], value['local_provider'] = picked['model'], picked['provider']
            elif selected == 'modes':
                choices = list(m['MODES'])
                picked = ui.pick('Workflow modes', [(k, m['MODES'][k][0]) for k in choices], multi=True,
                                 marked=[i for i, k in enumerate(choices) if k in value['modes']],
                                 values={k: m['MODES'][k][1] for k in choices})
                if picked:
                    value['modes'] = list(picked)
            elif selected in ('concurrency', 'repetitions', 'task_seconds'):
                choices = {'concurrency': [1, 2, 4], 'repetitions': [1, 2, 3, 5], 'task_seconds': [900, 3000, 3600, 7200]}[selected]
                picked = ui.pick(dict(concurrency='Concurrent tasks · local inference remains serialized', repetitions='Repetitions', task_seconds='Task time limit')[selected],
                                 [(v, str(v//60) + ' minutes' if selected == 'task_seconds' else str(v)) for v in choices])
                if picked:
                    value[selected] = picked
            elif selected == 'spend_limit_usd':
                text = ui.cooked_input('USD limit (blank: not set): ')
                value[selected] = float(text) if text else None
            elif selected == 'pricing':
                picked = ui.pick('Pricing · USD per million tokens', [('edit', 'Enter dated provider rates'), ('clear', 'Mark pricing unknown'), ('back', 'Back')])
                if picked == 'clear':
                    value['pricing'] = None
                elif picked == 'edit':
                    rates = {'model': value['frontier_model'], 'kind': 'api-estimate', 'service_tier': 'default',
                             'as_of': datetime.date.today().isoformat(), 'source': ui.cooked_input('Provider pricing source URL: ')}
                    for key, label in [('input_per_million', 'Input'), ('cached_input_per_million', 'Cached input'), ('output_per_million', 'Output')]:
                        rates[key] = float(ui.cooked_input(label + ' USD/million: '))
                    value['pricing'] = rates
    finally:
        ui.restore()


def command(cfg, args, api):
    m = core()
    resource = getattr(args, 'eval_resource', None)
    action = getattr(args, 'eval_action', None)
    machine = getattr(args, 'json', False)
    def output(value, lines):
        width = max(24, shutil.get_terminal_size((80, 24)).columns)
        print(json.dumps(value, indent=2) if machine else '\n'.join(
            textwrap.fill(clean(line), width=width, subsequent_indent='    ') for line in lines))
    try:
        if resource is None:
            if not tty():
                print('Usage: t eval {config,benchmarks,runs} <verb>\nStart: t eval runs start [CONFIG_NAME]')
                return 0
            selected = choose([{'key': r, 'label': label} for r, label in [('config', 'Configurations'), ('start', 'Run configuration'), ('list', 'Evaluation runs')]], 'Evaluation')
            if not selected:
                return 130
            resource, action = ('config', None) if selected == 'config' else ('runs', selected)
        if resource == 'config':
            if action == 'list':
                saved = m['configurations']()
                output(saved, [line for v in saved.values() for line in config_lines(v, m) + ['']] or ['No evaluation configurations. Use t eval config.'])
                return 0
            if action == 'show':
                value = select_config(getattr(args, 'name', None), m, machine, 'show')
                if value is None:
                    return 130
                output(value, config_lines(value, m))
                return 0
            if action == 'edit':
                value = select_config(getattr(args, 'name', None), m, machine, 'edit')
                if value is None:
                    return 130
                edit_config(cfg, api, m, value)
            else:
                while True:
                    saved = m['configurations']()
                    selected = choose([{'key': '__new', 'label': '+ New configuration'}] + [{'key': n, 'label': n, 'preview': '\n'.join(config_lines(v, m))} for n, v in saved.items()], 'Configurations', 'edit')
                    if selected is None:
                        return 130
                    edit_config(cfg, api, m, saved.get(selected))
            return 0
        if resource == 'benchmarks':
            data = {'benchmark': m['BENCHMARK'], 'revision': m['BENCHMARK_REVISION'], 'profiles': m['PROFILES'],
                    'cached': (m['paths']()['cache'] / m['BENCHMARK_REVISION'] / 'v2/SHA256SUMS').is_file()}
            output(data, ['SWE-bench Pro V2 · official verifier'] + [f"  {v['label']:<22} {v['tasks']:>3} tasks · {v['seconds']//60} min/task" for v in m['PROFILES'].values()] + ['Assets: ' + ('cached' if data['cached'] else 'prepared automatically when starting')])
            return 0
        if resource != 'runs' or not action:
            print('Usage: t eval runs {start,list,show,compare,resume,cancel}')
            return 0
        if action == 'list':
            runs = m['list_runs']()
            output([m['summarize'](r) for r in runs], [line for r in runs for line in run_lines(r, m) + ['']] or ['No evaluation runs. Use t eval runs start.'])
            return 0
        if action == 'start':
            filename = getattr(args, 'config_file', None)
            if filename and getattr(args, 'name', None):
                raise ValueError('choose CONFIG_NAME or --config FILE, not both')
            value = m['validate'](m['read'](filename)) if filename else select_config(getattr(args, 'name', None), m, machine)
            if value is None:
                return 130
            for key in ('frontier_model', 'local_model', 'reasoning_effort', 'local_provider'):
                if getattr(args, key, None):
                    value[key] = getattr(args, key)
            value = m['validate'](value)
            if getattr(args, 'dry_run', False):
                output(m['plan'](value), config_lines(value, m))
                return 0
            run_id = m['create_run'](value)
            m['launch'](run_id)
            output({'id': run_id, 'status': 'started'}, ['Started ' + run_id, 'Progress: t eval runs show ' + run_id])
            return 0
        eligible = (lambda r: r['status'] not in m['TERMINAL']) if action == 'cancel' else (lambda r: r['status'] in m['TERMINAL'] and r['status'] != 'completed') if action == 'resume' else (lambda _: True)
        first = select_run(getattr(args, 'run_id', None), m, machine, eligible)
        if first is None:
            return 130
        if action == 'show':
            output(first, run_lines(first, m))
        elif action == 'compare':
            second = select_run(getattr(args, 'other_id', None), m, machine, exclude=(first['id'],))
            if second is None:
                return 130
            output(m['compare_runs'](first, second), run_lines(first, m) + [''] + run_lines(second, m))
        elif action == 'cancel':
            if tty() and choose([{'key': 'keep', 'label': 'Keep running'}, {'key': 'cancel', 'label': 'Cancel ' + first['id']}], 'Interrupt live evaluation', 'confirm') != 'cancel':
                return 130
            m['request_cancel'](first['id'])
            print('Cancellation requested; completed results and evidence are retained.')
        elif action == 'resume':
            m['launch'](first['id'], getattr(args, 'retry_interrupted', False))
            print('Resuming ' + first['id'])
        return 0
    except EmptySelection:
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('t eval: ' + str(error), file=sys.stderr)
        return 1
