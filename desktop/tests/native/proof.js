(async () => {
  if (window.__nativeProofStarted) return;
  window.__nativeProofStarted = true;
  const invoke = window.__TAURI_INTERNALS__.invoke;
  const core = (method, params = {}) => invoke("helper_request", { method, params });
  const wait = async (predicate, label) => {
    for (let attempt = 0; attempt < 300; attempt++) {
      if (await predicate()) return;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error(`Timed out: ${label}`);
  };
  const assert = (value, label) => { if (!value) throw new Error(label); };
  try {
    await wait(() => document.querySelector('[aria-label="Refresh workspace"]'), "React renders");
    const snapshot = await core("workspace.snapshot");
    assert(snapshot.repositories.some((repo) => repo.alias === "repo"), "real isolated repository discovered");
    assert(snapshot.sessions.length === 0, "no unrelated sessions discovered");
    const prepared = await core("sessions.open", { repo: "repo", slot: 1, agent: "claude" });
    assert(prepared.state === "prepared" && prepared.command === null, "typed review");
    const applied = await core("operations.apply", { operationId: prepared.operationId });
    assert(applied.state === "completed" && applied.attachment.token, "real session launched with opaque attachment");
    assert(!applied.attachment.argv, "raw command remains outside WebView");
    document.querySelector('[aria-label="Refresh workspace"]').click();
    await wait(() => [...document.querySelectorAll(".session-row")].some((row) => row.textContent.includes("repo")), "real session appears in rendered workspace");
    let terminalID;
    let pendingAcks = 0;
    let output = "";
    const callback = window.__TAURI_INTERNALS__.transformCallback((frame) => {
      const event = frame.message;
      if (event?.kind === "data") {
        output += atob(event.data);
        if (terminalID) invoke("pty_ack", { id: terminalID }).catch(() => {});
        else pendingAcks++;
      }
    });
    terminalID = await invoke("pty_open", { token: applied.attachment.token, cols: 80, rows: 24, onEvent: `__CHANNEL__:${callback}` });
    while (pendingAcks-- > 0) await invoke("pty_ack", { id: terminalID });
    await invoke("pty_resize", { id: terminalID, cols: 100, rows: 30 });
    await invoke("pty_write", { id: terminalID, data: "NATIVE-PROOF-INPUT\r" });
    await wait(() => output.includes("NATIVE-PROOF-INPUT"), "real PTY input/output");
    await invoke("pty_close", { id: terminalID });
    const afterDetach = await core("sessions.list");
    assert(afterDetach.sessions.some((session) => session.slot === "repo-1" || session.slot === "1"), "detach preserves real agent session");
    let rejected = false;
    try { await invoke("pty_open", { token: applied.attachment.token, cols: 80, rows: 24, onEvent: `__CHANNEL__:${callback}` }); }
    catch { rejected = true; }
    assert(rejected, "attachment token is one-use");
    window.__TAURI_INTERNALS__.unregisterCallback(callback);
    await invoke("native_proof_report", { result: { ok: true, checks: ["native WebView renders", "real IPC/helper snapshot", "typed prepare/apply", "session row refresh", "opaque one-use token", "real PTY input/output/resize", "detach preserves agent"] } });
  } catch (error) {
    await invoke("native_proof_report", { result: { ok: false, error: String(error) } });
  }
})();
