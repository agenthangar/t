import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public struct SessionRow: Identifiable, Sendable {
    public var id: String
    public let slot: String
    public let title: String
    public let host: String
    public let agent: String?
    public var canTarget: Bool
    public var hostLabel: String { host == "local" ? "This Mac" : host }
    // Only the ownership guard needs these fields; never display or log them.
    let cwd: String
    let sid: String

    init(raw: WireRow, host: String, configured: Bool) {
        slot = visible(raw.slot ?? "Unknown session")
        title = visible(raw.title.flatMap { $0.isEmpty ? nil : $0 } ?? "Untitled session")
        self.host = visible(host)
        agent = raw.agent.map(visible)
        cwd = raw.cwd ?? ""
        sid = raw.sid ?? ""
        let components = [host, raw.slot ?? "", sid, cwd]
        id = components.map { "\($0.utf8.count):\($0)" }.joined()
        canTarget = configured && safe(host) && safe(cwd) && cwd.hasPrefix("/") && safe(sid)
            && raw.slot != nil && safe(raw.slot!) && numberedSlot(raw.slot!)
    }
}

private func safe(_ value: String) -> Bool {
    !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
}

private func visible(_ value: String) -> String {
    String(value.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) })
}

private func numberedSlot(_ value: String) -> Bool {
    value.range(of: "^[A-Za-z0-9_][A-Za-z0-9_.-]*-[0-9]+$", options: .regularExpression) != nil
}

struct WireRow: Decodable {
    let slot: String?
    let title: String?
    let host: String?
    let agent: String?
    let cwd: String?
    let sid: String?
    let live: Bool?
    let state: String?
}

public struct SessionSnapshot: Decodable, Sendable {
    public var detached: [SessionRow]
    public var status: String?

    private enum CodingKeys: String, CodingKey { case sessions, remote, hosts, freshness }
    private struct Freshness: Decodable {
        var loading: Bool?
        var unavailable: Bool?
        var persistence_error: String?
        var hosts: [Host]?
        struct Host: Decodable {
            var host: String?
            var stale: Bool?
            var refreshing: Bool?
            var error: String?
            var error_kind: String?
        }
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let local = try values.decodeIfPresent([WireRow].self, forKey: .sessions) ?? []
        let remote = try values.decodeIfPresent([WireRow].self, forKey: .remote) ?? []
        let hosts = try values.decodeIfPresent([String].self, forKey: .hosts) ?? []
        let fresh = try values.decodeIfPresent(Freshness.self, forKey: .freshness)
        var rows = local.map { ($0, "local") }
        rows += remote.compactMap { row in row.host.map { (row, $0) } }
        var seen = Set<String>()
        detached = rows.compactMap { raw, host in
            guard raw.live == true, raw.state == "detached" else { return nil }
            let row = SessionRow(raw: raw, host: host, configured: host == "local" || hosts.contains(host))
            return seen.insert(row.id).inserted ? row : nil
        }
        let health = fresh?.hosts ?? []
        if fresh?.unavailable == true {
            status = "Session discovery unavailable. Showing the last available sessions."
        } else if health.contains(where: { !($0.error ?? "").isEmpty || !($0.error_kind ?? "").isEmpty }) {
            status = "Some hosts are unavailable. Showing cached sessions; actions verify ownership."
        } else if fresh?.loading == true || health.contains(where: { $0.refreshing == true }) {
            status = "Refreshing sessions… Showing available results."
        } else if health.contains(where: { $0.stale == true }) {
            status = "Showing older cached sessions. Refresh to check for changes."
        } else if !(fresh?.persistence_error ?? "").isEmpty {
            status = "Session cache storage is unavailable. Showing available sessions."
        } else {
            status = nil
        }
    }
}

public enum TerminalAction: Sendable {
    case new, all, open, resume, move, attach(SessionRow), end(SessionRow)

    public var arguments: [String]? {
        switch self {
        case .new: return ["session", "open", "--new"]
        case .all: return ["session", "list", "--all", "--remote"]
        case .open: return ["session", "open"]
        case .resume: return ["session", "resume", "--all"]
        case .move: return ["session", "move", "--all", "--remote"]
        case .attach, .end: return nil
        }
    }
}

public enum BackendError: Error, LocalizedError {
    case invalidTarget, discoveryFailed, discoveryTimedOut, oversizedResponse
    public var errorDescription: String? {
        switch self {
        case .invalidTarget: return "This session cannot be targeted safely. Refresh or use Open."
        case .discoveryFailed: return "Could not load sessions. Check the configured t source and try again."
        case .discoveryTimedOut: return "Session discovery timed out. Try refreshing."
        case .oversizedResponse: return "Session discovery returned too much data."
        }
    }
}

public enum RnnrBackend {
    public static func discover(executable: URL) throws -> SessionSnapshot {
        guard executable.isFileURL, executable.path.hasPrefix("/"), safe(executable.path) else {
            throw BackendError.discoveryFailed
        }
        let data = try run(executable: executable, arguments: ["mcp", "--call", "list_sessions", "{\"all_hosts\":true}"])
        do { return try JSONDecoder().decode(SessionSnapshot.self, from: data) }
        catch { throw BackendError.discoveryFailed }
    }

    public static func terminalCommand(helper: URL, source: URL, action: TerminalAction) throws -> String {
        guard helper.isFileURL, source.isFileURL, safe(helper.path), safe(source.path) else {
            throw BackendError.invalidTarget
        }
        var args = ["/bin/zsh", helper.path, source.path]
        switch action {
        case .attach(let row), .end(let row):
            guard row.canTarget, safe(row.cwd), safe(row.sid), safe(row.host), numberedSlot(row.slot) else {
                throw BackendError.invalidTarget
            }
            let operation: String
            if case .attach = action { operation = "attach" } else { operation = "end" }
            args += [operation, row.host, row.slot, row.cwd, row.sid]
        default:
            args += action.arguments ?? []
        }
        return "/bin/zsh -lic " + quote("exec " + args.map(quote).joined(separator: " "))
    }

    public static func appleScript(command: String) -> String {
        let escaped = command.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\n", with: "\\n")
        return "tell application \"Terminal\"\nactivate\ndo script \"\(escaped)\"\nend tell"
    }

    private static func quote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    // One nonblocking reader bounds both time and memory, even when a child
    // leaves descendants holding stdout. Never retain a background drain task.
    // stderr is discarded: diagnostics may contain private paths or prompts.
    private static func run(executable: URL, arguments: [String]) throws -> Data {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        let existing = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PATH"] = ([executable.deletingLastPathComponent().path, existing,
                                 "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                                 "/usr/sbin", "/sbin"]).joined(separator: ":")
        process.environment = environment
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardOutput = pipe
        let descriptor = pipe.fileHandleForReading.fileDescriptor
        guard fcntl(descriptor, F_SETFL, fcntl(descriptor, F_GETFL) | O_NONBLOCK) != -1 else {
            throw BackendError.discoveryFailed
        }
        defer {
            try? pipe.fileHandleForReading.close()
            try? pipe.fileHandleForWriting.close()
            if process.isRunning {
                process.terminate()
                Thread.sleep(forTimeInterval: 0.1)
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
        }
        do { try process.run() } catch { throw BackendError.discoveryFailed }
        try? pipe.fileHandleForWriting.close()
        let limit = 8 * 1024 * 1024
        var data = Data()
        var bytes = [UInt8](repeating: 0, count: 64 * 1024)
        var eof = false
        var deadline = ProcessInfo.processInfo.systemUptime + 15
        var exited = false
        while true {
            // Bound each drain pass so a noisy writer cannot starve the deadline.
            for _ in 0..<64 {
                let count = bytes.withUnsafeMutableBytes { read(descriptor, $0.baseAddress, $0.count) }
                if count > 0 {
                    guard count <= limit - data.count else { throw BackendError.oversizedResponse }
                    data.append(contentsOf: bytes.prefix(count))
                } else if count == 0 {
                    eof = true
                    break
                } else if errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR {
                    break
                } else {
                    throw BackendError.discoveryFailed
                }
            }
            let running = process.isRunning
            if !running && eof { break }
            let now = ProcessInfo.processInfo.systemUptime
            if !running && !exited {
                exited = true
                deadline = min(deadline, now + 0.5)
            }
            guard now < deadline else { throw BackendError.discoveryTimedOut }
            Thread.sleep(forTimeInterval: 0.01)
        }
        guard process.terminationStatus == 0 else { throw BackendError.discoveryFailed }
        return data
    }
}
