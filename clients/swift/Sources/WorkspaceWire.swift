import Foundation

// The existing local helper's JSON contract. This is not a remote control API.
public enum JSONValue: Codable, Equatable, Sendable {
    case null, bool(Bool), number(Double), string(String)
    case array([JSONValue]), object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let decoded = try? value.decode(Bool.self) { self = .bool(decoded) }
        else if let decoded = try? value.decode(String.self) { self = .string(decoded) }
        else if let decoded = try? value.decode(Double.self) { self = .number(decoded) }
        else if let decoded = try? value.decode([JSONValue].self) { self = .array(decoded) }
        else { self = .object(try value.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .null: try value.encodeNil()
        case .bool(let decoded): try value.encode(decoded)
        case .number(let decoded): try value.encode(decoded)
        case .string(let decoded): try value.encode(decoded)
        case .array(let decoded): try value.encode(decoded)
        case .object(let decoded): try value.encode(decoded)
        }
    }

    public func decode<T: Decodable>(_ type: T.Type) throws -> T {
        try JSONDecoder().decode(type, from: JSONEncoder().encode(self))
    }
}

public struct CoreRequest: Encodable, Sendable {
    public let version = 1
    public let id: String
    public let method: String
    public let params: [String: JSONValue]

    public init(id: String, method: String, params: [String: JSONValue] = [:]) {
        self.id = id
        self.method = method
        self.params = params
    }
}

public struct CoreFailure: Decodable, Error, Sendable {
    public let code: String
    public let message: String
    public let retryable: Bool
}

public enum WireFailure: Error {
    case unsupportedVersion(Int), invalidEnvelope, mismatchedRequest
}

public enum CoreMessage: Decodable, Sendable {
    case result(id: JSONValue, value: JSONValue)
    case failure(id: JSONValue, error: CoreFailure)
    case progress(id: JSONValue, operationId: String, stage: String)
    // New notification kinds must not make a compatible client fail decoding.
    case notification(id: JSONValue, event: String)

    private enum Keys: String, CodingKey {
        case version, id, result, error, event, operationId, stage
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: Keys.self)
        let version = try values.decode(Int.self, forKey: .version)
        guard version == 1 else { throw WireFailure.unsupportedVersion(version) }
        let id = try values.decode(JSONValue.self, forKey: .id)
        if let event = try values.decodeIfPresent(String.self, forKey: .event) {
            guard !values.contains(.result), !values.contains(.error) else {
                throw WireFailure.invalidEnvelope
            }
            if event == "progress" {
                self = .progress(id: id,
                    operationId: try values.decode(String.self, forKey: .operationId),
                    stage: try values.decode(String.self, forKey: .stage))
            } else { self = .notification(id: id, event: event) }
        } else if values.contains(.result), !values.contains(.error) {
            self = .result(id: id, value: try values.decode(JSONValue.self, forKey: .result))
        } else if values.contains(.error), !values.contains(.result) {
            self = .failure(id: id, error: try values.decode(CoreFailure.self, forKey: .error))
        } else { throw WireFailure.invalidEnvelope }
    }
}

public struct CoreInitialization: Decodable, Sendable {
    public let protocolVersion: Int
    public let version: String
    public let capabilities: [CoreCapability]
}

public struct CoreCapability: Decodable, Sendable {
    public let method: String
    public let command: [String]
    public let kind: String?
    public let coreMethod: String?
}

public struct CoreSnapshot: Decodable, Sendable {
    // Preserve optional/extended session fields and evolving provider metadata.
    public let sessions: [[String: JSONValue]]
    public let repositories: [[String: JSONValue]]
    public let hosts: [[String: JSONValue]]
    public let config: [String: JSONValue]
    public let capabilities: [CoreCapability]
    public let remoteStatus: [[String: JSONValue]]?
}

public struct CoreOperation: Decodable, Sendable {
    public let operationId: String
    public let method: String
    public let arguments: [String: JSONValue]
    // Strings deliberately preserve future states; clients must display unknown
    // states without assuming success or offering automatic mutation replay.
    public let state: String
    public let summary: String
    public let command: [String]?
    public let result: JSONValue?
    public let attachment: JSONValue?
}

// A transport owns framing, bounded buffering, and request correlation. A future
// URLSession adapter must use an authenticated gateway, never the helper directly.
public protocol CoreTransport: Sendable {
    func messages(for request: CoreRequest) -> AsyncThrowingStream<CoreMessage, Error>
}

public struct CoreClient: Sendable {
    private let transport: any CoreTransport

    public init(transport: any CoreTransport) { self.transport = transport }

    public func request<T: Decodable>(
        _ request: CoreRequest, as type: T.Type,
        onProgress: @Sendable (String, String) -> Void = { _, _ in }
    ) async throws -> T {
        for try await message in transport.messages(for: request) {
            switch message {
            case .progress(let id, let operationId, let stage):
                guard id == .string(request.id) else { throw WireFailure.mismatchedRequest }
                onProgress(operationId, stage)
            case .result(let id, let value):
                guard id == .string(request.id) else { throw WireFailure.mismatchedRequest }
                return try value.decode(type)
            case .failure(let id, let error):
                guard id == .string(request.id) else { throw WireFailure.mismatchedRequest }
                throw error
            case .notification: continue
            }
        }
        throw WireFailure.invalidEnvelope
    }
}
