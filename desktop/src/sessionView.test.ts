import { describe, expect, it } from "vitest";
import { sessionStatus, enrichSession } from "./sessionView";
describe("session register uses reported state", () => {
  it("keeps runtime attachment independent of agent activity", () => {
    expect(sessionStatus({ slot: "1", state: "attached" })).toMatchObject({
      label: "Tmux attached",
      group: "attached",
    });
    expect(sessionStatus({ slot: "1", state: "detached" })).toMatchObject({
      label: "Tmux detached",
      group: "detached",
    });
    expect(sessionStatus({ slot: "1", state: "app" }).label).toBe(
      "External app",
    );
    expect(sessionStatus({ slot: "1", state: "foreground" }).group).toBe(
      "unknown",
    );
    expect(sessionStatus({ slot: "1" }).label).toBe("Unknown");
  });
  it("uses activity filters only for explicit activity, and uncertainty wins", () => {
    expect(sessionStatus({ slot: "1", state: "needs_input" }).group).toBe(
      "attention",
    );
    expect(sessionStatus({ slot: "1", state: "working" }).group).toBe(
      "working",
    );
    expect(sessionStatus({ slot: "1", state: "idle" }).group).toBe("idle");
    expect(
      sessionStatus({ slot: "1", state: "working", host: "build" }, [
        { host: "build", status: "incomplete" },
      ]),
    ).toMatchObject({ label: "Unverified", group: "unknown" });
  });
});

it("transcript enrichment cannot overwrite live target identity, state, or context", () => {
  const live = {
    slot: "2",
    repo: "api",
    host: "build",
    sid: "remote-sid",
    cwd: "/remote/api",
    state: "attached",
    context: "active",
    agent: "codex",
  };
  expect(
    enrichSession(live, {
      slot: "99",
      repo: "other",
      host: "local",
      sid: "local-sid",
      cwd: "/local",
      state: "dead",
      context: "saved",
      agent: "claude",
      branch: "dev/api-2",
    }),
  ).toEqual({ ...live, branch: "dev/api-2" });
});
