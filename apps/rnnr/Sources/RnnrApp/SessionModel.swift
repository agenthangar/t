import AppKit
import Foundation
import RnnrCore

@MainActor
final class SessionModel: ObservableObject {
    @Published private(set) var rows: [SessionRow] = []
    @Published private(set) var notice: String? = "Loading sessions…"
    private var hasSnapshot = false
    private var visible = false
    private var refreshTask: Task<Void, Never>?
    let source: URL?
    var dismiss: (() -> Void)?

    init() {
        source = Bundle.main.resourceURL?.appendingPathComponent("backend", isDirectory: true)
    }

    func opened() {
        visible = true
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            guard let self else { return }
            defer { self.refreshTask = nil }
            while self.visible {
                await self.refresh()
                guard self.visible else { break }
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    func closed() { visible = false }

    private func refresh() async {
        guard let source else {
            notice = "Session discovery unavailable: the bundled runtime is missing. Reinstall rnnr."
            return
        }
        let executable = source.appendingPathComponent("bin/t")
        do {
            let snapshot = try await Task.detached(priority: .utility) {
                try RnnrBackend.discover(executable: executable)
            }.value
            if snapshot.discoveryAvailable {
                rows = snapshot.detached
                hasSnapshot = true
            }
            notice = snapshot.status
        } catch {
            notice = hasSnapshot
                ? "Showing last available sessions. Refresh failed: \(error.localizedDescription)"
                : "Sessions unavailable: \(error.localizedDescription)"
        }
    }

    func perform(_ action: TerminalAction) {
        do {
            guard let source,
                  let helper = Bundle.main.url(forResource: "terminal", withExtension: "zsh") else {
                throw AppFailure.message("The Terminal helper or bundled runtime is missing. Reinstall rnnr.")
            }
            let command = try RnnrBackend.terminalCommand(helper: helper, source: source, action: action)
            dismiss?()
            var details: NSDictionary?
            guard let script = NSAppleScript(source: RnnrBackend.appleScript(command: command)) else {
                throw AppFailure.message("Could not prepare the Terminal handoff.")
            }
            script.executeAndReturnError(&details)
            if let details {
                throw AppFailure.message((details[NSAppleScript.errorMessage] as? String)
                    ?? "Terminal did not accept the command. Check Automation access for rnnr in System Settings.")
            }
        } catch {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "Could not open Terminal"
            alert.informativeText = error.localizedDescription
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    func confirmEnd(_ row: SessionRow) {
        guard row.canTarget else { return }
        dismiss?()
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "End \(row.slot) on \(row.hostLabel)?"
        alert.informativeText = "This ends the running session. Its saved history and worktree are preserved. Terminal will verify the session and may ask you to confirm again."
        alert.addButton(withTitle: "Continue in Terminal")
        alert.addButton(withTitle: "Cancel")
        // A destructive handoff must never be the default Return-key action.
        alert.buttons[0].keyEquivalent = ""
        alert.buttons[1].keyEquivalent = "\r"
        if alert.runModal() == .alertFirstButtonReturn { perform(.end(row)) }
    }
}

private enum AppFailure: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let message): return message }
    }
}
