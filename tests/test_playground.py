"""The playground only hands a curated source snapshot to Docker."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("t_playground", ROOT / "scripts/playground.py")
playground = importlib.util.module_from_spec(spec)
spec.loader.exec_module(playground)


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts/build-release.py").write_text(
        'EXACT = {"install.sh", "bin/t"}\nPREFIXES = ("libexec/",)\n'
    )
    (repo / "bin").mkdir()
    (repo / "bin/t").write_text("tracked binary\n")
    (repo / "install.sh").write_text("tracked installer\n")
    (repo / "libexec").mkdir()
    (repo / "libexec/new.py").write_text("tracked helper\n")
    (repo / "scripts/playground").mkdir()
    (repo / "scripts/playground/Dockerfile").write_text("FROM scratch\n")
    (repo / "credentials.json").write_text("private\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "scripts/build-release.py",
                    "install.sh", "bin/t", "libexec/new.py"], check=True)
    monkeypatch.setattr(playground, "HELPERS", ("Dockerfile",))
    return repo


def test_context_uses_tracked_worktree_bytes_without_git_or_private_files(source_repo, tmp_path):
    (source_repo / "bin/t").write_text("edited binary\n")
    (source_repo / "libexec/untracked.py").write_text("untracked helper\n")
    destination = tmp_path / "context"
    playground.stage_context(source_repo, destination)
    assert (destination / "source/bin/t").read_text() == "edited binary\n"
    assert (destination / "source/libexec/new.py").read_text() == "tracked helper\n"
    assert (destination / "playground/Dockerfile").is_file()
    assert not (destination / "source/.git").exists()
    assert not (destination / "source/credentials.json").exists()
    assert not (destination / "source/libexec/untracked.py").exists()


def test_context_refuses_selected_symlink(source_repo, tmp_path):
    (source_repo / "bin/t").unlink()
    (source_repo / "bin/t").symlink_to(source_repo / "credentials.json")
    with pytest.raises(ValueError, match="regular file"):
        playground.stage_context(source_repo, tmp_path / "context")


def test_context_refuses_symlinked_parent_directory(source_repo, tmp_path):
    (source_repo / "private").mkdir()
    (source_repo / "private/new.py").write_text("private local data\n")
    shutil.rmtree(source_repo / "libexec")
    (source_repo / "libexec").symlink_to(source_repo / "private", target_is_directory=True)
    with pytest.raises(ValueError, match="regular file"):
        playground.stage_context(source_repo, tmp_path / "context")


def test_context_requires_complete_tracked_runtime(source_repo, tmp_path):
    subprocess.run(["git", "-C", str(source_repo), "rm", "-q", "--cached", "bin/t"], check=True)
    with pytest.raises(ValueError, match="missing tracked runtime files"):
        playground.stage_context(source_repo, tmp_path / "context")


@pytest.mark.parametrize("mode", ("smoke", "build"))
def test_launcher_passes_only_context_and_image_to_docker(monkeypatch, mode):
    calls = []
    monkeypatch.setattr(playground.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(playground, "stage_context", lambda repo, dest: None)
    monkeypatch.setattr(playground.subprocess, "run", lambda command, **kwargs:
                        calls.append(command) or subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(sys, "argv", ["playground.py", mode])
    assert playground.main() == 0
    assert calls[0] == ["docker", "info"]
    assert calls[1][:4] == ["docker", "build", "--tag", "t-playground:local"]
    context = Path(calls[1][-1])
    assert context.is_absolute() and context != ROOT
    assert calls[1][-2] == str(context / "playground/Dockerfile")
    if mode == "smoke":
        assert calls[2] == ["docker", "run", "--rm", "--init", "--hostname",
                            "t-playground", "t-playground:local", "smoke"]
    else:
        assert len(calls) == 2
    assert all("-v" not in command and "--mount" not in command
               and "-e" not in command and "--env" not in command for command in calls)


def test_launcher_propagates_docker_run_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(playground.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(playground, "stage_context", lambda repo, dest: None)

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 42 if command[:2] == ["docker", "run"] else 0)

    monkeypatch.setattr(playground.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["playground.py", "smoke"])
    assert playground.main() == 42
    assert [call[:2] for call in calls] == [["docker", "info"], ["docker", "build"],
                                           ["docker", "run"]]


def test_launcher_rejects_image_option_before_docker(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(playground.subprocess, "run", lambda *args, **kwargs: called.append(args))
    monkeypatch.setattr(sys, "argv", ["playground.py", "smoke", "--image=-v"])
    with pytest.raises(SystemExit) as error:
        playground.main()
    assert error.value.code == 2
    assert "image must be a Docker image tag" in capsys.readouterr().err
    assert not called
