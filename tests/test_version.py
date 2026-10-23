"""Version discovery works through real checkout and installed release entrypoints."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_release_install import artifact, release, sandbox

ROOT = Path(__file__).resolve().parents[1]


def invoke(binary, args, home, cwd, *, shell=False):
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
               XDG_CACHE_HOME=str(home / '.cache'), XDG_DATA_HOME=str(home / '.local/share'),
               PATH=str(binary.parent) + os.pathsep + env['PATH'])
    command = [str(binary), *args]
    if shell:
        command = ['zsh', '-f', '-c', 'source "$1"; shift; t "$@"',
                   'version-test', str(ROOT / 'zsh/shim.zsh'), *args]
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize('installation', ['checkout', 'release'])
@pytest.mark.parametrize('shell', [False, True], ids=['binary', 'zsh'])
def test_version_and_help_in_installations(sandbox, tmp_path, installation, shell):
    home, base = sandbox
    if installation == 'release':
        archive, sums = artifact()
        release.install(archive, sums, base, home)
        binary = home / 'bin/t'
        expected = 't v1.2.3\n'
    else:
        checkout = tmp_path / 'checkout'
        (checkout / 'bin').mkdir(parents=True)
        binary = checkout / 'bin/t'
        shutil.copy2(ROOT / 'bin/t', binary)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()
        git('init', '-q')
        git('add', 'bin/t')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture')
        expected = f"t {git('rev-parse', '--short', 'HEAD')} (checkout)\n"
    # Invoke outside the installation, through the installed symlink for releases.
    for args in (['version'], ['--version']):
        result = invoke(binary, args, home, tmp_path, shell=shell)
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, '')
    for args in (['--help'], ['help'], ['help', 'version'], ['version', '--help']):
        result = invoke(binary, args, home, tmp_path, shell=shell)
        assert result.returncode == 0 and result.stderr == ''
        assert 'version' in result.stdout and '--version' in result.stdout
    for args in (['version', 'extra'], ['version', '--invalid']):
        result = invoke(binary, args, home, tmp_path, shell=shell)
        assert result.returncode == 2
        assert 'unrecognized arguments' in result.stderr


@pytest.mark.parametrize('argv', [['version'], ['--version'], ['help', 'version'], ['version', '--help']])
def test_version_does_not_load_config_or_check_updates(t_mod, monkeypatch, capsys, argv):
    def forbidden(*args, **kwargs):
        pytest.fail('version/help must not load configuration or check updates')
    monkeypatch.setattr(t_mod, 'Config', forbidden)
    monkeypatch.setattr(t_mod, '_t_auto_update', forbidden)
    monkeypatch.setattr(t_mod, '_t_version', lambda: 'abc1234 (checkout)')
    if argv == ['--version']:
        with pytest.raises(SystemExit) as error:
            t_mod.main(argv)
        assert error.value.code == 0
    else:
        assert t_mod.main(argv) == 0
    result = capsys.readouterr()
    assert result.err == ''
    if argv in (['version'], ['--version']):
        assert result.out == 't abc1234 (checkout)\n'
