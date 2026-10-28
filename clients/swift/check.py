#!/usr/bin/env python3
"""Compile/run the Swift proof with shared goldens and real bridge envelopes.

No sessions, network requests, developer config, or native UI are touched.
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libexec"))
from t_bridge import serve

fixture = ROOT / "desktop/tests/fixtures/core-protocol.json"
golden = json.loads(fixture.read_text())


class FixtureCore:
    """Inject effect-free results; keep the actual bridge serializer unchanged."""
    def call(self, method, params):
        if method == "operations.apply":
            result = golden["sessions.open"]["applied"]
            self.progress(result["operationId"], "applying")
            return result
        return None


with tempfile.TemporaryDirectory(prefix="t-swift-proof-") as temporary:
    directory = Path(temporary)
    output = io.StringIO()
    requests = [
        {"version": 1, "id": "proof", "method": "operations.apply", "params": {}},
        {"version": 2, "id": "proof", "method": "invalid", "params": {}},
        {"version": 1, "id": "proof", "method": "attachments.validate", "params": {}},
    ]
    serve(FixtureCore(), io.StringIO("".join(json.dumps(row) + "\n" for row in requests)), output)
    wire = directory / "wire.jsonl"
    wire.write_text(output.getvalue())
    executable = directory / "swift-contract-proof"
    subprocess.run([
        "swiftc", "-swift-version", "6", "-warnings-as-errors",
        str(ROOT / "clients/swift/Sources/WorkspaceWire.swift"),
        str(ROOT / "clients/swift/Proof.swift"), "-o", str(executable),
    ], check=True)
    subprocess.run([str(executable), str(fixture), str(wire)], check=True)
