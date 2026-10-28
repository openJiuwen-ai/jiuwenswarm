import Foundation

/// One operation owns its downs and releases. The delivery closure captures
/// the original route, window, process incarnation and event source.
@MainActor final class PairedInput<Event> {
    private let source: AnyObject?
    private let post: (Event) -> Void
    private let canRelease: () -> Bool
    private let skipped: () -> Void
    private let settle: () async -> Void
    private let settleUnpaired: Bool
    private var held: [Event] = []
    private var released = false

    init(source: AnyObject? = nil, post: @escaping (Event) -> Void, canRelease: @escaping () -> Bool,
         skipped: @escaping () -> Void = {},
         settleUnpaired: Bool = false,
         settle: @escaping () async -> Void = PairedInput.settleDelivery) {
        self.source = source
        self.post = post; self.canRelease = canRelease; self.skipped = skipped; self.settle = settle
        self.settleUnpaired = settleUnpaired
    }

    func press(_ event: Event, release: Event) {
        held.append(release)
        post(event)
    }

    func release() {
        let pending = held.reversed()
        held.removeAll()
        guard !pending.isEmpty else { return }
        // Ordinary focus/window validation may now fail; release only requires
        // the original delivery process. Never release into a recycled PID.
        guard canRelease() else { skipped(); return }
        for event in pending { post(event) }
        released = true
    }

    func run<Value>(_ operation: () async throws -> Value) async throws -> Value {
        do {
            try Task.checkCancellation()
            let result = try await operation()
            await finish()
            return result
        } catch {
            await finish()
            throw error
        }
    }

    private func finish() async {
        release()
        // Event posting is asynchronous. Cancellation used to skip the normal
        // post-release interval and let shutdown discard a queued mouse-up.
        // Keep that same bounded interval on both paths, inside the input lease.
        // This is delivery settling, not proof of an application's outcome.
        if released || settleUnpaired { released = false; await settle() }
    }

    private static func settleDelivery() async {
        // Task.sleep would immediately throw in a cancelled operation. A timer
        // keeps cleanup alive through graceful EOF/signal/deadline shutdown.
        await withCheckedContinuation { continuation in
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(30)) { continuation.resume() }
        }
    }
}
