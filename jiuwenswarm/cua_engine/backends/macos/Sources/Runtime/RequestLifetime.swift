import Foundation

/// Own one native operation so disconnects cancel it before process shutdown.
/// The operation's defer blocks run before the returned task finishes.
@MainActor final class RequestLifetime {
    private var active: Task<Void, Never>?
    private var deadline: Task<Void, Never>?
    private(set) var stopping = false

    func start(timeoutMs: Int? = nil, onTimeout: (@MainActor () -> Void)? = nil, _ operation: @escaping @MainActor () async -> Void) -> Bool {
        guard !stopping, active == nil else { return false }
        active = Task { @MainActor in
            defer { self.deadline?.cancel(); self.deadline = nil; self.active = nil }
            guard !self.stopping else { return }
            await operation()
        }
        if let timeoutMs {
            deadline = Task { @MainActor in
                do { try await Task.sleep(for: .milliseconds(timeoutMs)) } catch { return }
                onTimeout?()
                self.active?.cancel()
            }
        }
        return true
    }

    /// Stops accepting work synchronously. Callers await the task before exit.
    func cancel() -> Task<Void, Never>? {
        stopping = true
        deadline?.cancel()
        active?.cancel()
        return active
    }

    /// Cancel one operation without admitting a replacement until cleanup ends.
    func cancelActive() { active?.cancel() }
}
