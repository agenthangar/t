import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const golden = JSON.parse(
  readFileSync(
    new URL("./fixtures/core-protocol.json", import.meta.url),
    "utf8",
  ),
);
// TEST ONLY transport: never included in the production renderer bundle.
async function setup(
  page: import("@playwright/test").Page,
  scenario: {
    applyFailure?: boolean;
    register?: boolean;
    terminalFailure?: boolean;
    conflictingDetail?: boolean;
    remoteDetail?: boolean;
    terminalReady?: boolean;
    issueResults?: "mixed" | "unavailable";
  } = {},
) {
  await page.addInitScript(
    ({ golden, scenario }) => {
      const calls: { method: string; params: unknown }[] = [];
      (window as any).fixtureCalls = calls;
      (window as any).fixtureIssueResults = scenario.issueResults;
      let appliedMethod = "sessions.open";
      let sequence = 0;
      let terminalAttempts = 0;
      const tokenize = (value: any): any => {
        if (!value || typeof value !== "object") return value;
        if (value.kind === "terminal" && value.argv)
          return { kind: "terminal", token: `fixture-${++sequence}` };
        if (Array.isArray(value)) return value.map(tokenize);
        return Object.fromEntries(
          Object.entries(value).map(([k, v]) => [k, tokenize(v)]),
        );
      };
      (window as any).__TAURI_INTERNALS__ = {
        transformCallback: () => 1,
        invoke: async (command: string, args: any) => {
          if (command === "helper_request") {
            calls.push({ method: args.method, params: args.params });
            if (args.method === "workspace.snapshot")
              return {
                ...golden.snapshot,
                repositories: [
                  { alias: "fixture", path: "/tmp/t-desktop-fixture" },
                  ...(scenario.register
                    ? [{ alias: "other", path: "/tmp/t-desktop-other" }]
                    : []),
                ],
                hosts: [{ alias: "build", target: "fixture@example.test" }],
                sessions: [
                  {
                    repo: "fixture",
                    slot: "fixture-1",
                    sid: "22222222-2222-4222-8222-222222222222",
                    summary: "Fixture · Build a shared workspace",
                    title: "Fixture · Build a shared workspace",
                    agent: "codex",
                    model: "gpt-test",
                    state: "attached",
                    context: "active",
                    branch: "dev/fixture-1",
                    host: "local",
                    cwd: "/tmp/t-desktop-fixture",
                  },
                  ...(scenario.register
                    ? [
                        {
                          repo: "other",
                          slot: "2",
                          summary: "Review database migration",
                          agent: "claude",
                          state: "detached",
                          context: "idle",
                          host: scenario.remoteDetail ? "build" : "local",
                          sid: scenario.remoteDetail
                            ? "remote-sid"
                            : "database-sid",
                          cwd: "/tmp/t-desktop-other",
                        },
                        {
                          repo: "fixture",
                          slot: "3",
                          summary: "Check unavailable build",
                          agent: "codex",
                          state: "attached",
                          context: "active",
                          host: "build",
                          cwd: "/tmp/t-desktop-build",
                        },
                        {
                          repo: "fixture",
                          slot: "4",
                          summary: "External app review",
                          agent: "codex",
                          state: "app",
                          context: "active",
                          host: "local",
                          cwd: "/tmp/t-desktop-app",
                        },
                      ]
                    : []),
                ],
                ...(scenario.register
                  ? {
                      remoteStatus: scenario.remoteDetail
                        ? []
                        : [{ host: "build", status: "incomplete" }],
                    }
                  : {}),
              };
            if (args.method === "sessions.detail" && scenario.conflictingDetail)
              return {
                repo: "wrong",
                slot: "99",
                host: "remote-wrong",
                sid: "wrong-sid",
                cwd: "/wrong",
                state: "dead",
                context: "saved",
                agent: "wrong-agent",
                branch: "enriched-branch",
              };
            if (args.method === "sessions.search") return golden.search;
            if (args.method === "issues.list") {
              return golden[
                (window as any).fixtureIssueResults === "unavailable"
                  ? "issues.list.unavailable"
                  : "issues.list.partial"
              ];
            }
            if (
              (args.method.startsWith("sessions.") &&
                args.method !== "sessions.attach") ||
              args.method === "issues.start"
            ) {
              appliedMethod = args.method;
              return golden[args.method as keyof typeof golden]?.prepared;
            }
            if (args.method === "operations.apply") {
              if (scenario.applyFailure)
                throw new Error(
                  "Fixture helper disconnected; outcome unverified",
                );
              if (scenario.terminalReady)
                return tokenize({ ...golden.terminal, state: "ready" });
              return tokenize(
                golden[appliedMethod as keyof typeof golden]?.applied,
              );
            }
            if (args.method === "sessions.attach")
              return {
                attachment: {
                  kind: "terminal",
                  token: `fixture-${++sequence}`,
                },
              };
            if (args.method === "operations.prepare")
              return { ...golden.terminal, command: args.params.command };
            if (args.method === "initialize") return golden.initialize;
            if (args.method === "system.diagnostics")
              return { dependencies: [{ name: "tmux", available: true }] };
            return { state: "completed" };
          }
          if (command === "pty_open") {
            if (scenario.terminalFailure && terminalAttempts++ === 0)
              throw new Error("Fixture attachment expired");
            setTimeout(
              () =>
                args.onEvent.onmessage({
                  kind: "data",
                  data: btoa("Fixture terminal - no live agent\r\n$ "),
                }),
              20,
            );
            return args.token;
          }
          return undefined;
        },
      };
    },
    { golden, scenario },
  );
  await page.goto("/");
  await expect(
    page.getByText("Fixture · Build a shared workspace", { exact: true }),
  ).toBeVisible();
}
test("typed new session review and apply use the real nullable-command protocol", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  await expect(page.getByText("Review before running")).toBeVisible();
  await page.getByRole("button", { name: "Run command", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(calls.find((c: any) => c.method === "sessions.open").params).toEqual({
    repo: "fixture",
    slot: "",
    new: true,
  });
  expect(calls.some((c: any) => c.method === "operations.prepare")).toBe(false);
});
test("attach and detach preserve session visibility; keyboard command palette works", async ({
  page,
}) => {
  await setup(page);
  await page
    .getByText("Fixture · Build a shared workspace", { exact: true })
    .dblclick();
  await expect(
    page.getByRole("button", {
      name: "Detach Fixture · Build a shared workspace",
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Detach Fixture · Build a shared workspace" })
    .click();
  await expect(
    page
      .getByText("Fixture · Build a shared workspace", { exact: true })
      .first(),
  ).toBeVisible();
  await page.keyboard.press("Meta+k");
  await expect(
    page.getByRole("dialog", { name: "Command palette" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
test("visual QA desktop, narrow, dark and command review", async ({ page }) => {
  await setup(page);
  await page.setViewportSize({ width: 1320, height: 860 });
  await page
    .getByText("Fixture · Build a shared workspace", { exact: true })
    .click();
  await page.screenshot({
    path: "test-results/workspace-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 880, height: 680 });
  await page.screenshot({
    path: "test-results/workspace-narrow.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Settings & diagnostics" }).click();
  await page.getByLabel("Appearance").selectOption("dark");
  await page.getByRole("button", { name: "Close settings" }).click();
  await page.setViewportSize({ width: 1320, height: 860 });
  await page.screenshot({
    path: "test-results/workspace-dark.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  await page.screenshot({
    path: "test-results/workspace-review.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /operations? to reconcile/ }),
  ).toHaveCount(0);
  await page
    .getByText("Fixture · Build a shared workspace", { exact: true })
    .first()
    .dblclick();
  await page.waitForTimeout(100);
  await page.screenshot({
    path: "test-results/workspace-attached.png",
    fullPage: true,
  });
  expect(
    await page
      .locator("body")
      .evaluate((el) => el.scrollWidth <= window.innerWidth),
  ).toBe(true);
});

test("existing resume carries selected SID and unslotted search routes to explicit terminal selection", async ({
  page,
}) => {
  await setup(page);
  await page
    .getByText("Fixture · Build a shared workspace", { exact: true })
    .click();
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  let calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.find((c: any) => c.method === "sessions.resume").params.sid,
  ).toBe("22222222-2222-4222-8222-222222222222");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /operations? to reconcile/ }),
  ).toHaveCount(0);
  await page.keyboard.press("Meta+k");
  await page
    .getByPlaceholder("What would you like to do?")
    .fill("session search");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.getByLabel("Search session history").fill("workspace");
  await page
    .getByRole("button", { name: "Read current state", exact: true })
    .click();
  await expect(
    page.getByText("Fixture shared workspace search", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Resume this conversation" }).click();
  await expect(page.getByText(/no resolvable numbered slot/)).toBeVisible();
  await expect(page.getByLabel("Command arguments")).toHaveValue(
    "--all --days all",
  );
  calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.find((c: any) => c.method === "sessions.search").params.query,
  ).toBe("workspace");
});
test("multi-issue apply opens independent tabs and interactive review remains available", async ({
  page,
}) => {
  await setup(page);
  await page.keyboard.press("Meta+k");
  await page.getByPlaceholder("What would you like to do?").fill("issue start");
  await page.keyboard.press("Enter");
  await page.getByLabel("Issue numbers or URLs").fill("12, 34");
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  await page.getByRole("button", { name: "Run command", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Detach Issue session" }),
  ).toHaveCount(2);
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.find((c: any) => c.method === "issues.start").params.targets,
  ).toEqual(["fixture", "12", "34"]);
});

test("failed submitted operation survives Cancel and Escape without a replay button", async ({
  page,
}) => {
  await setup(page, { applyFailure: true });
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  await page.getByRole("button", { name: "Run command", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Inspect outcome", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Run command", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "1 operation to reconcile" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "1 operation to reconcile" }),
  ).toBeVisible();
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.filter((call: any) => call.method === "operations.apply"),
  ).toHaveLength(1);
});
test("ready terminal operation retains durable recovery after Escape", async ({
  page,
}) => {
  await setup(page, { terminalReady: true });
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  await page.getByRole("button", { name: "Run command", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "1 operation to reconcile" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "1 operation to reconcile" }),
  ).toBeVisible();
});

for (const scenario of ["mixed", "unavailable"] as const) {
  test(`issue results surface ${scenario} repository failures without a false empty result`, async ({
    page,
  }) => {
    await setup(page, { issueResults: scenario });
    await page.keyboard.press("Meta+k");
    await page
      .getByPlaceholder("What would you like to do?")
      .fill("issue list");
    await page.keyboard.press("Enter");
    await page
      .getByRole("button", { name: "Read current state", exact: true })
      .click();
    await expect(
      page.getByText("Issue results are incomplete.", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "fixture/unavailable: repository issues could not be read",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      page.getByText("No issues found.", { exact: true }),
    ).toHaveCount(0);
    if (scenario === "mixed") {
      await expect(
        page.getByText("#12 Synthetic workspace task", { exact: true }),
      ).toBeVisible();
      await page.getByRole("checkbox").check();
      await expect(
        page.getByRole("button", { name: "Start 1 selected issue" }),
      ).toBeVisible();
      await expect(
        page.getByText("No issues could be verified.", { exact: true }),
      ).toHaveCount(0);
      await page.evaluate(() => {
        (window as any).fixtureIssueResults = "unavailable";
      });
      await page
        .getByRole("button", { name: "Read current state", exact: true })
        .click();
      await expect(
        page.getByText("No issues could be verified.", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Start 1 selected issue" }),
      ).toHaveCount(0);
      await expect(page.getByRole("checkbox")).toHaveCount(0);
    } else {
      await expect(
        page.getByText("No issues could be verified.", { exact: true }),
      ).toBeVisible();
      await expect(page.getByRole("checkbox")).toHaveCount(0);
    }
  });
}

test("session register filters real runtime state and keeps external/unverified attachment unavailable", async ({
  page,
}) => {
  await setup(page, { register: true });
  for (const width of [1320, 1024, 736, 320]) {
    await page.setViewportSize({ width, height: 860 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme;
      }, theme);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await expect(page.locator(".workspace")).toHaveCount(1);
      await expect(page.locator("header.toolbar")).toHaveCount(1);
      await expect(page.locator("main > footer")).toHaveCount(1);
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      await page.screenshot({
        path: `test-results/session-register-${width}-${theme}.png`,
        fullPage: width < 520,
        animations: "disabled",
      });
    }
  }
  await page.setViewportSize({ width: 1320, height: 860 });
  await expect(page.locator(".terminal-area")).toHaveCount(0);
  await expect(page.locator(".session-row")).toHaveCount(4);
  await expect(
    page.getByRole("button", { name: /Needs attention/ }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Working/ })).toHaveCount(0);
  await page.getByRole("button", { name: /Tmux detached/ }).click();
  await expect(page.locator(".session-row")).toHaveCount(1);
  await expect(page.locator(".session-row")).toContainText(
    "Review database migration",
  );
  await page
    .getByRole("button", { name: /All sessions/ })
    .last()
    .click();
  await expect(
    page.getByRole("button", {
      name: "Open terminal for Check unavailable build",
    }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Open terminal for External app review" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "By repository" }).click();
  await expect(page.locator(".repo-group")).toHaveCount(2);
  await page.getByLabel("Filter sessions").fill("build");
  await expect(page.locator(".session-row")).toHaveCount(2);
});
test("inspector lifecycle target follows selected row while another terminal is attached", async ({
  page,
}) => {
  await setup(page, { register: true });
  await page
    .getByRole("button", {
      name: "Open terminal for Fixture · Build a shared workspace",
    })
    .click();
  await page
    .getByRole("button", { name: "Review database migration", exact: true })
    .click();
  await expect(page.locator(".inspector h2")).toHaveText(
    "Review database migration",
  );
  await page
    .locator(".detail-actions")
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.find((c: any) => c.method === "sessions.close").params,
  ).toMatchObject({ repo: "other", slot: "2" });
});
test("new session uses current repository scope instead of an earlier selected row", async ({
  page,
}) => {
  await setup(page, { register: true });
  await page
    .getByRole("button", {
      name: "Fixture · Build a shared workspace",
      exact: true,
    })
    .click();
  await page
    .locator(".sidebar")
    .getByRole("button", { name: "other 1", exact: true })
    .click();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.find((c: any) => c.method === "sessions.open").params,
  ).toMatchObject({ repo: "other", new: true });
});
test("failed attachment can reconnect with a fresh token", async ({ page }) => {
  await setup(page, { terminalFailure: true });
  await page
    .getByRole("button", {
      name: "Open terminal for Fixture · Build a shared workspace",
    })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Fixture attachment expired",
  );
  await page.getByRole("button", { name: "Reconnect", exact: true }).click();
  await expect(page.locator(".terminal-tab")).toHaveCount(2);
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(calls.filter((c: any) => c.method === "sessions.attach")).toHaveLength(
    2,
  );
});

test("conflicting transcript detail cannot retarget a selected live session", async ({
  page,
}) => {
  await setup(page, { register: true, conflictingDetail: true });
  await page
    .getByRole("button", { name: "Review database migration", exact: true })
    .click();
  await expect(page.locator(".inspector")).toContainText("enriched-branch");
  await expect(page.locator(".inspector")).not.toContainText("wrong-agent");
  await page
    .locator(".detail-actions")
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(
    calls.find((c: any) => c.method === "sessions.close").params,
  ).toMatchObject({ repo: "other", slot: "2" });
  expect(
    calls.find((c: any) => c.method === "sessions.close").params.host,
  ).not.toBe("remote-wrong");
});

test("remote selection skips local transcript enrichment and retains remote lifecycle target", async ({
  page,
}) => {
  await setup(page, {
    register: true,
    remoteDetail: true,
    conflictingDetail: true,
  });
  await page
    .getByRole("button", { name: "Review database migration", exact: true })
    .click();
  await expect(page.locator(".inspector h2")).toHaveText(
    "Review database migration",
  );
  await page
    .locator(".detail-actions")
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Review command", exact: true })
    .click();
  const calls = await page.evaluate(() => (window as any).fixtureCalls);
  expect(calls.filter((c: any) => c.method === "sessions.detail")).toHaveLength(
    0,
  );
  expect(
    calls.find((c: any) => c.method === "sessions.close").params,
  ).toMatchObject({ repo: "other", slot: "2", host: "build" });
});
