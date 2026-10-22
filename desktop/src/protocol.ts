export type Session = {
  slot: string;
  repo?: string;
  cwd?: string;
  summary?: string;
  title?: string;
  state?: string;
  agent?: string;
  model?: string;
  branch?: string;
  issue?: string;
  pr?: string;
  host?: string;
  id?: string;
  [key: string]: unknown;
};
export type Capability = {
  method: string;
  command: string[];
  label?: string;
  coreMethod?: string;
  kind?: string;
  description?: string;
  summary?: string;
  mutating?: boolean;
  arguments?: { name: string; required?: boolean; help?: string }[];
};
export type Snapshot = {
  sessions: Session[];
  repositories: { alias: string; path: string }[];
  hosts: { alias: string; target: string; default?: boolean }[];
  config: Record<string, unknown>;
  capabilities: Capability[];
  remoteStatus?: { host: string; status: string }[];
  diagnostics?: Record<string, unknown>;
};
export type Operation = {
  operationId: string;
  command: string[] | null;
  summary: string;
  state: string;
  method?: string;
  arguments?: Record<string, unknown>;
  attachment?: { token: string; kind: "terminal" };
  result?: unknown;
};
export const emptySnapshot: Snapshot = {
  sessions: [],
  repositories: [],
  hosts: [],
  config: {},
  capabilities: [],
};
export function title(s: Session) {
  return String(
    s.summary || s.title || `Session ${s.slot || s.id || "unknown"}`,
  );
}
export function key(s: Session) {
  return `${s.host || "local"}:${s.repo || s.cwd}:${s.slot || s.id}`;
}
export function parseArgs(text: string): string[] {
  const words: string[] = [];
  let word = "";
  let quote = "";
  let escape = false;
  let started = false;
  for (const c of text) {
    if (escape) {
      word += c;
      escape = false;
      started = true;
      continue;
    }
    if (c === "\\" && quote !== "'") {
      escape = true;
      started = true;
      continue;
    }
    if (quote) {
      if (c === quote) quote = "";
      else word += c;
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
      started = true;
      continue;
    }
    if (/\s/.test(c)) {
      if (started) {
        words.push(word);
        word = "";
        started = false;
      }
    } else {
      word += c;
      started = true;
    }
  }
  if (quote || escape)
    throw new Error("Finish the quoted or escaped argument before reviewing.");
  if (started) words.push(word);
  return words;
}

export const mcpCapabilities: Capability[] = [
  {
    label: "MCP session tools",
    method: "mcp.call",
    coreMethod: "mcp.call",
    command: ["mcp", "--call"],
    kind: "core",
    description: "Read session tools through the shared MCP service.",
  },
  {
    label: "Install MCP integration",
    method: "mcp.install",
    command: ["mcp", "--install"],
    kind: "terminal",
    description: "Configure the sessions MCP integration for your agent.",
  },
  {
    label: "List MCP tools",
    method: "mcp.listTools",
    command: ["mcp", "--list-tools"],
    kind: "terminal",
    description: "List tools exposed by the sessions MCP server.",
  },
  {
    label: "Rebuild session history index",
    method: "mcp.reindex",
    command: ["mcp", "--reindex"],
    kind: "terminal",
    description: "Rebuild the shared transcript search index.",
  },
];
