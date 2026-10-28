import Foundation

/// Request-scoped diagnostics, not observations or action receipts. Timings are
/// inclusive and may overlap; summing phases does not give total request time.
@MainActor final class NativeMetrics {
    enum Phase: String {
        case windowPreparation, keyboardPreparation, focusPreparation, editorResolution
        case targetValidation, documentValidation, recipientValidation, focusValidation, pointerValidation
        case preparedInputValidation, pointerFocusValidation, windowMembership, windowValidation, keyboardIntentCapture
        case observationWait, accessibilityRead, windowCapture, overviewCapture, capabilityEvaluation
    }
    enum Counter: String {
        case alreadyForeground, cachedWindowPreparation, nativeWindowSelection, activationNotification
        case observationRetry
        case editorForwardOnlyLink, editorHandoff, collectionEditorHandoff, capabilityQuery
    }
    struct Interval {
        let owner: NativeMetrics
        let phase: Phase
        let started: TimeInterval
        @MainActor func end() { owner.record(phase, since: started) }
    }
    private let clock: () -> TimeInterval
    private let started: TimeInterval
    private var closed = false
    private var phases = [Phase: (count: Int, durationMs: Double)]()
    private var counters = [Counter: Int]()

    init(clock: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.clock = clock
        self.started = clock()
    }
    func start(_ phase: Phase) -> Interval? {
        closed ? nil : Interval(owner: self, phase: phase, started: clock())
    }
    func count(_ counter: Counter) {
        if !closed { counters[counter, default: 0] += 1 }
    }
    private func record(_ phase: Phase, since start: TimeInterval) {
        guard !closed else { return }
        var value = phases[phase] ?? (0, 0)
        value.count += 1
        value.durationMs += max(0, clock() - start) * 1000
        phases[phase] = value
    }
    func finish(method: String, status: String) -> [String: Any]? {
        guard !closed else { return nil }
        closed = true
        return ["kind": "native_metrics", "version": 1, "method": method, "status": status,
                "durationMs": max(0, clock() - started) * 1000,
                "phases": Dictionary(uniqueKeysWithValues: phases.map { ($0.key.rawValue,
                    ["count": $0.value.count, "durationMs": $0.value.durationMs] as [String: Any]) }),
                "counters": Dictionary(uniqueKeysWithValues: counters.map { ($0.key.rawValue, $0.value) })]
    }
}
