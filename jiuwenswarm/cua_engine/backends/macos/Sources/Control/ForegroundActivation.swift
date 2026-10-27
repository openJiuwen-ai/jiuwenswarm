import AppKit
import Foundation
import Darwin

/// One process-activation adapter. The parent owns policy, original window,
/// scope, request budget and retry decisions; this child never creates NSApp.
enum ForegroundActivation {
    static let argument = "--internal-foreground-activation-v1"

    struct Request: Codable {
        let version: Int
        let pid: pid_t
        let seconds: UInt64
        let microseconds: UInt64
        let executable: String
        let deadline: TimeInterval

        init(identity: NativeProcessIdentity, deadline: TimeInterval) {
            version = 1; pid = identity.pid; seconds = identity.seconds
            microseconds = identity.microseconds; executable = identity.executable; self.deadline = deadline
        }

        var identity: NativeProcessIdentity {
            .init(pid: pid, parent: 0, seconds: seconds, microseconds: microseconds, executable: executable)
        }

        static func decode(_ text: String) throws -> Self {
            guard text.utf8.count <= 16_384, let data = text.data(using: .utf8) else { throw InvalidRequest() }
            let value = try JSONDecoder().decode(Self.self, from: data)
            guard value.version == 1, value.pid > 0, !value.executable.isEmpty,
                  value.deadline.isFinite, value.microseconds < 1_000_000 else { throw InvalidRequest() }
            return value
        }
    }

    struct Receipt: Codable {
        let version: Int
        let invoked: Bool
        let accepted: Bool?
        let reason: String?
        let applicationBefore: Bool
        let applicationAfter: Bool
        let targetUnchanged: Bool
    }

    private struct InvalidRequest: Error {}
    private struct RejectedActivation: Error { let reason: String }

    /// This entry must run before main creates NSApplication.shared. Importing
    /// AppKit and reading the NSApp global do not create an application object.
    @MainActor static func runIfRequested(_ arguments: [String]) -> Int32? {
        guard arguments.dropFirst().first == argument else { return nil }
        let receipt: Receipt
        do {
            guard arguments.count == 3 else { throw InvalidRequest() }
            receipt = perform(try Request.decode(arguments[2]))
        } catch {
            receipt = .init(version: 1, invoked: false, accepted: nil, reason: "invalid request",
                applicationBefore: NSApp != nil, applicationAfter: NSApp != nil, targetUnchanged: false)
        }
        if let data = try? JSONEncoder().encode(receipt) {
            try? FileHandle.standardOutput.write(contentsOf: data + Data([10]))
        }
        return receipt.invoked ? 0 : 1
    }

    @MainActor static func perform(_ request: Request,
        now: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        identity: (pid_t) -> NativeProcessIdentity? = NativeProcessIdentity.read,
        applicationInitialized: () -> Bool = { NSApp != nil },
        resolve: (pid_t) -> (() -> Bool)? = { pid in
            guard let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else { return nil }
            return { app.activate(options: []) }
        }) -> Receipt {
        let initialized = applicationInitialized()
        func rejected(_ reason: String) -> Receipt {
            .init(version: 1, invoked: false, accepted: nil, reason: reason,
                applicationBefore: initialized, applicationAfter: applicationInitialized(), targetUnchanged: false)
        }
        guard !initialized else { return rejected("application object already initialized") }
        guard now() < request.deadline else { return rejected("deadline exceeded") }
        guard identity(request.pid) == request.identity else { return rejected("process identity changed") }
        guard let activate = resolve(request.pid) else { return rejected("running application unavailable") }
        // Proxy creation can cross process boundaries. Recheck the original
        // kernel lifetime and deadline immediately before the one native call.
        guard !applicationInitialized(), now() < request.deadline else { return rejected("activation precondition changed") }
        guard identity(request.pid) == request.identity else { return rejected("process identity changed") }
        let accepted = activate()
        return .init(version: 1, invoked: true, accepted: accepted, reason: nil,
            applicationBefore: initialized, applicationAfter: applicationInitialized(),
            targetUnchanged: identity(request.pid) == request.identity)
    }

    /// Await only this child within the original native execution boundary.
    /// A started child without a valid receipt may already have queued an LS
    /// request; stopping it cannot undo that request or authorize a replay.
    @MainActor static func request(_ target: BackgroundTarget,
        check: @escaping @MainActor () throws -> Void,
        onCleanupUnknown: @escaping @MainActor () -> Void = {},
        executable: URL? = Bundle.main.executableURL) async throws -> Receipt {
        try check()
        guard let identity = target.capturedProcessIdentity, let executable else {
            throw DriverError("foreground activation identity or executable is unavailable", code: .staleTarget)
        }
        let deadline = RequestDiagnostics.deadline ?? (ProcessInfo.processInfo.systemUptime + 1)
        let request = Request(identity: identity, deadline: deadline)
        let text = String(decoding: try JSONEncoder().encode(request), as: UTF8.self)
        return try await run(request: request, executable: executable, arguments: [argument, text],
            check: check, onCleanupUnknown: onCleanupUnknown)
    }

    @MainActor static func run(request: Request, executable: URL, arguments: [String],
        check: @escaping @MainActor () throws -> Void,
        onCleanupUnknown: @escaping @MainActor () -> Void = {}) async throws -> Receipt {
        let child = Process(), output = Pipe()
        child.executableURL = executable; child.arguments = arguments
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = output; child.standardError = FileHandle.nullDevice
        try check()
        // A failed spawn proves this adapter made no activation request.
        do { try child.run() }
        catch { throw DriverError("could not start foreground activation helper", code: .inputUnavailable) }
        diagnose(["kind": "foreground_activation_helper", "phase": "launched", "parentPID": getpid(),
            "childPID": child.processIdentifier, "targetPID": request.pid, "deadline": request.deadline])
        try? output.fileHandleForWriting.close()
        defer { try? output.fileHandleForReading.close() }
        do {
            while child.isRunning {
                try check()
                guard ProcessInfo.processInfo.systemUptime < request.deadline else { throw RequestDeadlineExceeded() }
                try await Task.sleep(for: .milliseconds(10))
            }
            try check()
            let descriptor = output.fileHandleForReading.fileDescriptor
            guard fcntl(descriptor, F_SETFL, O_NONBLOCK) == 0 else { throw InvalidRequest() }
            var bytes = [UInt8](repeating: 0, count: 4097)
            let count = bytes.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress, $0.count) }
            guard count > 0, count <= 4096 else { throw InvalidRequest() }
            let receipt = try JSONDecoder().decode(Receipt.self, from: Data(bytes.prefix(count)))
            guard receipt.version == 1 else { throw InvalidRequest() }
            diagnose(["kind": "foreground_activation_helper", "phase": "receipt", "parentPID": getpid(),
                "childPID": child.processIdentifier, "targetPID": request.pid, "deadline": request.deadline,
                "applicationBefore": receipt.applicationBefore, "applicationAfter": receipt.applicationAfter,
                "invoked": receipt.invoked, "accepted": receipt.accepted as Any? ?? NSNull()])
            if !receipt.invoked {
                guard receipt.accepted == nil, let reason = receipt.reason, !reason.isEmpty else { throw InvalidRequest() }
                throw RejectedActivation(reason: reason)
            }
            guard !receipt.applicationBefore, !receipt.applicationAfter, receipt.accepted != nil,
                  receipt.reason == nil, receipt.targetUnchanged else { throw InvalidRequest() }
            // accepted=false is still a returned native request. Only the
            // parent's current foreground/window readiness decides the effect.
            return receipt
        } catch {
            if !(await stop(child)) { onCleanupUnknown() }
            if error is CancellationError || error is RequestDeadlineExceeded { throw error }
            if let rejected = error as? RejectedActivation {
                throw DriverError("foreground activation helper did not request activation: \(rejected.reason)",
                    code: rejected.reason == "process identity changed" ? .staleTarget : .inputUnavailable)
            }
            // Even an unexpected exit or lost output can follow a native call.
            // Keep it out of the preparationUnavailable retry classification.
            throw DriverError("foreground activation result is unavailable; activation may have occurred", code: .executionUnknown,
                effect: .possible, next: "observe the original application before continuing")
        }
    }

    @MainActor private static func stop(_ child: Process) async -> Bool {
        guard child.isRunning else { return true }
        child.terminate()
        for _ in 0..<10 {
            if !child.isRunning { return true }
            await cleanupPause()
        }
        if child.isRunning { _ = Darwin.kill(child.processIdentifier, SIGKILL) }
        for _ in 0..<10 {
            if !child.isRunning { return true }
            await cleanupPause()
        }
        return !child.isRunning
    }

    /// Cleanup must finish even when the owning Task is already cancelled.
    private static func cleanupPause() async {
        await withCheckedContinuation { continuation in
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(10)) { continuation.resume() }
        }
    }
}
