"""Reviewed local model installation and Codex MCP registration for t config."""

import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = 't-local-worker'
_spinner = runpy.run_path(str(ROOT / 'libexec/t_progress.py'))['spinner']


def worker():
    return runpy.run_path(str(ROOT / 'libexec/t_local_worker.py'))


def status():
    w = worker()
    config = w['settings']()
    return dict(config, codex=w['executable']('codex'), ollama=w['executable']('ollama'),
                models_directory=str(Path.home() / '.ollama/models'),
                settings_path=str(w['config_path']()),
                note='Dependencies present does not prove inference readiness; setup verifies model metadata.')


def installation_plan(api, model):
    w = worker()
    w['model_name'](model)
    steps = []
    if not w['executable']('codex'):
        label, command = api['_install_cmd']('codex', sys.platform, bool(shutil.which('brew')))
        steps.append((label, command))
    if not w['executable']('ollama'):
        if sys.platform == 'darwin' and shutil.which('brew'):
            command = ['brew', 'install', 'ollama']
        elif sys.platform.startswith('linux') or sys.platform == 'darwin':
            command = ['sh', '-c', 'curl -fsSL https://ollama.com/install.sh | sh']
        else:
            raise ValueError('local worker setup supports macOS and Linux')
        steps.append((shlex.join(command), command))
    return steps


@_spinner("Checking Codex MCP registration")
def registration(binary):
    result = subprocess.run([binary, 'mcp', 'list', '--json'], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'could not inspect Codex MCP configuration')
    entries = json.loads(result.stdout)
    if not isinstance(entries, list):
        raise ValueError('unexpected Codex MCP list response')
    return next((entry for entry in entries if entry.get('name') == SERVER), None)


def check_registration(binary, command):
    entry = registration(binary)
    if entry:
        transport = entry.get('transport', {})
        if transport.get('command') != command or transport.get('args') != ['mcp', '--local-worker']:
            raise ValueError('Codex already has a different t-local-worker server; preserve or rename it first')
    return entry


@_spinner('Checking local Codex compatibility')
def check_codex(binary):
    result = subprocess.run([binary, 'exec', '--help'], capture_output=True, text=True, timeout=20)
    if result.returncode or '--ignore-user-config' not in result.stdout:
        raise ValueError('update Codex CLI: local workers require exec --ignore-user-config')


def register(binary, command):
    # MCP's default environment allowlist omits XDG paths. Preserve this user's
    # explicitly configured t state without passing credentials or session IDs.
    env_args = [part for key in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME') if os.environ.get(key)
                for part in ('--env', key + '=' + os.environ[key])]
    subprocess.run([binary, 'mcp', 'add', SERVER, *env_args, '--', command, 'mcp', '--local-worker'], check=True)


def setup(api, model, steps):
    w = worker()
    with w['reservation']():
        for label, command in steps:
            print('Installing: ' + label, flush=True)
            subprocess.run(command, check=True)
        binary = w['executable']('codex')
        if not binary or not w['executable']('ollama'):
            raise ValueError('installation did not expose Codex and Ollama; fix PATH and rerun t config local')
        check_codex(binary)
        command = str(ROOT / 'bin/t')
        check_registration(binary, command)
        print('Starting private local Ollama runtime…', flush=True)
        with w['ollama_server']() as (base, env):
            print('Downloading local model: ' + model, flush=True)
            subprocess.run([w['executable']('ollama'), 'pull', model], env=env, check=True)
            w['check_model'](base, model)
        # Codex writes its own config; never parse/rewrite unrelated auth or MCP entries.
        register(binary, command)
        check_registration(binary, command)
    runpy.run_path(str(ROOT / 'libexec/t_local_inference.py'))['ollama_action'](model, 'verify')
    w['save_settings']({'enabled': True, 'model': model, 'provider': 'ollama'})
    print('Local worker enabled. Refresh MCP tools or reopen Codex to load start_task, task_status and cancel_task.')
    print('Ollama starts only during local work. Cloud inference is disabled; findings return to the lead.')


def setup_mlx(model):
    local = runpy.run_path(str(ROOT / 'libexec/t_local_inference.py'))
    w = worker()
    binary = w['executable']('codex')
    if not binary:
        raise ValueError('Install Codex with t agent install first')
    local['lms']()
    check_codex(binary)
    command = str(ROOT / 'bin/t')
    check_registration(binary, command)
    with w['reservation']():
        local['install'](model, download_only=True)
    local['install'](model)
    register(binary, command)
    check_registration(binary, command)
    w['save_settings']({'enabled': True, 'model': model, 'provider': 'lmstudio'})
    print('MLX local worker enabled. Refresh MCP tools or reopen Codex; select routing in t workflow.')


def disable():
    w = worker()
    config = w['settings']()
    # Keep registration and model files, so existing connections fail closed and setup is reversible.
    w['save_settings'](dict(config, enabled=False))
    print('Local worker disabled. Downloaded models and MCP registration were kept.')


def run(args, api):
    dialog = None
    try:
        if getattr(args, 'show', False):
            for key, value in status().items():
                print(f'{key}: {value}')
            return 0
        model = getattr(args, 'local_model', None)
        disabling = getattr(args, 'disable_local', False)
        yes = getattr(args, 'yes', False)
        if model and disabling:
            raise ValueError('--model and --disable cannot be combined')
        if yes and not (model or disabling):
            raise ValueError('--yes requires --model NAME or --disable')
        if not yes:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError('use a terminal or t config local --model NAME --yes; --show is read-only')
            onboarding = runpy.run_path(str(ROOT / 'libexec/t_onboarding.py'))
            dialog = onboarding['Dialog'](api)
            if not model and not disabling:
                current = status()
                provider = current.get('provider', 'ollama')
                details = [('Delegation', 'Enabled' if current['enabled'] else 'Disabled'),
                           ('Provider', 'LM Studio' if provider == 'lmstudio' else 'Ollama'),
                           ('Model', current['model'])]
                choices = [('setup', 'Install / configure local worker'),
                           ('disable', 'Disable local delegation'), ('back', 'Back')]
                if getattr(dialog, 'ui', None):
                    choice = dialog.ui.pick('Local worker', choices, context=details)
                else:
                    dialog.notice('\n'.join(f'{label}: {value}' for label, value in details))
                    choice = dialog.choose('Local worker', choices)
                if choice in (None, 'back'):
                    return 130
                disabling = choice == 'disable'
                if not disabling:
                    if getattr(dialog, 'ui', None):
                        selected = runpy.run_path(str(ROOT / 'libexec/t_local_inference.py'))['select_model'](
                            dialog.ui, current['model'], provider=provider)
                        if not selected:
                            return 130
                        model = selected['model']
                    else:
                        model = runpy.run_path(str(ROOT / 'libexec/t_local_models.py'))['choose'](dialog)
                    if model == 'custom':
                        with dialog.suspended():
                            model = input('Ollama model name: ').strip()
        if disabling:
            lines, steps = ['Disable new local tasks; keep downloaded models and MCP registration.'], []
        else:
            model = worker()['model_name'](model)
            is_mlx = any(row['model'] == model for row in runpy.run_path(str(ROOT / 'libexec/t_local_inference.py'))['artifacts']())
            steps = [] if is_mlx else installation_plan(api, model)
            lines = [label for label, _ in steps] + [
                f'Download {model} into {Path.home() / ".ollama/models"}. Large downloads may take time.',
                'Test local inference through the shared queue; keep idle models available for reuse.',
                f'Register Codex MCP server {SERVER} → {ROOT / "bin/t"} mcp --local-worker.',
                'Enable bounded local delegation. Worker uses separate snapshots; lead reviews results.',
                'No cloud sign-in is needed for the worker. Existing Codex model defaults stay unchanged.']
            if is_mlx:
                lines = [f'Install and verify MLX artifact {model} in ~/.lmstudio/models.',
                         'Requires LM Studio or llmster with the MLX runtime installed.',
                         f'Register {SERVER} and enable bounded local workers. Select routing in t workflow.']
        if dialog and not dialog.review(lines):
            return 130
        if dialog:
            dialog.close()
            dialog = None
        if disabling:
            disable()
        elif is_mlx:
            setup_mlx(model)
        else:
            setup(api, model, steps)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f't config local: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        # The shared dialog uses its own Aborted class loaded through runpy.
        if type(exc).__name__ == 'Aborted':
            return 130
        raise
    finally:
        if dialog:
            dialog.close()
