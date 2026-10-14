import Foundation

/// One request owns the deadline. Optional observation work leaves a small
/// bounded reserve for identity checks and encoding; stages never refill it.
struct ObservationBudget: Sendable {
    let deadline: TimeInterval
    let collectionDeadline: TimeInterval
    let clock: @Sendable () -> TimeInterval

    init(deadline: TimeInterval, startedAt: TimeInterval,
         clock: @escaping @Sendable () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.deadline = deadline
        self.collectionDeadline = deadline - min(0.5, max(0, deadline - startedAt) * 0.1)
        self.clock = clock
    }

    func collectionLimit(maximumDuration: TimeInterval) -> TimeInterval {
        min(collectionDeadline, clock() + maximumDuration)
    }

    func canCollect(until phaseDeadline: TimeInterval? = nil, cancelled: Bool = false) -> Bool {
        !cancelled && clock() < min(collectionDeadline, phaseDeadline ?? collectionDeadline)
    }
}

struct ObservationBudgetExceeded: Error, CustomStringConvertible {
    let description = "observation time budget exhausted; optional collection omitted"
}
