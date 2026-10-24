import AppKit
import ApplicationServices
import Darwin

/// Run the production receiver, request lifetime and JSON response path without
/// opening a window, reading another application or dispatching any input.
@main struct DriverDispatchTests {
    @MainActor final class Peer {
        let driver = Driver()
        let version: Int
        private let pipe = Pipe()
        private let savedOutput: Int32
        private var pending = Data()
        private var session = ""
        private var nextID: Int64 = 1
        private var restored = false

        init(version: Int) {
            self.version = version
            savedOutput = dup(STDOUT_FILENO)
            precondition(savedOutput >= 0)
            precondition(dup2(pipe.fileHandleForWriting.fileDescriptor, STDOUT_FILENO) >= 0)
            let descriptor = pipe.fileHandleForReading.fileDescriptor
            precondition(fcntl(descriptor, F_SETFL, fcntl(descriptor, F_GETFL) | O_NONBLOCK) >= 0)
        }

        func restore() {
            guard !restored else { return }
            restored = true
            precondition(dup2(savedOutput, STDOUT_FILENO) >= 0)
            close(savedOutput)
        }

        func handshake() async throws {
            let frame: [String: Any] = ["version": version, "id": 0, "method": "hello",
                "params": ["desktopMode": ProcessInfo.processInfo.environment["COMPUTER_USE_DESKTOP"] ?? "shared"]]
            let reply = try await exchange(frame)
            precondition(reply["error"] == nil)
            session = reply["session"] as! String
        }

        func frame(_ method: String, _ params: [String: Any]) -> [String: Any] {
            defer { nextID += 1 }
            return ["version": version, "session": session, "id": nextID, "method": method,
                    "params": params, "timeoutMs": 10_000]
        }

        func call(_ method: String, _ params: [String: Any]) async throws -> [String: Any] {
            try await exchange(frame(method, params))
        }

        func exchange(_ frame: [String: Any]) async throws -> [String: Any] {
            driver.receive(try JSONSerialization.data(withJSONObject: frame))
            let end = ProcessInfo.processInfo.systemUptime + 5
            while ProcessInfo.processInfo.systemUptime < end {
                var bytes = [UInt8](repeating: 0, count: 8192)
                let count = bytes.withUnsafeMutableBytes {
                    Darwin.read(pipe.fileHandleForReading.fileDescriptor, $0.baseAddress, $0.count)
                }
                if count > 0 { pending.append(contentsOf: bytes.prefix(count)) }
                else if count < 0 { precondition(errno == EAGAIN || errno == EINTR) }
                if let newline = pending.firstIndex(of: 10) {
                    let line = Data(pending[..<newline])
                    pending.removeSubrange(...newline)
                    let response = try JSONSerialization.jsonObject(with: line) as! [String: Any]
                    precondition(response["version"] as? Int == 5)
                    precondition((response["id"] as? NSNumber)?.int64Value == (frame["id"] as? NSNumber)?.int64Value)
                    return response
                }
                try await Task.sleep(for: .milliseconds(2))
            }
            preconditionFailure("driver did not complete request \(frame)")
        }

        var scope: [String: Any] {
            ["session": session, "instance": NSNull(), "rootWindow": NSNull(),
             "surface": NSNull(), "node": NSNull(), "capture": NSNull()]
        }

        func cancel(_ id: Int64) throws {
            driver.receive(try JSONSerialization.data(withJSONObject: [
                "version": version, "session": session, "cancel": id]))
        }
    }

    static func error(_ response: [String: Any], code: String) -> [String: Any] {
        let error = response["error"] as! [String: Any]
        precondition(error["code"] as? String == code, "\(response)")
        precondition(error["effect"] as? String == "none", "\(response)")
        return error
    }

    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        try await nativeDispatch()
        try await legacyHelloRefused()
        try await unregisteredSurfaceCannotBindNativeInput()
        print("PASS protocol 5 only; legacy hello refused; dedup and target scope remain isolated")
    }

    @MainActor static func nativeDispatch() async throws {
        let peer = Peer(version: 5)
        defer { peer.restore() }
        try await peer.handshake()
        let duplicate = try await peer.exchange(["version": 5, "id": 0, "method": "hello",
            "params": ["desktopMode": ProcessInfo.processInfo.environment["COMPUTER_USE_DESKTOP"] ?? "shared"]])
        _ = error(duplicate, code: "invalid_request")
        let legacy = ["listApps", "readApp", "readDesktop", "findElement", "readElementText",
            "click", "scroll", "drag", "typeText", "paste", "pressKey", "clickElement",
            "openElementMenu", "performElementAction", "pressKeyElement", "focusElement",
            "typeTextElement", "pasteElement", "unrecognizedMethod", "hello", "cancel"]
        func refused(_ method: String) async throws {
            // Reject the method before parsing a legacy payload. This also
            // makes this regression test harmless against the old dispatcher.
            let value = try await peer.call(method, ["unexpected": true])
            let failure = error(value, code: "invalid_request")
            precondition(failure["detail"] as? String == "method '\(method)' is not supported by protocol v5")
        }
        for method in legacy { try await refused(method) }

        let opened = try await peer.call("begin", ["mode": "read", "target": peer.scope,
            "intent": ["kind": "read", "parameters": [:]]])
        let operation = (opened["result"] as! [String: Any])["operation"] as! String
        // The denial must not depend on current operation state, or consume
        // that operation. Its captured-text query still reaches the endpoint.
        for method in legacy { try await refused(method) }
        let inspected = try await peer.call("inspect", ["operation": operation, "target": peer.scope,
            "query": ["kind": "capturedText", "textRef": "not-retained", "offset": 0, "length": 2]])
        _ = error(inspected, code: "snapshot_expired")
        for method in ["capture", "execute"] {
            let failure = error(try await peer.call(method, [:]), code: "invalid_request")
            precondition(!(failure["detail"] as! String).contains("not supported by protocol v5"))
        }
        // Cancellation remains a one-way envelope, not an ordinary method.
        try peer.cancel(999_999)
        let closed = try await peer.call("finish", ["operation": operation])
        precondition((closed["result"] as? [String: Any])?["cleanup"] as? String == "complete")
        for method in legacy { try await refused(method) }
        let rejected = peer.frame("readApp", [:])
        let first = try await peer.exchange(rejected), replay = try await peer.exchange(rejected)
        precondition(NSDictionary(dictionary: first).isEqual(to: replay), "rejection must retain request dedup semantics")
        let reopened = try await peer.call("begin", ["mode": "read", "target": peer.scope,
            "intent": ["kind": "read", "parameters": [:]]])
        let next = (reopened["result"] as! [String: Any])["operation"] as! String
        _ = try await peer.call("finish", ["operation": next])
    }

    @MainActor static func legacyHelloRefused() async throws {
        let peer = Peer(version: 5)
        defer { peer.restore() }
        for version in [3, 4, 6] {
            let response = try await peer.exchange(["version": version, "id": 0, "method": "hello",
                "params": ["desktopMode": ProcessInfo.processInfo.environment["COMPUTER_USE_DESKTOP"] ?? "shared"]])
            let failure = error(response, code: "version_mismatch")
            precondition(failure["detail"] as? String == "this macOS driver requires protocol 5")
        }
        // A refused legacy hello does not create a session or prevent a valid
        // client from negotiating the current version on this connection.
        try await peer.handshake()
        let opened = try await peer.call("begin", ["mode": "read", "target": peer.scope,
            "intent": ["kind": "read", "parameters": [:]]])
        let token = (opened["result"] as! [String: Any])["operation"] as! String
        _ = try await peer.call("finish", ["operation": token])
    }

    @MainActor static func unregisteredSurfaceCannotBindNativeInput() async throws {
        let state = NativeDesktopState(), owner = NSRunningApplication.current
        let selected = state.appSelections.get(owner: owner, root: "unregistered-window")
        let session = "native-scope-probe"
        let endpoint = NativeExecution(state: state, session: session, cancelActive: {})
        let binding: [String: Any] = ["session": session, "instance": selected.id, "rootWindow": "unregistered-window",
            "surface": "unregistered-surface", "node": NSNull(), "capture": NSNull()]
        do {
            _ = try await endpoint.perform("begin", ["mode": "write", "target": binding,
                "intent": ["kind": "insert", "parameters": ["text": "must-not-be-sent"]]], timeoutMs: 1000)
            preconditionFailure("an unregistered surface authorized a native input scope")
        } catch let failure as DriverError {
            precondition(failure.code == .staleTarget)
            precondition(failure.description == "target has no bound working-window observation")
        }
        precondition(!endpoint.hasUnfinishedOperation, "rejected target must not retain an operation or desktop lease")
    }
}
