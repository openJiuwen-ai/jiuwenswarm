import Foundation

private final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var time: TimeInterval
    init(_ time: TimeInterval) { self.time = time }
    func read() -> TimeInterval { lock.withLock { time } }
    func set(_ value: TimeInterval) { lock.withLock { time = value } }
}

@main struct ObservationBudgetTests {
    @MainActor static func main() async throws {
        // Lease acquisition and screenshot work have already consumed 3 s of
        // this 5 s request when AX collection starts. They share one deadline.
        let clock = TestClock(0)
        let request = ObservationBudget(deadline: 5, startedAt: 0, clock: { clock.read() })
        clock.set(3)
        let axDeadline = request.collectionDeadline
        assert(axDeadline == 4.5)
        assert(axDeadline - clock.read() == 1.5)
        assert(request.canCollect())

        // Retrying a capture or starting another collection phase never gives
        // the request a new 5 s budget after the preceding attempt's work.
        for time in [3.2, 4.0, 4.49] {
            clock.set(time)
            assert(request.collectionDeadline == axDeadline)
            assert(request.canCollect())
        }
        clock.set(4.5)
        assert(!request.canCollect())
        assert(5 - clock.read() == 0.5,
               "collection stops while identity validation and publication retain 500 ms")
        clock.set(4.9)
        assert(!request.canCollect() && clock.read() < 5)
        clock.set(5)
        assert(!request.canCollect() && clock.read() == 5)

        // Short requests reserve 10%, rather than losing a fixed half second.
        let shortClock = TestClock(20)
        let shortRequest = ObservationBudget(deadline: 22, startedAt: 20, clock: { shortClock.read() })
        assert(abs(shortRequest.collectionDeadline - 21.8) < 0.000_001)
        assert(abs(22 - shortRequest.collectionDeadline - 0.2) < 0.000_001)
        shortClock.set(shortRequest.collectionDeadline)
        assert(!shortRequest.canCollect())
        assert(shortClock.read() < 22)

        let liveDeadline = ProcessInfo.processInfo.systemUptime + 60
        try RequestDiagnostics.$deadline.withValue(liveDeadline) {
            try RequestDiagnostics.$observationBudget.withValue(shortRequest) {
                do {
                    try RequestDiagnostics.checkObservationBudget()
                    preconditionFailure("expired optional collection must stop")
                } catch { assert(error is ObservationBudgetExceeded) }
                // The caller still has time for required identity checks and
                // can finalize previously collected observations normally.
                try RequestDiagnostics.checkCancellation()
            }
        }

        struct ProviderFailure: Error {}
        RequestDiagnostics.$deadline.withValue(ProcessInfo.processInfo.systemUptime - 1) {
            do {
                try RequestDiagnostics.rethrowCancellation(ProviderFailure())
                preconditionFailure("a late provider failure must not start a fallback after D")
            } catch { assert(error is RequestDeadlineExceeded) }
        }
        try RequestDiagnostics.$deadline.withValue(liveDeadline) {
            try RequestDiagnostics.rethrowCancellation(ProviderFailure())
            do {
                try RequestDiagnostics.rethrowCancellation(CancellationError())
                preconditionFailure("explicit cancellation must propagate")
            } catch { assert(error is CancellationError) }
            do {
                try RequestDiagnostics.rethrowCancellation(RequestDeadlineExceeded())
                preconditionFailure("an existing deadline error must propagate")
            } catch { assert(error is RequestDeadlineExceeded) }
        }

        await captureBoundary(cancel: true)
        await captureBoundary(cancel: false)
        print("PASS shared observation budget, prior-stage consumption, retries, TaskLocal boundaries, cancellation cleanup and finalization reserve")
    }

    @MainActor private static func captureBoundary(cancel: Bool) async {
        let clock = TestClock(0)
        let budget = ObservationBudget(deadline: 5, startedAt: 0, clock: { clock.read() })
        let deadline = ProcessInfo.processInfo.systemUptime + 60
        await RequestDiagnostics.$deadline.withValue(deadline) {
            await RequestDiagnostics.$observationBudget.withValue(budget) {
                var provider: CheckedContinuation<Void, Never>?
                var stages = [String]()
                var published: String?
                let capture = Task { @MainActor in
                    defer { stages.append("cleanup") }
                    assert(RequestDiagnostics.deadline == deadline)
                    assert(RequestDiagnostics.observationBudget?.collectionDeadline == 4.5)
                    do {
                        try RequestDiagnostics.checkObservationBudget()
                        stages.append("capture.await")
                        // A system provider may return normally after cancellation.
                        // Hold its return explicitly instead of depending on sleeps.
                        await withCheckedContinuation { provider = $0 }
                        stages.append("capture.return")
                        try RequestDiagnostics.checkObservationBudget()
                        stages.append("AX")
                        stages.append("overview")
                        published = "late window mapping"
                    } catch {
                        try RequestDiagnostics.rethrowCancellation(error)
                        if error is ObservationBudgetExceeded { throw error }
                        stages.append("fallback")
                    }
                }
                while provider == nil { await Task.yield() }
                if cancel { capture.cancel() } else { clock.set(4.5) }
                provider!.resume()
                do {
                    try await capture.value
                    preconditionFailure("capture boundary must stop late work")
                } catch {
                    if cancel { assert(error is CancellationError) }
                    else { assert(error is ObservationBudgetExceeded) }
                }
                assert(stages == ["capture.await", "capture.return", "cleanup"],
                       "no AX collection, overview or fallback may start after cancellation or Dc")
                assert(published == nil, "late captures must not publish shared mappings")
                assert(!Task.isCancelled, "cancelling one request does not cancel its parent")
            }
        }
    }
}
