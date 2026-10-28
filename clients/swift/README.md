# Swift client foundation

Tracked in [issue #110](https://github.com/agenthangar/t/issues/110).

`Sources/WorkspaceWire.swift` is Foundation-only Swift 6 code suitable for sharing
with a future SwiftUI iOS app. It models the **existing local helper contract**:
versioned requests, correlated results/errors/progress, snapshots, capabilities,
and durable operations. Its transport protocol separates these values from
stdio, Tauri, or a future authenticated network transport. It does not launch a
helper or implement an HTTP server, URLSession transport, or iOS application.

Run the executable compatibility proof from the repository root:

```sh
python3 clients/swift/check.py
```

Requires Swift 6 and Python 3. The script compiles into a temporary directory,
decodes `desktop/tests/fixtures/core-protocol.json`, and obtains wire envelopes
from the actual `t_bridge.serve` serializer with an effect-free injected core.
It checks lifecycle operations, nullable commands/results, progress, structured
errors, version rejection, request correlation, and unknown fields/states. It
does not open windows or touch session state. The shared golden has empty session
rows, so this proves envelope/operation compatibility, not complete session-state
or remote gateway behavior. Dynamic JSON preserves provider-specific data;
session statuses and gateway identity fields need a dedicated contract next.

Raw helper attachment plans include host-local argv/env/paths. The proof decodes
them as data only. A future mobile transport must consume a restricted gateway
projection with opaque identifiers/tokens, never execute those plans or forward
arbitrary helper methods. See [mobile architecture](../../docs/mobile-client-architecture.md)
for that boundary and the remaining work.
