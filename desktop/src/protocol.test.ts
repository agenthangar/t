import { describe, it, expect } from "vitest";
import { parseArgs, mcpCapabilities } from "./protocol";
import { coreParams, target } from "./CoreForm";
import golden from "../tests/fixtures/core-protocol.json";
describe("actual shared-core protocol goldens", () => {
  it("exposes every allowed MCP maintenance route from actual core preparations", () => {
    expect(
      mcpCapabilities
        .filter((c) => c.kind === "terminal")
        .map((c) => c.command),
    ).toEqual([
      golden["mcp.install"].prepared.command,
      golden["mcp.listTools"].command,
      golden["mcp.reindex"].command,
    ]);
  });
  it("prepares the actual narrow MCP installation syntax without executing it", () => {
    expect(golden["mcp.install"].prepared.command).toEqual([
      "mcp",
      "--install",
    ]);
    expect(golden["mcp.install"].applied.state).toBe("ready");
    expect(golden["mcp.install"].applied.method).toBe("terminal");
  });
  it("typed prepared operations have nullable command and retain human parameters", () => {
    expect(golden["sessions.open"].prepared.command).toBeNull();
    expect(golden["sessions.open"].prepared.state).toBe("prepared");
    expect(
      coreParams("sessions.open", { repo: "fixture", new: "true", agent: "" }),
    ).toMatchObject(golden["sessions.open"].prepared.arguments);
    expect(
      coreParams("sessions.open", { repo: "fixture", new: "true", agent: "" }),
    ).not.toHaveProperty("agent");
  });
  it("supports typed lifecycle and multiple independently attached issues", () => {
    for (const method of [
      "sessions.resume",
      "sessions.restart",
      "sessions.close",
    ] as const)
      expect(golden[method].applied.state).toBe("completed");
    expect(golden["issues.start"].applied.result.attachments).toHaveLength(2);
  });
  it("catalog declares actual typed methods", () => {
    expect(
      golden.initialize.capabilities.find((c) => c.method === "session.open")
        ?.coreMethod,
    ).toBe("sessions.open");
  });
  it("parses arguments without evaluating shell substitutions", () => {
    expect(parseArgs(`fixture --model 'a b' "$(touch /tmp/no)"`)).toEqual([
      "fixture",
      "--model",
      "a b",
      "$(touch /tmp/no)",
    ]);
    expect(() => parseArgs('"unfinished')).toThrow();
    expect(parseArgs('a "" b')).toEqual(["a", "", "b"]);
  });
  it("uses canonical repository rather than guessing hyphenated names", () => {
    expect(target({ repo: "a-b", slot: "a-b-16" })).toEqual({
      repo: "a-b",
      slot: "16",
    });
  });
});
