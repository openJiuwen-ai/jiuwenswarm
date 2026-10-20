import AppKit
@preconcurrency import ApplicationServices
import CoreGraphics

@MainActor
final class Driver {
    private let session = UUID().uuidString
    private let wireVersion = 5
    private var handshaken = false
    private lazy var native = NativeExecution(state: state, session: session, cancelActive: { [weak self] in self?.requests.cancelActive() })
    private let ledger = RequestLedger()
    private var activeID: Int64?
    private var cancellationCode: String?
    private let requests = RequestLifetime()
    private let state: NativeDesktopState
    private lazy var cuaDesktop = CuaDesktopBackend(state: state)
    private let cuaBackendEnabled = CommandLine.arguments.contains("--cua-backend")
    init() {
        let state = NativeDesktopState()
        self.state = state
        state.sharing.onUserStop = { [weak self] in
            self?.state.pointer.stop()
            self?.requests.cancelActive()
        }
    }

    static func permissionStatus(request: Bool = false) -> [String: Any] {
        if request {
            // Only the explicit setup command may prompt. Requesting access is
            // not evidence of a grant: AX prompts asynchronously, and macOS
            // can require restarting the responsible launcher after approval.
            if !AXIsProcessTrusted() {
                let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
                _ = AXIsProcessTrustedWithOptions(options)
            }
            if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
            if !CGPreflightPostEventAccess() { _ = CGRequestPostEventAccess() }
        }
        return ["accessibility": AXIsProcessTrusted(),
         "screenCapture": CGPreflightScreenCaptureAccess(),
         "input": CGPreflightPostEventAccess(),
         "executable": Bundle.main.executablePath ?? CommandLine.arguments[0]]
    }

    func shutdown(code: Int32) {
        guard !requests.stopping else { return }
        let operation = requests.cancel()
        // Decoration has no input cleanup responsibilities. Remove it as soon
        // as the connection ends, even while native input is still unwinding.
        state.pointer.shutdown()
        Task { @MainActor in
            await operation?.value
            native.shutdown()
            await state.sharing.stop()
            diagnose(["kind": "driver_shutdown", "time": ProcessInfo.processInfo.systemUptime, "code": code])
            exit(code)
        }
    }

    private func reply(_ id: Int64, _ payload: [String: Any], remember: Bool = false) {
        let value = payload.merging(["version": wireVersion, "session": session, "id": id]) { _, envelope in envelope }
        do {
            var data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed])
            data.append(10)
            if remember { ledger.finish(id: id, response: data) }
            try FileHandle.standardOutput.write(contentsOf: data)
        } catch { shutdown(code: 1) }
    }

    private func failure(_ code: String, target: String = "request", effect: String = "none", detail: String, next: String, context: [String: Any]? = nil) -> [String: Any] {
        var failure: [String: Any] = ["code": code, "target": target, "effect": effect, "detail": detail, "next": next]
        if let context { failure["context"] = context }
        return ["error": failure]
    }

    func receive(_ data: Data) {
        guard !requests.stopping else { return }
        var incomingID: Int64?
        do {
            guard !data.starts(with: [0xef, 0xbb, 0xbf]), String(data: data, encoding: .utf8) != nil,
                  let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw DriverError("invalid UTF-8 or JSON object")
            }
            // Cancellation is a one-way control frame. The original request
            // receives its terminal reply only after input cleanup has unwound.
            if raw["cancel"] != nil {
                _ = try object(raw, keys: ["version", "session", "cancel"])
                guard try number(raw["version"], label: "version") == Double(wireVersion), handshaken,
                      try string(raw["session"], label: "session") == session else { throw DriverError("invalid cancellation session") }
                let n = try number(raw["cancel"], label: "cancel")
                guard n > 0, n <= 9_007_199_254_740_991, n.rounded() == n else { throw DriverError("invalid cancellation id") }
                if activeID == Int64(n) { cancellationCode = "cancelled"; native.cancel(); requests.cancelActive() }
                return
            }
            let n = try number(raw["id"], label: "id")
            guard n >= 0, n <= 9_007_199_254_740_991, n.rounded() == n else { throw DriverError("invalid id") }
            let id = Int64(n); incomingID = id
            let requestedVersion = try number(raw["version"], label: "version")
            guard requestedVersion == Double(wireVersion) else {
                reply(id, failure("version_mismatch", detail: "this macOS driver requires protocol 5", next: "update the SDK and native backend together; legacy commands are not supported")); return
            }
            if id == 0 {
                guard !handshaken else { throw DriverError("hello has already completed for this connection", code: .invalidRequest) }
                _ = try object(raw, keys: ["version", "id", "method", "params"])
                guard try string(raw["method"], label: "method") == "hello" else { throw DriverError("id 0 is reserved for hello") }
                let params = try object(raw["params"], keys: ["desktopMode"])
                let requestedMode = try string(params["desktopMode"], label: "desktopMode")
                let mode = ProcessInfo.processInfo.environment["COMPUTER_USE_DESKTOP"] ?? "shared"
                guard ["shared", "exclusive"].contains(mode), requestedMode == mode else {
                    reply(id, failure("mode_mismatch", detail: "requested desktop mode does not match the backend", next: "configure the SDK and backend for the same shared/exclusive mode")); return
                }
                handshaken = true
                reply(id, ["result": ["platform": "macos", "desktopMode": mode, "maxTimeoutMs": 120_000,
                    "capabilities": ["cancel", "requestDedup", "desktopCoordination", "nativeOperations", "nativeBoundContracts", "nativeUnitReadinessV2", "nativeDirectory", "nativeApplicationRoots", "nativeMenuActions", "nativeCapture", "nativeCaptureKinds", "nativeDesktopInput", "nativeRelations", "nativeNodePages", "nativeNodeDepthBudget", "nativeRelationPages", "nativeCapturedText", "nativeActionIntents", "nativeWindowScope", "nativeKeyboardIntents", "nativeTextInput", "nativeActuators", "nativeTextReplacement", "nativeSelectedText", "nativeEditorPreparation", "nativePointer", "nativeElementPointer", "nativeElementScroll", "nativeSelectionBasis", "nativeControlParts", "nativeTableFacts", "nativeBusyFacts", "nativeVirtualFocus", "nativeFocusMembers", "nativeAdvertisedFields", "nativeFollowRelations", "nativeLaunch", "nativeScroll"] + (cuaBackendEnabled ? ["cuaDesktopBackend"] : [])]])
                return
            }
            _ = try object(raw, keys: ["version", "session", "id", "method", "params", "timeoutMs"])
            guard handshaken, try string(raw["session"], label: "session") == session else {
                reply(id, failure("session_mismatch", detail: "request does not belong to this live session", next: "perform hello and obtain fresh observations; do not replay an old action")); return
            }
            let timeout = try number(raw["timeoutMs"], label: "timeoutMs")
            guard (1...120_000).contains(timeout), timeout.rounded() == timeout else { throw DriverError("timeoutMs must be an integer in 1...120000") }
            _ = try string(raw["method"], label: "method")
            let canonical = try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys, .withoutEscapingSlashes])
            switch ledger.begin(id: id, request: canonical) {
            case .replay(let cached):
                do { try FileHandle.standardOutput.write(contentsOf: cached) } catch { shutdown(code: 1) }
                return
            case .inProgress:
                reply(id, failure("in_progress", effect: "possible", detail: "this request is already executing; no duplicate was dispatched", next: "wait for its terminal response")); return
            case .expired:
                reply(id, failure("request_expired", effect: "possible", detail: "request id is below the session high-water mark and its result is no longer retained; no duplicate was dispatched", next: "observe the current state; do not replay the action with a new id")); return
            case .conflict:
                reply(id, failure("request_conflict", detail: "this request id was already used with different content; no new action was dispatched", next: "inspect the original request and current state")); return
            case .start: break
            }
            if activeID != nil {
                reply(id, failure("busy", detail: "another request is executing", next: "wait for the active request and its cleanup to finish"), remember: true)
                return
            }
            activeID = id; cancellationCode = nil
            let deadline = ProcessInfo.processInfo.systemUptime + timeout / 1000
            _ = requests.start(timeoutMs: Int(timeout), onTimeout: { self.cancellationCode = "deadline_exceeded" }) { await self.perform(raw, id: id, deadline: deadline) }
        } catch {
            if let id = incomingID {
                reply(id, failure("invalid_request", detail: String(describing: error), next: "correct the protocol envelope before sending any action"))
            } else {
                diagnose("protocol: \(error)"); shutdown(code: 1)
            }
        }
    }

    private func perform(_ raw: [String: Any], id: Int64, deadline: TimeInterval) async {
        await RequestDiagnostics.$deadline.withValue(deadline) {
            await performWithinBudget(raw, id: id, deadline: deadline)
        }
    }

    private func performWithinBudget(_ raw: [String: Any], id: Int64, deadline: TimeInterval) async {
        defer {
            activeID = nil; cancellationCode = nil
        }
        state.beginRequest()
        let method = raw["method"] as? String ?? "request"
        do {
            try RequestDiagnostics.checkCancellation()
            // Native units own their availability checks. A session never
            // enters the retired high-level read/input strategy, and finish
            // remains callable to release resources when desktop access is lost.
            guard NativeExecution.methods.contains(method) || cuaBackendEnabled && method == "cuaDesktop" else {
                throw DriverError("method '\(method)' is not supported by protocol v5", code: .invalidRequest,
                    next: "use begin, inspect, capture, execute and finish in this native session")
            }
            let remaining = max(1, Int((deadline - ProcessInfo.processInfo.systemUptime) * 1000))
            let result: [String: Any]
            if method == "cuaDesktop" { result = try await cuaDesktop.perform(raw["params"]) }
            else { result = try await native.perform(method, raw["params"], timeoutMs: remaining) }
            if !requests.stopping { reply(id, ["result": result], remember: true) }

        } catch {
            // The connection may already be closed. Cleanup in the operation
            // must finish without writing another response or restarting work.
            guard !requests.stopping else { return }
            let classified = error as? DriverError
            let code = (error as? ClassifiedFailure)?.code ?? .executionFailed
            let recovery = classified?.next ?? code.next
            // A nested failure may know a text prefix was dispatched. Never let
            // a later no-action validation erase earlier preparation or input.
            let effect = (classified?.effect ?? .none).including(state.effectPossible ? .possible : .none)
            // Keep facts and recovery separate on the wire. Application labels
            // may contain arbitrary prose, including "Effect:" and "Next:".
            let detail = String(describing: error)
            reply(id, failure(cancellationCode ?? (error is RequestDeadlineExceeded ? "deadline_exceeded" : code.rawValue), target: "native request",
                effect: effect.rawValue, detail: detail, next: recovery), remember: true)
        }
    }

}
