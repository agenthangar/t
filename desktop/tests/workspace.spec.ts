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
  scenario: { applyFailure?: boolean; terminalReady?: boolean } = {},
) {
  await page.addInitScript(
    ({ golden, scenario }) => {
      const calls: { method: string; params: unknown }[] = [];
      (window as any).fixtureCalls = calls;
      let appliedMethod = "sessions.open";
      let sequence = 0;
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
                    state: "live",
                    branch: "dev/fixture-1",
                    host: "local",
                    cwd: "/tmp/t-desktop-fixture",
                  },
                ],
              };
            if (args.method === "sessions.search") return golden.search;
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
