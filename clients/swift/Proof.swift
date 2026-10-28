import Foundation

private struct FixtureTransport: CoreTransport {
    let lines: [Data]
    func messages(for request: CoreRequest) -> AsyncThrowingStream<CoreMessage, Error> {
        AsyncThrowingStream { continuation in
            do {
                for line in lines {
                    continuation.yield(try JSONDecoder().decode(CoreMessage.self, from: line))
                }
                continuation.finish()
            } catch { continuation.finish(throwing: error) }
        }
    }
}

private func require(_ condition: Bool, _ message: String) throws {
    if !condition { throw NSError(domain: "SwiftContractProof", code: 1,
        userInfo: [NSLocalizedDescriptionKey: message]) }
}

@main struct Proof {
    static func main() async throws {
        let decoder = JSONDecoder()
        let fixture = try decoder.decode([String: JSONValue].self,
            from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let initialization = try fixture["initialize"]!.decode(CoreInitialization.self)
        try require(initialization.protocolVersion == 1, "handshake version")
        let snapshot = try fixture["snapshot"]!.decode(CoreSnapshot.self)
        try require(snapshot.capabilities.contains { $0.method == "session.open" }, "shared capabilities")
        for name in ["sessions.open", "sessions.resume", "sessions.restart", "sessions.close", "issues.start"] {
            guard case .object(let pair) = fixture[name] else { throw WireFailure.invalidEnvelope }
            let prepared = try pair["prepared"]!.decode(CoreOperation.self)
            let applied = try pair["applied"]!.decode(CoreOperation.self)
            try require(prepared.command == nil && prepared.state == "prepared", "typed operation review")
            try require(applied.operationId == prepared.operationId && applied.state == "completed", "durable operation")
        }
        // The bridge envelopes below come from the actual Python serializer;
        // the existing shared fixture contains result bodies only.
        let wire = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
        let lines = wire.split(separator: 10).map { Data($0) }
        try require(lines.count == 4, "progress/result/error/null bridge frames")
        guard case .progress(let id, _, let stage) = try decoder.decode(CoreMessage.self, from: lines[0]) else {
            throw WireFailure.invalidEnvelope
        }
        try require(id == .string("proof") && stage == "applying", "correlated progress")
        let client = CoreClient(transport: FixtureTransport(lines: Array(lines.prefix(2))))
        let operation = try await client.request(CoreRequest(id: "proof", method: "operations.apply"), as: CoreOperation.self)
        try require(operation.state == "completed", "client decodes actual operation")
        let errorClient = CoreClient(transport: FixtureTransport(lines: [lines[2]]))
        do {
            let _: JSONValue = try await errorClient.request(CoreRequest(id: "proof", method: "invalid"), as: JSONValue.self)
            throw WireFailure.invalidEnvelope
        } catch let error as CoreFailure {
            try require(error.code == "invalid_request" && !error.retryable, "structured bridge error")
        }
        guard case .result(_, .null) = try decoder.decode(CoreMessage.self, from: lines[3]) else {
            throw WireFailure.invalidEnvelope
        }
        // Deliberate compatibility probes, distinct from the shared goldens.
        let unknown = Data(#"{"version":1,"id":"proof","result":{"operationId":"opaque","method":"future","arguments":{},"state":"future-state","summary":"future","command":null,"newField":true}}"#.utf8)
        let unknownClient = CoreClient(transport: FixtureTransport(lines: [unknown]))
        let future = try await unknownClient.request(CoreRequest(id: "proof", method: "future"), as: CoreOperation.self)
        try require(future.state == "future-state", "forward compatible states/fields")
        do {
            _ = try decoder.decode(CoreMessage.self, from: Data(#"{"version":2,"id":"proof","result":null}"#.utf8))
            throw WireFailure.invalidEnvelope
        } catch WireFailure.unsupportedVersion(2) { }
        let wrongIDClient = CoreClient(transport: FixtureTransport(lines: [lines[1]]))
        do {
            let _: CoreOperation = try await wrongIDClient.request(CoreRequest(id: "other", method: "operations.apply"), as: CoreOperation.self)
            throw WireFailure.invalidEnvelope
        } catch WireFailure.mismatchedRequest { }
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(CoreRequest(id: "proof", method: "workspace.snapshot"))) as! [String: Any]
        try require(encoded["version"] as? Int == 1 && encoded["params"] is [String: Any], "request envelope")
        print("Swift contract proof passed: shared snapshot/capabilities, five lifecycle pairs, actual bridge progress/error/null, version and correlation guards, future states.")
    }
}
