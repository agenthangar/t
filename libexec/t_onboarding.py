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


class Back(Exception):
    """Return to the previous setup choice, retaining the current selection."""

    def __init__(self, selection=None):
        self.selection = selection


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

    def choose(self, label, rows, *, multi=False, marked=(), default=0, back=False):
        if not rows:
            return [] if multi else None
        if self.ui is not None:
            selection = []
            result = self.ui.pick(label, rows, multi=multi, marked=marked,
                                  default=default, on_back=selection.append,
                                  back_label="back" if back else "cancel",
                                  primary=("enter", "CONTINUE", ""))
            if result is None:
                if back:
                    raise Back(selection[0] if selection else None)
                raise Aborted
            return result
        print("\n" + label)
        for index, (_, text) in enumerate(rows, 1):
            mark = " [x]" if multi and index - 1 in marked else ""
            print(f"  {index}. {text}{mark}")
        action = "goes back" if back else "cancels"
        while True:
            answer = input(f"Numbers separated by commas (Enter keeps marks, - selects none, q {action}, Ctrl-C cancels): "
                           if multi else f"Number (Enter keeps choice, q {action}, Ctrl-C cancels): ").strip()
            if answer.lower() in ("q", "quit", "back", "\x1b"):
                if back:
                    raise Back([rows[i][0] for i in marked] if multi else rows[default][0])
                raise Aborted
            if not multi and not answer:
                return rows[default][0]
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
                               keys="y apply · n/esc back · q quit · ctrl-c cancel",
                               primary=("y", "APPLY SETUP", ""))
        else:
            print("\nReview setup changes\n" + "\n".join("  " + line for line in lines))
            key = input("Apply these changes? [y apply / n back / q quit; Ctrl-C cancels] ").strip().lower()
        if key in ("q", "abort"):
            raise Aborted
        return key == "y"


def _discovery(api):
    return runpy.run_path(os.path.join(api["_t_source_root"](), "libexec", "t_discovery.py"))


def _organizations(dialog, discovery, cache=None, choose=None):
    cache = {} if cache is None else cache
    choose = choose or dialog.choose
    if "state" not in cache:
        cache["state"] = discovery["github_auth_state"]()
    state = cache["state"]
    if state == "missing":
        if "command" not in cache:
            cache["command"] = discovery["github_install_command"]()
        command = cache["command"]
        if command:
            choice = choose("Install GitHub CLI to discover organization repositories?", [
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
            state = cache["state"] = discovery["github_auth_state"]()
        if state == "missing":
            dialog.notice("Install GitHub CLI from https://cli.github.com, then run t config onboard.")
            return []
    if state != "authenticated":
        choice = choose("Connect GitHub to discover organization repositories?", [
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
        cache["state"] = "authenticated"
    if "orgs" not in cache:
        orgs, warnings = discovery["github_orgs"]()
        for warning in warnings:
            dialog.notice(warning)
        cache["orgs"] = orgs
    return cache["orgs"]


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


def _collect(cfg, api, dialog, discovery, state=None, *, back=False):
    """Visit applicable choices in order; Back retains edits and skips empty steps."""
    state = {} if state is None else state
    choices = state.setdefault("choices", {})
    cache = state.setdefault("cache", {})
    if "probed" not in cache:
        cache["probed"] = api["_install_probe"]()
    probed = cache["probed"]
    agents_label = "Choose agents to use (missing tools will be installed)"
    default_label = "Default agent for new sessions"
    local_label = "Register existing checkouts"
    org_label = "Organizations to browse (optional)"
    clone_label = "Clone repositories (none selected by default)"
    host_label = "Optional Tailscale hosts (SSH access is not verified)"
    index, direction = (state.get("last", 0), -1) if back else (0, 1)
    shown = False

    def choose(label, rows, *, multi=False, marked=()):
        nonlocal shown
        keys = [key for key, _ in rows]
        initial = [keys[i] for i in marked] if multi else next(iter(keys), None)
        previous = choices.get(label, initial)
        previous = ([key for key in previous if key in keys] if multi else
                    previous if previous in keys else initial)
        choices[label] = previous
        if not rows:
            return previous
        shown = True
        try:
            result = dialog.choose(label, rows, multi=multi, back=True,
                                   marked=[i for i, key in enumerate(keys) if key in previous] if multi else (),
                                   default=0 if multi else keys.index(previous))
        except Back as exc:
            if exc.selection is not None:
                choices[label] = exc.selection
            raise
        choices[label] = result
        return result

    while 0 <= index < 7:
        shown = False
        try:
            if index == 0:
                agents = list(api["INSTALL_AGENTS"])
                choose(agents_label, [
                    (agent, f"{agent} · " + ("installed" if probed[agent].get("version") else "install") +
                     (" · chats only" if agent not in api["AGENTS"] else ""))
                    for agent in agents], multi=True,
                    marked=[i for i, agent in enumerate(agents) if probed[agent].get("version")])
            elif index == 1:
                launchable = [agent for agent in choices[agents_label] if agent in api["AGENTS"]]
                if len(launchable) > 1:
                    choose(default_label, [(agent, agent) for agent in launchable])
                else:
                    choices[default_label] = next(iter(launchable), None)
            elif index == 2:
                if "local" not in cache:
                    home = Path(api["HOME"])
                    current = api["_run"](["git", "rev-parse", "--show-toplevel"], timeout=5)
                    cwd = current.stdout.strip() if current.returncode == 0 else os.getcwd()
                    roots = list(dict.fromkeys([str(home / "code"), str(home / "config"), cwd,
                                               *cfg.repos.values()]))
                    cache["local"] = discovery["discover_local"](roots, [cfg.worktree_root])
                    registered = {os.path.realpath(path) for path in cfg.repos.values()}
                    cache["candidates"] = [repo for repo in cache["local"]
                                           if os.path.realpath(repo["path"]) not in registered]
                candidates = cache["candidates"]
                choose(local_label, [(repo["path"], repo["path"]) for repo in candidates],
                       multi=True, marked=range(len(candidates)))
            elif index == 3:
                cache["orgs"] = _organizations(dialog, discovery, cache.setdefault("github", {}), choose)
            elif index == 4:
                choose(org_label, [(org, org) for org in cache["orgs"]], multi=True)
            elif index == 5:
                remote = []
                org_repos = cache.setdefault("org_repos", {})
                for org in choices[org_label]:
                    if org not in org_repos:
                        found, warnings = discovery["github_repos"](org)
                        for warning in warnings:
                            dialog.notice(warning)
                        org_repos[org] = found
                    remote.extend(org_repos[org])
                identities = {repo["identity"]: repo["path"] for repo in cache["local"] if repo.get("identity")}
                clone_rows, candidates, seen = [], [], set()
                for repo in remote:
                    identity = repo["full_name"].lower()
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if identity in identities:
                        if not any(os.path.realpath(path) == os.path.realpath(identities[identity])
                                   for path in choices[local_label]):
                            dialog.notice(f"Already cloned: {repo['full_name']} → {identities[identity]}")
                        continue
                    destinations = cache.setdefault("destinations", {})
                    if identity not in destinations:
                        destinations[identity] = discovery["clone_destination"](repo["full_name"])
                    target = destinations[identity]
                    suffix = " · archived" if repo.get("archived") else " · fork" if repo.get("fork") else ""
                    clone_rows.append((identity, f"{repo['full_name']}{suffix} → {target}"))
                    candidates.append({**repo, "path": target})
                cache["clones"] = candidates
                choose(clone_label, clone_rows, multi=True)
            else:
                if "hosts" not in cache:
                    hosts, warnings = discovery["tailscale_hosts"]()
                    for warning in warnings:
                        dialog.notice(warning)
                    targets, aliases = set(cfg.hosts.values()), set(cfg.hosts)
                    available = []
                    for host in hosts:
                        if host["target"] in targets:
                            continue
                        alias = host["alias"]
                        if alias in aliases:
                            alias = host["target"]
                        if not api["_CONFIG_ALIAS"].fullmatch(alias) or alias in aliases:
                            base, number = host["alias"], 2
                            alias = f"{base}-{number}"
                            while alias in aliases:
                                number += 1
                                alias = f"{base}-{number}"
                        aliases.add(alias)
                        targets.add(host["target"])
                        available.append({**host, "alias": alias})
                    cache["hosts"] = available
                choose(host_label, [(host["target"], f"{host['alias']} → {host['target']}")
                                    for host in cache["hosts"]], multi=True)
        except Back:
            index -= 1
            direction = -1
            continue
        if shown:
            state["last"] = index
            direction = 1
        index += direction
    if index < 0:
        raise Back
    selected = choices[agents_label]
    repos = [repo for repo in cache["candidates"] if repo["path"] in choices[local_label]]
    clones = [repo for repo in cache["clones"] if repo["full_name"].lower() in choices[clone_label]]
    hosts = [host for host in cache["hosts"] if host["target"] in choices[host_label]]
    proposals = _repo_aliases(api, [repo["path"] for repo in repos + clones], cfg.repos)
    return {"agent": choices[default_label], "selected": selected,
            "steps": _agent_plan(api, selected, probed), "repos": repos,
            "clones": clones, "aliases": proposals, "hosts": hosts}


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
        state, discovery = {}, None
        welcome, back = True, False
        while True:
            if welcome:
                action = dialog.choose("Welcome to t", [("setup", "Set up agents and repositories"),
                                                        ("later", "Not now")])
                if action == "later":
                    dialog.notice("Run t again when ready, or use t config onboard to revisit setup.")
                    return 0
                if discovery is None:
                    discovery = _discovery(api)
                welcome = False
            try:
                plan = _collect(cfg, api, dialog, discovery, state, back=back)
            except Back:
                welcome, back = True, False
                continue
            if dialog.review(_review(plan, api)):
                break
            back = True
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
