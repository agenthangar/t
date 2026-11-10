"""Provider help and explicit desktop launch. Never starts inference or installs apps."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROVIDERS = {'lmstudio': ('LM Studio', 'https://lmstudio.ai/docs/cli', 'https://lmstudio.ai/download'),
             'ollama': ('Ollama', 'https://docs.ollama.com/cli', 'https://ollama.com/download')}


def provider_info(provider):
    if provider not in PROVIDERS:
        raise ValueError('choose lmstudio or ollama')
    name, docs, download = PROVIDERS[provider]
    return {'name': name, 'docs': docs, 'download': download}


def cli_guide(provider, endpoint=None):
    provider_info(provider)
    commands = ([('List downloaded models', 'lms ls', 'Models stored on disk.'),
                 ('See loaded models', 'lms ps', 'Models currently loaded in memory.'),
                 ('Chat in the terminal', 'lms chat', 'Start an interactive chat; select a local model.'),
                 ('Command help', 'lms --help', 'Available commands and options.')]
                if provider == 'lmstudio' else
                [('List downloaded models', 'ollama list', 'Models available to this Ollama server.'),
                 ('See loaded models', 'ollama ps', 'Loaded models and reported memory/processor placement.'),
                 ('Chat in the terminal', 'ollama run MODEL', 'Replace MODEL with a local model name; missing models may download.'),
                 ('Command help', 'ollama --help', 'Available commands and options.')])
    return [{'label': label, 'command': command, 'description': description,
             'note': ('Direct chat runs outside t’s queue. Use it when the model is free.' if i == 2 else
                      'lms may start LM Studio if it is stopped.' if provider == 'lmstudio' else
                      'Set OLLAMA_HOST to the runtime URL shown by t.'),
             'endpoint': endpoint} for i, (label, command, description) in enumerate(commands)]


def desktop_app(provider):
    info = provider_info(provider)
    candidates = []
    if sys.platform == 'darwin':
        candidates = [Path('/Applications') / (info['name'] + '.app'),
                      Path.home() / 'Applications' / (info['name'] + '.app')]
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs'
        candidates = [base / info['name'] / (info['name'] + '.exe')]
        if provider == 'ollama':
            candidates = [base / 'Ollama' / 'ollama app.exe']
    elif provider == 'lmstudio':
        binary = shutil.which('lm-studio')
        candidates = [Path(binary)] if binary else []
    path = next((p for p in candidates if p.is_absolute() and p.exists()), None)
    return dict(info, installed=path is not None, path=str(path) if path else None, platform=sys.platform)


def open_desktop(provider):
    app = desktop_app(provider)
    if not app['installed']:
        raise ValueError(app['name'] + ' desktop app was not found; see ' + app['download'])
    command = ['/usr/bin/open', '-a', app['path']] if sys.platform == 'darwin' else [app['path']]
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True) if sys.platform != 'darwin' else subprocess.run(
                             command, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Could not open ' + app['name'] + '; try opening it from your application launcher.') from exc
    return app
