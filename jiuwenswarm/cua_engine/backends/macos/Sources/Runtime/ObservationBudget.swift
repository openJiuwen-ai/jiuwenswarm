import Foundation

/// One request owns the deadline. Optional observation work leaves a small
/// bounded reserve for identity checks and encoding; stages never refill it.
struct ObservationBudget: Sendable {
    let collectionDeadline: TimeInterval
    let clock: @Sendable () -> TimeInterval

    init(deadline: TimeInterval, startedAt: TimeInterval,
         clock: @escaping @Sendable () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.collectionDeadline = deadline - min(0.5, max(0, deadline - startedAt) * 0.1)
        self.clock = clock
    }

    func canCollect() -> Bool {
        clock() < collectionDeadline
    }
}

struct ObservationBudgetExceeded: Error, CustomStringConvertible {
    let description = "observation time budget exhausted; optional collection omitted"
}
