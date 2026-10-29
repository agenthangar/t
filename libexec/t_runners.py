"""Private, receipt-backed execution of one worktree snapshot on remote targets.

Status and log access use local receipts only. Remote resources are addressed by
an immutable broker repository ID and a random job ID, never a source branch.
"""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
import uuid


PROTOCOL = 1
WORKFLOW = ".github/workflows/t-runner.yml"
SWEEPER = ".github/workflows/t-runner-sweep.yml"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "error"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
HOST = re.compile(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
RESULT_LIMITS = {"result.json": 8 * 1024 * 1024, "output.log": 8 * 1024 * 1024,
                 "artifacts.tar": 64 * 1024 * 1024 + 10000 * 16384 + 10240}
_LOCK = threading.RLock()


class RunnerError(ValueError):
    pass


def state_path(state_dir=None):
    return Path(state_dir) if state_dir else Path(os.environ.get(
        "XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "t/runners"


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def _run(argv, timeout=60, input=None, output_path=None):
    try:
        if output_path is None:
            result = subprocess.run(argv, input=input, text=True, capture_output=True,
                                    timeout=timeout, check=False)
        else:
            with Path(output_path).open("wb") as output:
                result = subprocess.run(argv, input=input, stdout=output, stderr=subprocess.PIPE,
                                        timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"runner transport failed: {exc}") from exc
    if result.returncode:
        error = result.stderr or result.stdout or "runner transport failed"
        raise RunnerError((error.decode(errors="replace") if isinstance(error, bytes) else error).strip())
    return result.stdout or ""


def _api(endpoint, method="GET", data=None):
    args = ["gh", "api", endpoint, "--method", method]
    if data is not None:
        args += ["--input", "-"]
    output = _run(args, input=json.dumps(data) if data is not None else None)
    return json.loads(output) if output.strip() else {}


def _templates():
    root = Path(__file__).resolve().parents[1] / "share/runners"
    return {WORKFLOW: (root / "broker.yml").read_text(),
            SWEEPER: (root / "sweeper.yml").read_text(),
            ".t-runner-protocol.json": json.dumps({"protocol": PROTOCOL,
                "helper_sha256": hashlib.sha256(Path(__file__).with_name("t_runner_job.py").read_bytes()).hexdigest()}) + "\n"}


def _private(repository, expected_id=None):
    if not isinstance(repository, str) or not REPO.fullmatch(repository):
        raise RunnerError("broker repository must be OWNER/REPO")
    remote = _api(f"repos/{repository}")
    if remote.get("private") is not True or remote.get("visibility", "private") != "private":
        raise RunnerError("runner broker must be a private GitHub repository")
    if expected_id is not None and remote.get("id") != expected_id:
        raise RunnerError("runner broker identity changed; run setup again")
    if not isinstance(remote.get("id"), int):
        raise RunnerError("GitHub did not return an immutable repository ID")
    return remote


def setup_github(repository, create=False, state_dir=None):
    """Provision an explicitly named private broker and bind its immutable ID."""
    if not isinstance(repository, str) or not REPO.fullmatch(repository):
        raise RunnerError("broker repository must be OWNER/REPO")
    if create:
        _run(["gh", "repo", "create", repository, "--private", "--disable-issues", "--disable-wiki"])
    remote = _private(repository)
    templates = _templates()
    current_files = {}
    # Finish the read-only preflight before installing any managed files.
    for path, content in templates.items():
        try:
            current = _api(f"repos/{repository}/contents/{path}")
        except RunnerError as exc:
            if "404" not in str(exc):
                raise
            current = None
        current_files[path] = current
        if current:
            decoded = base64.b64decode(current.get("content", "")).decode()
            if decoded != content:
                raise RunnerError(f"broker has an incompatible managed file: {path}")
    try:
        existing = _api(f"repos/{repository}/contents")
    except RunnerError as exc:
        if "404" not in str(exc):
            raise
        existing = []
    if existing and not current_files[".t-runner-protocol.json"]:
        raise RunnerError("broker repository is not empty and is not managed by t runners")
    for path in sorted(templates, key=lambda item: item != ".t-runner-protocol.json"):
        content = templates[path]
        if not current_files[path]:
            try:
                _api(f"repos/{repository}/contents/{path}", "PUT", {
                    "message": "Install private t runner protocol", "content":
                    base64.b64encode(content.encode()).decode()})
            except RunnerError as exc:
                if path.startswith(".github/workflows/") and any(code in str(exc) for code in ("403", "404")):
                    raise RunnerError("GitHub refused workflow installation; the credential needs workflow write permission. "
                                      "For a classic gh OAuth token, explicitly choose `gh auth refresh -s workflow`, then retry setup.") from exc
                raise
    binding = {"repository": repository, "repository_id": remote["id"],
               "protocol": PROTOCOL, "configured_at": time.time()}
    path = state_path(state_dir) / "brokers.json"
    bindings = _read(path, {})
    bindings[repository] = binding
    _write(path, bindings)
    return binding


def _broker(repository, state_dir):
    binding = _read(state_path(state_dir) / "brokers.json", {}).get(repository)
    if not binding or binding.get("protocol") != PROTOCOL:
        raise RunnerError(f"run t config workflow runners setup for private broker {repository} first")
    remote = _private(repository, binding["repository_id"])
    for path, expected in _templates().items():
        item = _api(f"repos/{repository}/contents/{path}")
        if base64.b64decode(item.get("content", "")).decode() != expected:
            raise RunnerError("runner broker protocol changed; refusing dispatch")
    workflow = _api(f"repos/{repository}/actions/workflows/t-runner.yml")
    if workflow.get("path") != WORKFLOW or workflow.get("state") != "active":
        raise RunnerError("private broker workflow is not active")
    return {**binding, "branch": remote.get("default_branch", "main"),
            "workflow_id": workflow["id"]}


def _normalized_os(value):
    return {"macos": "darwin", "windows": "windows"}.get(str(value).lower(), str(value).lower())


def _normalized_arch(value):
    return {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(str(value).lower(), str(value).lower())


def select_targets(config, targets=None, os_filter=None, capabilities=None):
    section = config.get("runners", config)
    available = section.get("targets", {})
    names = targets if targets is not None else section.get("default_targets", list(available))
    if isinstance(names, str):
        names = [names]
    if not isinstance(available, dict) or not isinstance(names, (list, tuple)):
        raise RunnerError("runner targets must be a mapping and selection a list")
    selected = {}
    required = set(capabilities or ())
    for name in names:
        if not isinstance(name, str) or not NAME.fullmatch(name) or name not in available:
            raise RunnerError(f"unknown runner target: {name}")
        target = dict(available[name])
        kind = target.get("provider", target.get("type", target.get("kind")))
        if kind not in {"github", "ssh"}:
            raise RunnerError(f"invalid runner transport for {name}")
        target["type"] = kind
        if _normalized_os(target.get("os")) not in {"linux", "darwin"}:
            raise RunnerError(f"unsupported runner OS for {name}")
        if _normalized_arch(target.get("arch")) not in {"arm64", "x86_64"}:
            raise RunnerError(f"unsupported runner architecture for {name}")
        if kind == "ssh" and not HOST.fullmatch(str(target.get("host", ""))):
            raise RunnerError(f"invalid SSH host for {name}")
        if kind == "github" and (not REPO.fullmatch(str(target.get("repository", "")))
                                 or not isinstance(target.get("label"), (str, list))):
            raise RunnerError(f"invalid private GitHub target for {name}")
        if os_filter and _normalized_os(target["os"]) != _normalized_os(os_filter):
            continue
        if not required.issubset(set(target.get("capabilities", []))):
            continue
        selected[name] = target
    if not selected:
        raise RunnerError("no runner targets match the selection")
    return selected


def _job_dir(job_id, state_dir=None):
    try:
        parsed = uuid.UUID(str(job_id))
    except (ValueError, AttributeError) as exc:
        raise RunnerError("runner job ID must be a UUID") from exc
    if str(parsed) != str(job_id):
        raise RunnerError("runner job ID must be a canonical UUID")
    return state_path(state_dir) / "jobs" / str(parsed)


def status(job_id, state_dir=None):
    receipt = _read(_job_dir(job_id, state_dir) / "receipt.json")
    if not receipt:
        raise RunnerError(f"unknown runner job: {job_id}")
    return receipt


def list_jobs(state_dir=None):
    root = state_path(state_dir) / "jobs"
    return sorted((_read(path) for path in root.glob("*/receipt.json")),
                  key=lambda item: item["created_at"], reverse=True)


def logs(job_id, target=None, state_dir=None):
    receipt = status(job_id, state_dir)
    names = [target] if target else receipt["targets"]
    result = {}
    for name in names:
        if name not in receipt["targets"]:
            raise RunnerError(f"unknown target in job: {name}")
        path = _job_dir(job_id, state_dir) / name / "output.log"
        result[name] = path.read_text(errors="replace") if path.exists() else ""
    return result


def artifacts(job_id, target=None, destination=None, state_dir=None):
    receipt = status(job_id, state_dir)
    names = [target] if target else receipt["targets"]
    result = {}
    for name in names:
        if name not in receipt["targets"]:
            raise RunnerError(f"unknown target in job: {name}")
        path = _job_dir(job_id, state_dir) / name / "artifacts.tar"
        if path.is_file():
            if destination:
                out = Path(destination) / f"{job_id}-{name}"
                records = receipt["targets"][name].get("result", {}).get("artifacts")
                _extract_artifacts(path, out, records)
                result[name] = str(out)
            else:
                result[name] = str(path)
    return result


def _extract_artifacts(archive, destination, records):
    if not isinstance(records, list):
        raise RunnerError("artifact result manifest unavailable; cannot verify bundle")
    try:
        manifest = {"version": 1, "id": str(uuid.uuid4()), "files": records,
                    "total_bytes": sum(record["size"] for record in records)}
    except (KeyError, TypeError) as exc:
        raise RunnerError("invalid artifact result manifest") from exc
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _helper().extract_snapshot(archive, manifest, destination)


def _save(receipt, directory):
    with _LOCK:
        receipt["updated_at"] = time.time()
        _write(directory / "receipt.json", receipt)


def _ssh(host, command, timeout=60):
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise RunnerError("invalid SSH runner host in receipt")
    return _run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ForwardAgent=no",
                 "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no", "--", host,
                 "zsh -lic " + shlex.quote(command)], timeout=timeout)


def _helper():
    path = Path(__file__).with_name("t_runner_job.py")
    spec = importlib.util.spec_from_file_location("t_runner_job", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(target, path):
    if Path(path).stat().st_size > 8 * 1024 * 1024:
        raise RunnerError("runner result metadata exceeds limit")
    value = _read(path)
    if (not isinstance(value, dict) or value.get("status") not in TERMINAL
            or not (isinstance(value.get("exit_code"), int) and not isinstance(value.get("exit_code"), bool)
                    or value.get("status") == "error" and value.get("exit_code") is None)):
        raise RunnerError("runner did not return a valid command result")
    if value["status"] == "succeeded" and value["exit_code"] != 0:
        raise RunnerError("runner returned an inconsistent success result")
    if value.get("id") != target.get("token") or (value["status"] != "error"
            and value.get("snapshot_id") != target.get("snapshot_id")):
        raise RunnerError("runner result does not match the job token and snapshot")
    target.update(status=value["status"], exit_code=value["exit_code"] if value["exit_code"] is not None else 1, result=value)


def _remote_path(receipt, name, target):
    expected = f"/tmp/t-runner-{receipt['id']}-{name}"
    _job_dir(receipt["id"])
    if not NAME.fullmatch(name) or target.get("remote_path") != expected:
        raise RunnerError("SSH job path does not match the exact job identity")
    return expected


def _download_ssh_result(target, name, directory):
    probe = ("import os, stat, sys\nroot = sys.argv[1]\nlimits = " + repr(RESULT_LIMITS) + "\n"
             "for name, limit in limits.items():\n"
             " try: item = os.lstat(os.path.join(root, name))\n"
             " except FileNotFoundError:\n"
             "  if name == 'result.json': raise\n"
             "  continue\n"
             " if not stat.S_ISREG(item.st_mode) or not 0 <= item.st_size <= limit:\n"
             "  raise ValueError('unsafe or oversized runner output')\n")
    _ssh(target["host"], shlex.join(["python3", "-c", probe, target["remote_path"]]), timeout=10)
    for filename in ("result.json", "output.log", "artifacts.tar"):
        try:
            _run(["scp", "-q", "-o", "BatchMode=yes", "--",
                  f"{target['host']}:{target['remote_path']}/{filename}", str(directory / name / filename)])
            if (directory / name / filename).stat().st_size > RESULT_LIMITS[filename]:
                raise RunnerError("SSH runner output exceeds transfer limit")
        except RunnerError:
            if filename == "result.json":
                raise RunnerError("SSH runner result unavailable")
    _result(target, directory / name / "result.json")


def _ssh_target(receipt, name, target, directory, timeout):
    remote = _remote_path(receipt, name, target)
    host = target["host"]
    detected = _ssh(host, "uname -s; uname -m").splitlines()
    if len(detected) != 2 or (_normalized_os(detected[0]), _normalized_arch(detected[1])) != (
            _normalized_os(target["os"]), _normalized_arch(target["arch"])):
        raise RunnerError("SSH runner OS/architecture does not match target")
    _ssh(host, f"umask 077; mkdir -- {shlex.quote(remote)} && printf '%s\\n' {shlex.quote(target['token'])} > {shlex.quote(remote + '/token')}")
    target["resource_created"] = True
    _save(receipt, directory)
    files = [directory / "snapshot.tar", directory / "manifest.json",
             Path(__file__).with_name("t_runner_job.py"), directory / f"{name}.json"]
    _run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", "--",
          *map(str, files), f"{host}:{remote}/"])
    if (directory / "cancel.requested").exists():
        target.update(status="cancelled", exit_code=130)
        return
    spec = _read(directory / f"{name}.json")
    args = ["python3", f"{remote}/t_runner_job.py", "run", "--job-dir", remote,
            "--archive", f"{remote}/snapshot.tar", "--manifest", f"{remote}/manifest.json",
            "--token", target["token"], "--timeout", str(timeout), "--os", target["os"],
            "--arch", target["arch"]]
    for path in spec["artifacts"]:
        args += ["--artifact", path]
    target["status"] = "running"
    _save(receipt, directory)
    # The deadline owner must survive a dropped SSH connection. Commands inherit
    # neither the login session's terminal nor its standard input/output pipes.
    launch = ("nohup " + shlex.join(args + ["--", *spec["command"]]) + " > "
              + shlex.quote(remote + "/transport.log") + " 2>&1 < /dev/null &")
    _ssh(host, launch, timeout=30)
    deadline = time.monotonic() + timeout + 30
    cancellation_sent = False
    last_error = "SSH runner result unavailable"
    while time.monotonic() < deadline:
        if (directory / "cancel.requested").exists() and not cancellation_sent:
            _ssh(host, shlex.join(["python3", remote + "/t_runner_job.py", "cancel",
                                  "--job-dir", remote, "--token", target["token"]]), timeout=10)
            cancellation_sent = True
            deadline = min(deadline, time.monotonic() + 15)
        try:
            _ssh(host, "test -f " + shlex.quote(remote + "/result.json"), timeout=10)
            _download_ssh_result(target, name, directory)
            return
        except RunnerError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RunnerError("SSH terminal state is unverified: " + last_error)


def _find_run(target, job_id, name):
    title = f"t-runner-{job_id}-{name}"
    rows = []
    # A burst of concurrent dispatches must not hide this job behind page one.
    for page in range(1, 101):
        data = _api(f"repos/{target['repository']}/actions/workflows/{target['workflow_id']}/runs?event=workflow_dispatch&per_page=100&page={page}")
        batch = data.get("workflow_runs", [])
        rows.extend(row for row in batch if row.get("display_title") == title
                    and row.get("workflow_id") == target["workflow_id"])
        if rows or len(batch) < 100:
            break
    if len(rows) > 1:
        raise RunnerError("multiple broker runs claimed one job target")
    return rows[0] if rows else None


def _assets(repository, release_id):
    assets = []
    for page in range(1, 101):
        batch = _api(f"repos/{repository}/releases/{release_id}/assets?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RunnerError("GitHub returned an invalid release asset inventory")
        assets.extend(batch)
        if len(batch) < 100:
            return assets
    raise RunnerError("private broker release asset inventory exceeds limit")


def _download_result(target, name, directory):
    assets = _assets(target["repository"], target["release_id"])
    for filename in ("result.json", "output.log", "artifacts.tar"):
        limit = RESULT_LIMITS[filename]
        matches = [asset for asset in assets if asset.get("name") == f"{name}-{filename}"]
        if not matches and filename != "result.json":
            continue
        if (len(matches) != 1 or type(matches[0].get("id")) is not int
                or type(matches[0].get("size")) is not int or not 0 <= matches[0]["size"] <= limit):
            raise RunnerError("broker result asset missing, ambiguous, or exceeds limit")
        asset = matches[0]
        try:
            destination = directory / name / filename
            _run(["gh", "api", f"repos/{target['repository']}/releases/assets/{asset['id']}",
                  "-H", "Accept: application/octet-stream"], output_path=destination)
            if destination.stat().st_size != asset["size"]:
                raise RunnerError("broker result asset size changed during transfer")
        except RunnerError:
            if filename == "result.json":
                raise RunnerError("broker completed without a command result")
    _result(target, directory / name / "result.json")


def _cancel_github(target, run, wait_seconds=15):
    """A successful cancel request is not evidence that execution stopped."""
    repository = target["repository"]
    if not run:
        return False
    if run.get("status") != "completed":
        _api(f"repos/{repository}/actions/runs/{run['id']}/cancel", "POST")
    deadline = time.monotonic() + wait_seconds
    while run.get("status") != "completed":
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
        run = _api(f"repos/{repository}/actions/runs/{run['id']}")
    target["provider_conclusion"] = run.get("conclusion")
    return True


def _github_target(receipt, name, target, directory, timeout, queue_timeout):
    repository = target["repository"]
    labels = target["label"] if isinstance(target["label"], list) else [target["label"]]
    # A lost dispatch response is ambiguous: retain the exact correlation ID.
    target["dispatched"] = True
    _save(receipt, directory)
    _api(f"repos/{repository}/actions/workflows/{target['workflow_id']}/dispatches", "POST", {
        "ref": target["branch"], "inputs": {"job_id": receipt["id"], "target": name,
        "release_id": str(target["release_id"]), "labels": json.dumps(labels),
        "asset_ids": json.dumps(target["asset_ids"]),
        "archive_sha256": receipt["snapshot"]["archive_sha256"],
        "manifest_sha256": receipt["snapshot"]["manifest_sha256"],
        "timeout_minutes": str(min(360, max(1, (timeout + 119) // 60)))}})
    _save(receipt, directory)
    queued_until = time.monotonic() + queue_timeout
    running_until = None
    while True:
        run = _find_run(target, receipt["id"], name)
        if run:
            target["run_id"] = run["id"]
            target["url"] = run.get("html_url")
            if run.get("status") == "completed":
                break
            if run.get("status") == "in_progress" and running_until is None:
                running_until = time.monotonic() + timeout + 120
                target["status"] = "running"
                _save(receipt, directory)
        if (directory / "cancel.requested").exists() or time.monotonic() > (running_until or queued_until):
            if _cancel_github(target, run):
                try:
                    _download_result(target, name, directory)
                except RunnerError:
                    interrupted = (directory / "cancel.requested").exists()
                    if target.get("provider_conclusion") == "cancelled":
                        target.update(status="cancelled" if interrupted else "timed_out",
                                      exit_code=130 if interrupted else 124)
                    else:
                        target.update(status="error", exit_code=1,
                                      error="provider terminated without a command result")
                return
            target["status"] = "unknown"
            target["error"] = "cancellation requested; terminal provider state is not yet verified"
            return
        time.sleep(2)
    target["provider_conclusion"] = run.get("conclusion")
    # Provider success alone never establishes the command's exit status.
    _download_result(target, name, directory)


def start(root, config, command, targets=None, os_filter=None, capabilities=None,
          include=None, artifacts=None, timeout=None, queue_timeout=None, state_dir=None, progress=None):
    """Wait for concurrent targets, retaining a durable receipt even on failure."""
    section = config.get("runners", config)
    selected = select_targets(config, targets, os_filter, capabilities)
    if not isinstance(command, (list, tuple)) or not command or any(
            not isinstance(value, str) or "\0" in value for value in command):
        raise RunnerError("runner command must be a nonempty argv list")
    timeout = timeout if timeout is not None else section.get("timeout_seconds", section.get("timeout", 1800))
    queue_timeout = queue_timeout if queue_timeout is not None else section.get("queue_timeout_seconds", section.get("queue_timeout", 300))
    if (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 21600
            or not isinstance(queue_timeout, int) or isinstance(queue_timeout, bool) or not 1 <= queue_timeout <= 3600):
        raise RunnerError("runner deadlines require execution 1..21600 and queue 1..3600 seconds")
    job_id = str(uuid.uuid4())
    directory = _job_dir(job_id, state_dir)
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    receipt = {"id": job_id, "protocol": PROTOCOL, "created_at": time.time(),
               "root": str(Path(root).resolve()), "command": list(command), "status": "preparing",
               "targets": {}, "timeout": timeout, "queue_timeout": queue_timeout, "resources": []}
    for name, spec in selected.items():
        receipt["targets"][name] = {**spec, "status": "queued", "token": str(uuid.uuid4()),
                                    "remote_path": f"/tmp/t-runner-{job_id}-{name}"}
        (directory / name).mkdir(parents=True, mode=0o700)
    _save(receipt, directory)
    if progress:
        progress(receipt)
    try:
        receipt["snapshot"] = _helper().create_snapshot(Path(root), directory / "snapshot.tar",
            directory / "manifest.json", include=list(dict.fromkeys([*section.get("include", ()), *(include or [])])))
        for filename, key in (("snapshot.tar", "archive_sha256"), ("manifest.json", "manifest_sha256")):
            receipt["snapshot"][key] = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
        for name, target in receipt["targets"].items():
            target["snapshot_id"] = receipt["snapshot"]["id"]
            _write(directory / f"{name}.json", {"command": list(command), "timeout": timeout,
                "os": target["os"], "arch": target["arch"], "token": target["token"],
                "artifacts": list(dict.fromkeys([*section.get("artifacts", []), *(artifacts or [])]))})
        repositories = {t["repository"] for t in selected.values() if t["type"] == "github"}
        for repository in repositories:
            binding = _broker(repository, state_dir)
            tag = f"t-runner-{job_id}"
            # Record a deterministic locator before the first remote mutation.
            resource = {"type": "github", "repository": repository, "repository_id": binding["repository_id"],
                        "tag": tag, "expires_at": time.time() + 86400, "status": "creating"}
            receipt["resources"].append(resource)
            _save(receipt, directory)
            release = _api(f"repos/{repository}/releases", "POST", {"tag_name": tag,
                "name": tag, "body": "Ephemeral private runner snapshot; expires after 24 hours.",
                "draft": False, "prerelease": True})
            resource.update(id=release["id"], status="created")
            _save(receipt, directory)
            files = [directory / "snapshot.tar", directory / "manifest.json", Path(__file__).with_name("t_runner_job.py")]
            files += [directory / f"{name}.json" for name, t in selected.items() if t.get("repository") == repository]
            _run(["gh", "release", "upload", tag, "--repo", repository, *map(str, files)])
            assets = _assets(repository, release["id"])
            resource["assets"] = {asset["name"]: asset["id"] for asset in assets}
            if len(resource["assets"]) != len(assets) or any(path.name not in resource["assets"] for path in files):
                raise RunnerError("broker release assets are incomplete or ambiguous")
            _save(receipt, directory)
            for name, target in receipt["targets"].items():
                if target.get("repository") == repository:
                    target.update(binding, release_id=release["id"], release_tag=tag,
                        asset_ids={filename: resource["assets"][filename] for filename in (
                            "snapshot.tar", "manifest.json", "t_runner_job.py", name + ".json")})
        receipt["status"] = "running"
        _save(receipt, directory)

        def execute(name, target):
            try:
                if (directory / "cancel.requested").exists():
                    target.update(status="cancelled", exit_code=130)
                    return
                if target["type"] == "ssh":
                    _ssh_target(receipt, name, target, directory, timeout)
                else:
                    _github_target(receipt, name, target, directory, timeout, queue_timeout)
            except Exception as exc:
                target.update(status="unknown" if target.get("dispatched") or target.get("resource_created") else "error",
                              error=str(exc), exit_code=1)
            finally:
                _save(receipt, directory)

        pool = ThreadPoolExecutor(max_workers=len(selected))
        try:
            list(pool.map(lambda pair: execute(*pair), receipt["targets"].items()))
        except KeyboardInterrupt:
            # Persist and send cancellation before waiting for worker threads.
            cancel(job_id, state_dir)
            receipt["interrupted"] = True
        finally:
            pool.shutdown(wait=True)
        values = list(receipt["targets"].values())
        receipt["status"] = "succeeded" if all(t["status"] == "succeeded" for t in values) else (
            "unknown" if any(t["status"] == "unknown" for t in values) else "failed")
        receipt["exit_code"] = next((t.get("exit_code", 1) or 1 for t in values if t["status"] != "succeeded"), 0)
        if receipt.get("interrupted"):
            receipt["exit_code"] = 130
    except KeyboardInterrupt:
        interrupted = cancel(job_id, state_dir)
        # Keep newer in-memory resource locators while merging cancellation state.
        for name, target in interrupted["targets"].items():
            receipt["targets"][name].update(target)
        receipt.update(interrupted=True, exit_code=130, status=interrupted["status"])
    except Exception as exc:
        receipt.update(status="error", exit_code=1, error=str(exc))
        for target in receipt["targets"].values():
            if not target.get("dispatched") and not target.get("resource_created"):
                target.update(status="error", exit_code=1)
    finally:
        _save(receipt, directory)
        _cleanup_receipt(receipt, directory, terminal_only=True)
        _save(receipt, directory)
    return receipt


def _cleanup_receipt(receipt, directory, terminal_only=True):
    for name, target in receipt["targets"].items():
        if target["type"] == "ssh" and target.get("resource_created") and not target.get("cleaned"):
            if terminal_only and target["status"] not in TERMINAL:
                continue
            try:
                _ssh(target["host"], "rm -rf -- " + shlex.quote(_remote_path(receipt, name, target)))
                target["cleaned"] = True
            except RunnerError as exc:
                target["cleanup_error"] = str(exc)
    for resource in receipt["resources"]:
        related = [t for t in receipt["targets"].values() if t.get("repository") == resource["repository"]]
        if resource.get("status") == "deleted" or terminal_only and any(t["status"] not in TERMINAL for t in related):
            continue
        try:
            _private(resource["repository"], resource["repository_id"])
            _run(["gh", "release", "delete", resource["tag"], "--repo", resource["repository"], "--yes", "--cleanup-tag"])
            resource["status"] = "deleted"
        except RunnerError as exc:
            resource["cleanup_error"] = str(exc)
    if all(t["status"] in TERMINAL for t in receipt["targets"].values()) or receipt["status"] == "error":
        for filename in ("snapshot.tar", "manifest.json"):
            (directory / filename).unlink(missing_ok=True)


def cancel(job_id, state_dir=None):
    receipt = status(job_id, state_dir)
    directory = _job_dir(job_id, state_dir)
    _write(directory / "cancel.requested", {"requested_at": time.time()})
    for name, target in receipt["targets"].items():
        if target["status"] in TERMINAL:
            continue
        try:
            if target["type"] == "github" and not target.get("dispatched"):
                target.update(status="cancelled", exit_code=130)
            elif target["type"] == "github":
                _private(target["repository"], target.get("repository_id"))
                run = _find_run(target, job_id, name)
                if _cancel_github(target, run):
                    try:
                        _download_result(target, name, directory)
                    except RunnerError:
                        if target.get("provider_conclusion") == "cancelled":
                            target.update(status="cancelled", exit_code=130)
                        else:
                            target.update(status="error", exit_code=1,
                                          error="provider terminated without a command result")
                else:
                    target["status"] = "unknown"
            elif target.get("resource_created"):
                _remote_path(receipt, name, target)
                _ssh(target["host"], shlex.join(["python3", target["remote_path"] + "/t_runner_job.py",
                    "cancel", "--job-dir", target["remote_path"], "--token", target["token"]]))
                target["status"] = "unknown"
            else:
                target["status"] = "cancelled"
                target["exit_code"] = 130
            target["cancellation_requested"] = True
        except RunnerError as exc:
            target.update(status="unknown", error=str(exc))
    receipt["status"] = "unknown" if any(t["status"] == "unknown" for t in receipt["targets"].values()) else receipt["status"]
    _save(receipt, directory)
    return receipt


def cleanup(job_id=None, state_dir=None):
    receipts = [status(job_id, state_dir)] if job_id else list_jobs(state_dir)
    for receipt in receipts:
        directory = _job_dir(receipt["id"], state_dir)
        for name, target in receipt["targets"].items():
            # A controller may disappear before changing its durable running or
            # queued state to unknown. Reconcile every unfinished receipt, and
            # let terminal-only cleanup retain resources that are still active.
            if target["status"] in TERMINAL:
                continue
            try:
                if target["type"] == "github":
                    if not target.get("dispatched"):
                        continue
                    if not all(target.get(field) for field in ("repository_id", "workflow_id", "release_id")):
                        raise RunnerError("GitHub dispatch metadata is incomplete; refusing reconciliation")
                    _private(target["repository"], target["repository_id"])
                    run = _find_run(target, receipt["id"], name)
                    if run and run.get("status") == "completed":
                        try:
                            _download_result(target, name, directory)
                        except RunnerError:
                            if run.get("conclusion") == "cancelled":
                                target.update(status="cancelled", exit_code=130)
                            else:
                                target.update(status="error", exit_code=1,
                                              error="provider terminated without a command result")
                else:
                    if not target.get("resource_created"):
                        continue
                    _remote_path(receipt, name, target)
                    _download_ssh_result(target, name, directory)
            except (RunnerError, ValueError) as exc:
                target["cleanup_error"] = str(exc)
        _cleanup_receipt(receipt, directory)
        if all(t["status"] in TERMINAL for t in receipt["targets"].values()):
            receipt["status"] = "succeeded" if all(t["status"] == "succeeded" for t in receipt["targets"].values()) else "failed"
            receipt["exit_code"] = next((t.get("exit_code", 1) or 1 for t in receipt["targets"].values()
                                         if t["status"] != "succeeded"), 0)
        _save(receipt, directory)
    return receipts[0] if job_id else receipts
