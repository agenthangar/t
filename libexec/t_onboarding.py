"""Reviewed first-run setup. Discovery is read-only; applying a plan is explicit."""

from contextlib import contextmanager
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap


class Aborted(Exception):
    """The user left a setup screen without applying it."""


def _marker():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "t" / "onboarding.json"


def needed(cfg, api):
    """Existing configured accounts keep their normal launch behavior."""
    try:
        return json.loads(_marker().read_text()).get("version") != 1
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        return True
    _, state = api["_shell_setup_helpers"]()["state"](api["HOME"], os.environ)
    return not (cfg.repos and state == "configured")


def _complete(agent, *, completed=True):
    path = _marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump({"version": 1 if completed else 0, "agent": agent}, stream)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class Dialog:
    """Use the shared rail UI, with a cooked fallback when raw mode is unavailable."""

    def __init__(self, api):
        self.ui = None
        try:
            self.ui = api["_RailUI"]()
            self.ui.raw()
        except Exception:
            if self.ui is not None:
                self.ui.restore()
            self.ui = None

    def close(self):
        if self.ui is not None:
            self.ui.restore()

    @contextmanager
    def suspended(self):
        if self.ui is not None:
            self.ui.commit([])
            self.ui.restore()
        try:
            yield
        finally:
            if self.ui is not None:
                self.ui.raw()

    def notice(self, text):
        if self.ui is not None:
            self.ui.done("setup", text)
        else:
            print(text)

    def choose(self, label, rows, *, multi=False, marked=()):
        if not rows:
            return [] if multi else None
        if self.ui is not None:
            result = self.ui.pick(label, rows, multi=multi, marked=marked,
                                  primary=("enter", "CONTINUE", ""))
            if result is None:
                raise Aborted
            return result
        print("\n" + label)
        for index, (_, text) in enumerate(rows, 1):
            mark = " [x]" if multi and index - 1 in marked else ""
            print(f"  {index}. {text}{mark}")
        while True:
            answer = input("Numbers separated by commas (Enter keeps marks, - selects none, q quits): "
                           if multi else "Number (q quits): ").strip()
            if answer.lower() in ("q", "quit"):
                raise Aborted
            if multi and answer in ("", "-"):
                return [rows[i][0] for i in marked] if not answer else []
            try:
                indexes = list(dict.fromkeys(int(n.strip()) - 1 for n in answer.split(",")))
                if (not multi and len(indexes) != 1) or any(i < 0 or i >= len(rows) for i in indexes):
                    raise ValueError
                return [rows[i][0] for i in indexes] if multi else rows[indexes[0]][0]
            except ValueError:
                print("Choose one of the displayed numbers.")

    def review(self, lines):
        if self.ui is not None:
            body = ["      " + part for line in lines
                    for part in (textwrap.wrap(line, width=max(10, self.ui.cols() - 6)) or [""])]
            key = self.ui.page("Review setup changes", body,
                               {"y", "n", "q", "esc", "abort"},
                               keys="y apply · n change choices · q quit",
                               primary=("y", "APPLY SETUP", ""))
        else:
            print("\nReview setup changes\n" + "\n".join("  " + line for line in lines))
            key = input("Apply these changes? [y/n/q] ").strip().lower()
        if key in ("q", "esc", "abort"):
            raise Aborted
        return key == "y"


def _discovery(api):
    return runpy.run_path(os.path.join(api["_t_source_root"](), "libexec", "t_discovery.py"))


def _organizations(dialog, discovery):
    state = discovery["github_auth_state"]()
    if state == "missing":
        command = discovery["github_install_command"]()
        if command:
            choice = dialog.choose("Install GitHub CLI to discover organization repositories?", [
                ("install", "Run " + shlex.join(command)), ("skip", "Skip GitHub for now")])
            if choice == "skip":
                return []
            with dialog.suspended():
                try:
                    installed = subprocess.run(command).returncode == 0
                except OSError:
                    installed = False
            if not installed:
                dialog.notice("GitHub CLI installation did not finish; continuing with local setup.")
                return []
            state = discovery["github_auth_state"]()
        if state == "missing":
            dialog.notice("Install GitHub CLI from https://cli.github.com, then run t config onboard.")
            return []
    if state != "authenticated":
        choice = dialog.choose("Connect GitHub to discover organization repositories?", [
            ("login", "Run gh auth login --hostname github.com"), ("skip", "Skip GitHub for now")])
        if choice == "skip":
            return []
        with dialog.suspended():
            try:
                result = subprocess.run(["gh", "auth", "login", "--hostname", "github.com"])
                ok = result.returncode == 0
            except OSError:
                ok = False
        if not ok:
            dialog.notice("GitHub sign-in did not finish; continuing with local setup.")
            return []
    orgs, warnings = discovery["github_orgs"]()
    for warning in warnings:
        dialog.notice(warning)
    return orgs


def _agent_plan(api, selected, probed):
    selection = {"install": [a for a in selected if not probed[a].get("version")],
                 "login": [a for a in selected if not probed[a].get("logged_in")],
                 "reinstall": [], "update": False, "hosts": []}
    steps = api["_install_plan"](selection, probed,
                "darwin" if sys.platform == "darwin" else "linux", bool(shutil.which("brew")),
                api["_install_headless"](os.environ, False), [])
    # The final integration step covers links/hooks once, after agent installation.
    return [step for step in steps if step["step"] != "sync"]


def _repo_aliases(api, paths, taken):
    proposed, _ = api["_propose_aliases"](paths, taken)
    used, aliases = set(taken), {}
    for path, value in proposed.items():
        base = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip(".-") or "repo"
        alias, number = base, 2
        while alias in used or alias in ("__add__", "__back__"):
            alias, number = f"{base}-{number}", number + 1
        used.add(alias)
        aliases[path] = alias
    return aliases


def _collect(cfg, api, dialog, discovery):
    probed = api["_install_probe"]()
    agents = list(api["INSTALL_AGENTS"])
    selected = dialog.choose("Choose agents to use (missing tools will be installed)", [
        (agent, f"{agent} · " + ("installed" if probed[agent].get("version") else "install") +
         (" · chats only" if agent not in api["AGENTS"] else ""))
        for agent in agents], multi=True,
        marked=[i for i, agent in enumerate(agents) if probed[agent].get("version")])
    launchable = [agent for agent in selected if agent in api["AGENTS"]]
    agent = (dialog.choose("Default agent for new sessions", [(a, a) for a in launchable])
             if len(launchable) > 1 else next(iter(launchable), None))
    steps = _agent_plan(api, selected, probed)
    home = Path(api["HOME"])
    current = api["_run"](["git", "rev-parse", "--show-toplevel"], timeout=5)
    cwd = current.stdout.strip() if current.returncode == 0 else os.getcwd()
    roots = list(dict.fromkeys([str(home / "code"), str(home / "config"), cwd,
                               *cfg.repos.values()]))
    local = discovery["discover_local"](roots, [cfg.worktree_root])
    registered = {os.path.realpath(path) for path in cfg.repos.values()}
    candidates = [repo for repo in local if os.path.realpath(repo["path"]) not in registered]
    chosen = dialog.choose("Register existing checkouts", [
        (i, repo["path"]) for i, repo in enumerate(candidates)], multi=True,
        marked=range(len(candidates)))
    repos = [candidates[i] for i in chosen]
    orgs = _organizations(dialog, discovery)
    org_choices = dialog.choose("Organizations to browse (optional)", [(o, o) for o in orgs], multi=True)
    remote = []
    identities = {repo["identity"]: repo["path"] for repo in local if repo.get("identity")}
    for org in org_choices:
        found, warnings = discovery["github_repos"](org)
        for warning in warnings:
            dialog.notice(warning)
        remote.extend(found)
    clone_rows, clone_candidates = [], []
    for repo in remote:
        identity = repo["full_name"].lower()
        if identity in identities:
            if not any(os.path.realpath(r["path"]) == os.path.realpath(identities[identity]) for r in repos):
                dialog.notice(f"Already cloned: {repo['full_name']} → {identities[identity]}")
            continue
        target = discovery["clone_destination"](repo["full_name"])
        suffix = " · archived" if repo.get("archived") else " · fork" if repo.get("fork") else ""
        clone_rows.append((len(clone_candidates), f"{repo['full_name']}{suffix} → {target}"))
        clone_candidates.append({**repo, "path": target})
    picked = dialog.choose("Clone repositories (none selected by default)", clone_rows, multi=True)
    clones = [clone_candidates[i] for i in picked]
    hosts, warnings = discovery["tailscale_hosts"]()
    for warning in warnings:
        dialog.notice(warning)
    targets = set(cfg.hosts.values())
    aliases = set(cfg.hosts)
    available = []
    for host in hosts:
        if host["target"] in targets:
            continue
        alias = api["_host_short_name"](host["alias"], aliases)
        aliases.add(alias)
        targets.add(host["target"])
        available.append({**host, "alias": alias})
    chosen_hosts = dialog.choose("Optional Tailscale hosts (SSH access is not verified)", [
        (i, f"{host['alias']} → {host['target']}") for i, host in enumerate(available)], multi=True)
    proposals = _repo_aliases(api, [r["path"] for r in repos + clones], cfg.repos)
    return {"agent": agent, "selected": selected, "steps": steps, "repos": repos,
            "clones": clones, "aliases": proposals, "hosts": [available[i] for i in chosen_hosts]}


def _review(plan, api):
    lines = ["Connect t to this account:",
             "  Add t's zsh startup block and command/helper links; preserve existing settings.",
             "  Add agent prompts, session hooks, and public-repository workflow instructions.",
             "  Register Claude sessions MCP when available; hook approval stays with the agent."]
    if plan["agent"]:
        lines.append(f"Default agent: {plan['agent']} (repository overrides are preserved)")
    lines.extend(api["_install_render"](plan["steps"], api["Style"](tty=False)))
    for repo in plan["repos"]:
        lines.append(f"Register {plan['aliases'][repo['path']]} → {repo['path']}")
    for repo in plan["clones"]:
        lines.append(f"Clone {repo['full_name']} → {repo['path']} and register {plan['aliases'][repo['path']]}")
    for host in plan["hosts"]:
        lines.append(f"Register host {host['alias']} → {host['target']} (no remote commands)")
    lines.append("Existing checkouts are preserved. Completed steps are kept if a later step fails.")
    return lines


def _save_settings(cfg, api, agent, hosts):
    local = api["_current_local_rc"]()
    before = api["_config_read"](local)
    initial = api["_config_state"](cfg)
    if agent:
        cfg.agent_default = agent
    for host in hosts:
        if cfg.hosts.get(host["alias"]) == host["target"]:
            continue
        api["_host_change"](cfg, "add", host["alias"], host["target"])
    changes = api["_config_diff"](initial, api["_config_state"](cfg))
    if changes:
        api["_config_write"](local, before, api["_config_text"](before, changes, initial))
        api["_host_refresh_config"]()


def _apply(cfg, api, discovery, plan):
    # Revalidate selected existing paths before any mutation, then reuse the public
    # registration checks after each clone. A checkout is never silently replaced.
    checked = []
    for repo in plan["repos"] + plan["clones"]:
        path = repo["path"]
        if repo in plan["repos"]:
            _, path = api["_repo_add_target"](cfg, path, plan["aliases"][path])
        conflict = api["_repo_basename_conflict"](path, [*cfg.repos.values(), *checked])
        if conflict:
            raise ValueError(f"checkout directory names collide: {path} and {conflict}; choose one checkout")
        checked.append(path)
    # An interrupted first run must remain distinguishable from older accounts
    # that were already configured before the guided setup existed.
    _complete(plan["agent"], completed=False)
    if plan["steps"] and api["_install_walk"](plan["steps"]):
        return 1
    if api["cmd_integrate"](cfg, argparse.Namespace()):
        return 1
    api["_host_refresh_config"]()
    current = api["Config"]()
    _save_settings(current, api, plan["agent"], plan["hosts"])
    for repo in plan["repos"] + plan["clones"]:
        if "full_name" in repo:
            ok, message = discovery["clone_repo"](repo["full_name"], repo["path"])
            print(message)
            if not ok:
                return 1
        rc = api["cmd_repo_add"](api["Config"](), argparse.Namespace(
            path=repo["path"], alias=plan["aliases"][repo["path"]], dry_run=False))
        if rc:
            return rc
    _complete(plan["agent"])
    return 0


def run(cfg, api, *, force=False):
    """Bare-terminal onboarding; explicit reruns discover newly cloned repositories."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Run `t config onboard` in a terminal to review setup; no changes made.")
        return 1
    if not force and not needed(cfg, api):
        return api["_onboarding_start"](cfg, None)
    dialog = Dialog(api)
    try:
        action = dialog.choose("Welcome to t", [("setup", "Set up agents and repositories"),
                                                ("later", "Not now")])
        if action == "later":
            dialog.notice("Run t again when ready, or use t config onboard to revisit setup.")
            return 0
        discovery = _discovery(api)
        while True:
            plan = _collect(cfg, api, dialog, discovery)
            if dialog.review(_review(plan, api)):
                break
        with dialog.suspended():
            result = _apply(cfg, api, discovery, plan)
        if result:
            dialog.notice("Setup is incomplete. Completed steps were kept; run t again to resume.")
            return result
        dialog.notice("Setup complete. Run t config onboard to discover new repositories or hosts.")
        api["_host_refresh_config"]()
        cfg = api["Config"]()
        if plan["agent"] and cfg.repos:
            action = dialog.choose("Ready to work", [("start", "Start a session"), ("done", "Finish setup")])
            if action == "start":
                with dialog.suspended():
                    return api["_onboarding_start"](cfg, plan["agent"])
        return 0
    except (Aborted, EOFError, KeyboardInterrupt):
        dialog.notice("Setup canceled. Any previously completed steps were kept.")
        return 130
    except (OSError, ValueError) as exc:
        dialog.notice(f"Setup could not finish: {exc}. Run t config onboard to retry.")
        return 1
    finally:
        dialog.close()
