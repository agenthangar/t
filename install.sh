#!/usr/bin/env bash
# Install t's links and additive agent integrations. This script never owns shell rc.
set -euo pipefail

for required in git python3; do
    if ! command -v "$required" >/dev/null 2>&1; then
        echo "t install: missing required tool: $required" >&2
        exit 1
    fi
done

T_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

resolve_primary() {
    local common
    common="$(git -C "$T_SCRIPT_DIR" rev-parse --git-common-dir 2>/dev/null)" || return 1
    case "$common" in /*) ;; *) common="$T_SCRIPT_DIR/$common" ;; esac
    (cd "$common/.." && pwd -P)
}

T_PRIMARY="$(resolve_primary || true)"
if [[ -z "$T_PRIMARY" || ! -d "$T_PRIMARY/.git" ]]; then
    echo "t install: expected a normal canonical Git checkout with a .git directory" >&2
    exit 1
fi

valid_source() {
    local root="$1" asset
    [[ -f "$root/.t-install-version" ]] || return 1
    [[ "$(cat "$root/.t-install-version")" == 1 ]] || return 1
    for asset in install.sh t.plugin.zsh local.zsh.example \
        claude/settings.json.example bin/t bin/claude-stamp-tmux bin/cursor-beam \
        claude/commands/tpush.md claude/commands/tpop.md \
        codex/prompts/tpush.md codex/prompts/tpop.md; do
        [[ -f "$root/$asset" ]] || return 1
    done
    [[ -x "$root/install.sh" && -x "$root/bin/t" ]] || return 1
}

if [[ -n "${T_LINK_DEV:-}" ]]; then
    T_LINK_SRC="$T_SCRIPT_DIR"
else
    T_LINK_SRC="$T_PRIMARY"
fi
if ! valid_source "$T_LINK_SRC"; then
    echo "t install: $T_LINK_SRC lacks the standalone t installer capability (version 1)" >&2
    echo "  merge or check out a tested t revision, then rerun install.sh" >&2
    exit 1
fi

link() {
    local src="$1" dst="$2"
    [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]] && return 0
    mkdir -p "$(dirname "$dst")"
    if [[ -L "$dst" ]]; then
        rm "$dst"
    elif [[ -e "$dst" ]]; then
        local backup="$dst.bak" n=1
        while [[ -e "$backup" || -L "$backup" ]]; do
            backup="$dst.bak.$n"
            n=$((n + 1))
        done
        mv "$dst" "$backup"
        echo "Backed up $dst -> $backup"
    fi
    ln -s "$src" "$dst"
    echo "Linked $dst -> $src"
}

link_all() {
    link "$T_LINK_SRC/bin/t" "$HOME/bin/t"
    link "$T_LINK_SRC/bin/claude-stamp-tmux" "$HOME/bin/claude-stamp-tmux"
    link "$T_LINK_SRC/bin/cursor-beam" "$HOME/bin/cursor-beam"
    link "$T_LINK_SRC/claude/commands/tpush.md" "$HOME/.claude/commands/tpush.md"
    link "$T_LINK_SRC/claude/commands/tpop.md" "$HOME/.claude/commands/tpop.md"
    link "$T_LINK_SRC/codex/prompts/tpush.md" "$HOME/.codex/prompts/tpush.md"
    link "$T_LINK_SRC/codex/prompts/tpop.md" "$HOME/.codex/prompts/tpop.md"
}
link_all

# Remove only the retired status line that the old dotfiles installer wrote.
retire_statusline() {
    [[ -f "$HOME/.claude/settings.json" ]] || return 0
    python3 - "$HOME/.claude/settings.json" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
except (OSError, ValueError):
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
line = data.get("statusLine")
if not isinstance(line, dict) or line.get("type") != "command":
    sys.exit(0)
command = line.get("command", "")
if command not in ("$HOME/bin/t todo --statusline", os.path.expanduser("~/bin/t todo --statusline")):
    sys.exit(0)
del data["statusLine"]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as file:
    json.dump(data, file, indent=2)
    file.write("\n")
os.replace(tmp, path)
print("Removed retired t todo status line from " + path)
PY
}

seed_claude_settings() {
    local dst="$HOME/.claude/settings.json"
    [[ -e "$dst" ]] && return 0
    if [[ -L "$dst" ]]; then
        rm "$dst"
    fi
    mkdir -p "$(dirname "$dst")"
    cp "$T_LINK_SRC/claude/settings.json.example" "$dst"
    echo "Created $dst"
}

merge_claude_settings() {
    [[ -f "$HOME/.claude/settings.json" ]] || return 0
    python3 - "$HOME/.claude/settings.json" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
except (OSError, ValueError):
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
changed = False
hooks = data.setdefault("hooks", {})
if isinstance(hooks, dict):
    groups = hooks.setdefault("SessionStart", [])
    if isinstance(groups, list) and not any(
        isinstance(h, dict) and "claude-stamp-tmux" in str(h.get("command", ""))
        for g in groups if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        for h in g["hooks"]
    ):
        groups.append({"hooks": [{"type": "command", "command": "$HOME/bin/claude-stamp-tmux"}]})
        changed = True
if not os.environ.get("T_NO_MCP"):
    permissions = data.setdefault("permissions", {})
    if isinstance(permissions, dict):
        allow = permissions.setdefault("allow", [])
        if isinstance(allow, list) and not any(
            isinstance(rule, str) and (rule == "mcp__sessions" or rule.startswith("mcp__sessions__"))
            for rule in allow
        ):
            allow.append("mcp__sessions")
            changed = True
if changed:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    os.replace(tmp, path)
    print("Updated t hook/MCP settings in " + path)
PY
}

install_codex_hooks() {
    [[ -z "${T_NO_CODEX_HOOKS:-}" ]] || return 0
    command -v codex >/dev/null 2>&1 || [[ -f "$HOME/.codex/config.toml" || -f "$HOME/.codex/auth.json" ]] || return 0
    # Codex executes hook commands through a shell; keep $HOME literal for portability.
    # shellcheck disable=SC2016
    python3 - "$HOME/.codex/hooks.json" '$HOME/bin/claude-stamp-tmux --agent codex' <<'PY'
import json, os, sys
path, command = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
except OSError:
    data = {}
except ValueError:
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)
hooks = data.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.exit(0)
groups = hooks.setdefault("SessionStart", [])
if not isinstance(groups, list):
    sys.exit(0)
for group in groups:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        continue
    if any(isinstance(hook, dict) and "claude-stamp-tmux" in str(hook.get("command", "")) for hook in group["hooks"]):
        sys.exit(0)
groups.append({"hooks": [{"type": "command", "command": command}]})
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as file:
    json.dump(data, file, indent=2)
    file.write("\n")
os.replace(tmp, path)
print("Registered the SessionStart hook in " + path)
PY
}

install_claude_mcp() {
    [[ -z "${T_NO_MCP:-}" ]] || return 0
    command -v claude >/dev/null 2>&1 || return 0
    python3 - "$HOME/.claude.json" <<'PY' && return 0
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as file:
        data = json.load(file)
except OSError:
    sys.exit(1)
except ValueError:
    sys.exit(0)
servers = data.get("mcpServers") if isinstance(data, dict) else None
sys.exit(0 if isinstance(servers, dict) and "sessions" in servers else 1)
PY
    if command claude mcp add sessions -s user -- "$HOME/bin/t" mcp >/dev/null 2>&1; then
        echo "Registered Claude MCP server 'sessions'"
    else
        echo "t install: Claude MCP registration failed; run: claude mcp add sessions -s user -- $HOME/bin/t mcp" >&2
    fi
}

retire_statusline
install_codex_hooks
install_claude_mcp
merge_claude_settings

# Policy is deliberately absent by default. The dotfiles adapter opts in with a
# directory of user-owned rules, and independent users may do the same explicitly.
if [[ -n "${T_PERMISSIONS_DIR:-}" && -z "${T_NO_PERMISSIONS:-}" ]]; then
    if [[ ! -f "$T_PERMISSIONS_DIR/permissions.allow" ]]; then
        echo "t install: T_PERMISSIONS_DIR must contain permissions.allow" >&2
        exit 1
    fi
    python3 "$T_LINK_SRC/bin/t" permissions --apply
fi
if [[ "${T_AUTO_TRUST:-}" == 1 && -z "${T_NO_TRUST:-}" ]]; then
    python3 "$T_LINK_SRC/bin/t" trust --all -q
fi

if [[ -n "${T_LINKS_ONLY:-}" ]]; then
    exit 0
fi

seed_claude_settings
merge_claude_settings

T_LOCAL_RC="${T_LOCAL_RC:-${XDG_CONFIG_HOME:-$HOME/.config}/t/local.zsh}"
if [[ ! -e "$T_LOCAL_RC" ]]; then
    mkdir -p "$(dirname "$T_LOCAL_RC")"
    cp "$T_LINK_SRC/local.zsh.example" "$T_LOCAL_RC"
    echo "Created $T_LOCAL_RC"
fi

printf '\nAdd this to ~/.zshrc (after your PATH setup):\n'
cat <<'SNIPPET'
export PATH="$HOME/bin:$PATH"
if [[ -L "$HOME/bin/t" ]]; then
  _t_source="$(dirname "$(dirname "$(readlink "$HOME/bin/t")")")/t.plugin.zsh"
  [[ -f "$_t_source" ]] && source "$_t_source"
  unset _t_source
fi
SNIPPET
printf '\nRun t setup in a new zsh shell.\n'
for tool in git python3 zsh tmux; do
    command -v "$tool" >/dev/null 2>&1 || echo "Missing dependency: $tool" >&2
done
