import Foundation
import RnnrCore

var checks = 0
func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    checks += 1
    guard try condition() else { throw NSError(domain: "RnnrCoreChecks", code: checks, userInfo: [NSLocalizedDescriptionKey: message]) }
}
func snapshot(_ json: String) throws -> SessionSnapshot {
    try JSONDecoder().decode(SessionSnapshot.self, from: Data(json.utf8))
}
func json(_ value: Any) throws -> String {
    String(decoding: try JSONSerialization.data(withJSONObject: value), as: UTF8.self)
}

do {
    let raw: [String: Any] = ["slot": "repo-name-12", "title": "Session title", "cwd": "/tmp/work tree", "sid": "sid-1", "live": true, "state": "detached", "agent": "codex"]
    let fixture: [String: Any] = [
        "sessions": [raw, raw.merging(["slot": "repo-name-13", "state": "attached"]) { _, b in b },
                     raw.merging(["slot": "repo-name-14", "state": "foreground"]) { _, b in b },
                     raw.merging(["slot": "repo-name-15", "live": false]) { _, b in b }],
        "remote": [raw.merging(["host": "mini", "agent": NSNull()]) { _, b in b }],
        "hosts": ["mini"],
    ]
    let loaded = try snapshot(json(fixture))
    try check(loaded.detached.count == 2, "Only live detached rows should be displayed")
    try check(loaded.detached.allSatisfy(\.canTarget), "Numbered local and registered remote rows should target")
    try check(loaded.detached[0].id != loaded.detached[1].id, "Hosts must distinguish row identity")
    try check(loaded.detached[0].hostLabel == "This Mac", "Local host label")
    try check(loaded.detached[1].hostLabel == "mini", "Remote host label")
    try check(loaded.detached[1].agent == nil, "Remote agent metadata is optional")
    try check(loaded.status == nil, "Healthy snapshot has no warning")
    let missing = try snapshot("{\"sessions\":[{\"live\":true,\"state\":\"detached\"}]}")
    try check(missing.detached.count == 1 && !missing.detached[0].canTarget, "Missing identity remains visible but untargetable")
    try check(missing.detached[0].title == "Untitled session", "Missing title fallback")
    try check(try snapshot("{}").detached.isEmpty, "Empty response")
    let idle = try snapshot(json(["sessions": [raw.merging(["sid": ""]) { _, b in b }]]))
    try check(idle.detached[0].canTarget, "An empty SID can target an empty current identity")
    let unsafe = try snapshot(json(["sessions": [raw.merging(["cwd": "/tmp/control\nvalue"]) { _, b in b }]]))
    try check(!unsafe.detached[0].canTarget, "Control characters disable targeting")
    let unknown = try snapshot(json(["remote": [raw.merging(["host": "unknown"]) { _, b in b }]]))
    try check(!unknown.detached[0].canTarget, "Unregistered remote host cannot target")
    let foreground = try snapshot(json(["sessions": [raw.merging(["slot": "repo:abcdef"]) { _, b in b }]]))
    try check(!foreground.detached[0].canTarget, "Non-numbered session cannot target")
    let duplicate = try snapshot(json(["sessions": [raw, raw]]))
    try check(duplicate.detached.count == 1, "Deduplicate exact identities")
    for freshness: [String: Any] in [
        ["loading": true], ["unavailable": true], ["hosts": [["host": "mini", "stale": true]]],
        ["hosts": [["host": "mini", "refreshing": true]]],
        ["hosts": [["host": "mini", "error": "private diagnostic", "error_kind": "transport"]]],
        ["persistence_error": "private path"],
    ] {
        let value = try snapshot(json(["freshness": freshness]))
        try check(value.status != nil, "Incomplete discovery needs a status")
        try check(!(value.status?.contains("private") ?? true), "Do not expose private diagnostics")
    }
    try check(TerminalAction.new.arguments == ["session", "open", "--new"], "New command")
    try check(TerminalAction.all.arguments == ["session", "list", "--all", "--remote"], "All command")
    try check(TerminalAction.open.arguments == ["session", "open"], "Open command")
    try check(TerminalAction.resume.arguments == ["session", "resume", "--all"], "Resume command")
    try check(TerminalAction.move.arguments == ["session", "move", "--all", "--remote"], "Move command")
    try check(TerminalAction.attach(loaded.detached[0]).arguments == nil, "Attach has a guarded handoff")
    let helper = URL(fileURLWithPath: "/tmp/helper 'quote' $(touch hostile).zsh")
    let source = URL(fileURLWithPath: "/tmp/source 'quote' `touch hostile`")
    let handoff = try RnnrBackend.terminalCommand(helper: helper, source: source, action: .attach(loaded.detached[0]))
    try check(handoff.hasPrefix("/bin/zsh -lic 'exec "), "Terminal handoff runs a login interactive shell")
    try check(handoff.contains("attach") && !handoff.contains("--yes"), "Attach uses guarded operation")
    do {
        _ = try RnnrBackend.terminalCommand(helper: helper, source: source, action: .end(missing.detached[0]))
        throw NSError(domain: "RnnrCoreChecks", code: 1, userInfo: [NSLocalizedDescriptionKey: "Unsafe target accepted"])
    } catch BackendError.invalidTarget { checks += 1 }
    let script = RnnrBackend.appleScript(command: "example \"quoted\" \\ value")
    try check(script.contains("do script \"example \\\"quoted\\\" \\\\ value\""), "AppleScript escapes command data")
    try check(!script.contains("in window"), "A handoff creates a new Terminal window")

    // Verify actual shell parsing with a fake helper, never a real tmux session.
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("rnnr-check-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    defer { try? FileManager.default.removeItem(at: root) }
    let fakeHelper = root.appendingPathComponent("helper '$(touch injected)' .zsh")
    let captured = root.appendingPathComponent("captured")
    try Data("print -rn -- \"$1\" > \"$RNNR_CAPTURE\"\n".utf8).write(to: fakeHelper)
    let actual = try RnnrBackend.terminalCommand(helper: fakeHelper, source: source, action: .open)
    let shell = Process()
    shell.executableURL = URL(fileURLWithPath: "/bin/zsh")
    // Parse both quoting layers without loading the developer's shell profile.
    shell.arguments = ["-f", "-c", "zsh() { command /bin/zsh -f -c \"$2\"; }; " + actual.replacingOccurrences(of: "/bin/zsh -lic ", with: "zsh -lic ")]
    shell.environment = ["PATH": "/usr/bin:/bin", "HOME": root.path, "ZDOTDIR": root.path, "RNNR_CAPTURE": captured.path]
    shell.currentDirectoryURL = root
    try shell.run(); shell.waitUntilExit()
    try check(shell.terminationStatus == 0, "Hostile paths parse successfully")
    try check(try String(contentsOf: captured, encoding: .utf8) == source.path, "Source path survives both quoting layers exactly")
    try check(!FileManager.default.fileExists(atPath: root.appendingPathComponent("injected").path), "Shell substitutions never execute")

    let fakeExecutable = root.appendingPathComponent("t")
    try Data("#!/bin/sh\n[ \"$1\" = mcp ] && [ \"$2\" = --call ] && [ \"$3\" = list_sessions ] && [ \"$4\" = '{\"all_hosts\":true}' ] || exit 3\nprintf '%s' '{\"sessions\":[]}'\n".utf8).write(to: fakeExecutable)
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: fakeExecutable.path)
    try check(try RnnrBackend.discover(executable: fakeExecutable).detached.isEmpty, "Discovery passes exact read-only MCP arguments")
    try Data("#!/bin/sh\n/usr/bin/head -c 9000000 /dev/zero\n".utf8).write(to: fakeExecutable)
    do {
        _ = try RnnrBackend.discover(executable: fakeExecutable)
        throw NSError(domain: "RnnrCoreChecks", code: 1, userInfo: [NSLocalizedDescriptionKey: "Oversized response accepted"])
    } catch BackendError.oversizedResponse { checks += 1 }
    // A descendant can retain stdout after the main helper exits. The reader
    // must return promptly, close its descriptor, and leave no drain thread.
    try Data("#!/bin/sh\n/bin/sleep 2 &\nprintf '%s' '{\"sessions\":[]}'\n".utf8).write(to: fakeExecutable)
    let started = ProcessInfo.processInfo.systemUptime
    do {
        _ = try RnnrBackend.discover(executable: fakeExecutable)
        throw NSError(domain: "RnnrCoreChecks", code: 1, userInfo: [NSLocalizedDescriptionKey: "Inherited stdout did not time out"])
    } catch BackendError.discoveryTimedOut { checks += 1 }
    try check(ProcessInfo.processInfo.systemUptime - started < 1.5, "Inherited stdout is bounded independently of child descendants")
    print("Passed \(checks) rnnr core checks")
} catch {
    FileHandle.standardError.write(Data("rnnr core checks failed: \(error.localizedDescription)\n".utf8))
    exit(1)
}
