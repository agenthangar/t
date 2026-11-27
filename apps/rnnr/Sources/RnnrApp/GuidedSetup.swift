import AppKit
import Carbon
import RnnrCore
import SwiftUI

@MainActor struct GuidedSetup: View {
    enum Step: String, CaseIterable {
        case welcome = "Set up rnnr"
        case choose = "Choose how you’ll use rnnr"
        case terminal = "Allow Terminal access"
        case codex = "Connect Codex"
        case claude = "Connect Claude Code"
        case finish = "rnnr is ready"
    }
    let source: URL?
    let finish: () -> Void
    @State private var step = Step.welcome
    @State private var selected = Set<Step>()
    @State private var ready = Set<Step>()
    @State private var message = ""
    @State private var busy = false
    @State private var refreshID = UUID()
    @State private var terminalDenied = false
    @State private var integrationState = ""
    @State private var emptySessions = false
    private let choices: [Step] = [.terminal, .codex, .claude]
    private var pages: [Step] { [.welcome, .choose] + choices.filter(selected.contains) + [.finish] }
    private var index: Int { pages.firstIndex(of: step) ?? 0 }
    private var client: String { step == .codex ? "codex" : "claude" }
    private var description: String {
        switch step {
        case .welcome: return "Find your coding sessions in the menu bar and open them in Terminal."
        case .choose: return ""
        case .terminal: return "rnnr uses Terminal to open the session you choose. When macOS asks if rnnr can control Terminal, choose Allow."
        case .codex: return "Add the rnnr integration so you can find and inspect sessions from Codex."
        case .claude: return "Add the rnnr integration so you can find and inspect sessions from Claude Code."
        case .finish: return "Click the fox in the menu bar to see your sessions." + (emptySessions && selected.contains(.terminal) && ready.contains(.terminal) ? " Choose New to start your first session." : "")
        }
    }
    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading) {
                HStack { ForEach(pages.indices, id: \.self) { i in
                    Capsule().fill(i <= index ? Color.accentColor : Color.secondary.opacity(0.3)).frame(height: 4)
                } }
                Text("Step \(index + 1) of \(pages.count)").foregroundStyle(.secondary)
            }.padding(24)
            Spacer()
            VStack(spacing: 14) {
                if let image = foxImage() { Image(nsImage: image).resizable().scaledToFit().frame(width: 50, height: 50) }
                Text(step.rawValue).font(.title.bold())
                if !description.isEmpty { Text(description).foregroundStyle(.secondary).multilineTextAlignment(.center) }
                if step == .choose {
                    VStack(alignment: .leading, spacing: 12) {
                        choice(.terminal, "Open sessions in Terminal", "Open or resume a session from the menu bar.")
                        choice(.codex, "Connect Codex", "Find and inspect rnnr sessions from Codex.")
                        choice(.claude, "Connect Claude Code", "Find and inspect rnnr sessions from Claude Code.")
                    }
                }
                if !message.isEmpty { Text(message).foregroundStyle(ready.contains(step) ? Color.green : .secondary) }
                if busy { ProgressView().controlSize(.small) }
                else if !ready.contains(step) {
                    if step == .terminal && terminalDenied {
                        Button("Open Automation Settings") {
                            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")!)
                        }
                    } else if step == .terminal || ([.codex, .claude].contains(step) && ["available", "legacy", "signIn"].contains(integrationState)) {
                        Button(step == .terminal ? "Allow Terminal Access" : (integrationState == "legacy" ? "Update Integration" : step.rawValue)) { Task { await check(request: true) } }
                    } else if step != .choose && step != .finish {
                        Button("Check Again") { refreshID = UUID() }
                    }
                }
            }.frame(maxWidth: 460)
            Spacer()
            Divider()
            HStack {
                Button("Back") { move(-1) }.disabled(index == 0)
                if choices.contains(step) && !ready.contains(step) { Button("Skip for Now") { move(1) } }
                Spacer()
                Button(step == .finish ? "Open rnnr" : "Continue") {
                    if step == .finish { finish() } else { move(1) }
                }.keyboardShortcut(.defaultAction).disabled(!ready.contains(step) && ![.choose, .finish].contains(step))
            }.disabled(busy).padding(18)
        }.frame(width: 630, height: 465)
        .task(id: refreshID) { await check(request: false) }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            if !busy && choices.contains(step) { refreshID = UUID() }
        }
    }
    private func choice(_ value: Step, _ title: String, _ hint: String) -> some View {
        Toggle(isOn: Binding(get: { selected.contains(value) }, set: { on in
            if on { selected.insert(value) } else { selected.remove(value) }
        })) { Text(title); Text(hint).foregroundStyle(.secondary) }
    }
    private func move(_ by: Int) { step = pages[index + by]; message = ""; refreshID = UUID() }
    private func check(request: Bool) async {
        guard !busy, step != .choose, step != .finish else { return }
        busy = true
        defer { busy = false }
        ready.remove(step)
        guard let source, FileManager.default.isExecutableFile(atPath: source.appendingPathComponent("bin/rn").path) else {
            message = "This copy of rnnr is missing a required component. Reinstall rnnr to continue."
            return
        }
        do {
            switch step {
            case .welcome:
                let snapshot = try await Task.detached { try RnnrBackend.discover(executable: source.appendingPathComponent("bin/rn")) }.value
                guard snapshot.discoveryAvailable else { message = "Session discovery is unavailable. Check again."; return }
                emptySessions = snapshot.detached.isEmpty
                message = snapshot.status ?? "Ready"
            case .terminal:
                let status = await Task.detached {
                    let target = NSAppleEventDescriptor(bundleIdentifier: "com.apple.Terminal")
                    return AEDeterminePermissionToAutomateTarget(target.aeDesc, typeWildCard, typeWildCard, request)
                }.value
                terminalDenied = status == errAEEventNotPermitted
                guard status == noErr else {
                    if terminalDenied { message = "Turn on Terminal under rnnr in Automation settings." }
                    else if status == procNotFound { message = "Open Terminal, then return here to check access." }
                    else { message = request ? "Terminal access could not be verified." : "" }
                    return
                }
                message = "Allowed"
            case .codex, .claude:
                let target = client
                let action = request ? (integrationState == "legacy" ? "migrate" : "connect") : "check"
                integrationState = try await Task.detached { try RnnrBackend.integration(source: source, client: target, action: action) }.value
                switch integrationState {
                case "connected": message = "Connected"
                case "legacy": message = "Update your existing integration to use rnnr?"; return
                case "signIn": message = "Sign in to " + (step == .codex ? "Codex" : "Claude Code") + ", then return here."; return
                case "available": message = ""; return
                case "disabled": message = "This integration is disabled in the client."; return
                case "custom": message = "A different sessions integration is configured. Review it in the client."; return
                case "missing": message = "Install " + (step == .codex ? "Codex" : "Claude Code") + ", then choose Check Again."; return
                default: message = "The integration could not be verified. Check the client and try again."; return
                }
            default: return
            }
            ready.insert(step)
        } catch { message = "The check could not finish. Try again." }
    }
}
