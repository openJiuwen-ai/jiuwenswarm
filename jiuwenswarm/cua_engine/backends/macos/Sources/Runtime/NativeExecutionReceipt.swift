import Foundation

/// Per-step evidence and wire formatting. The executor owns dispatch barriers,
/// live validation and retry authorization; recording a preparation is not input.
@MainActor final class NativeExecutionReceipt {
    let target: [String: Any]
    var dispatch = "notDispatched"
    var detail: String
    var confirmation: String
    var retryReason: String?
    var context: [String: Any]?
    var application: [String: Any]?
    private(set) var errorCode: FailureCode?
    private(set) var acceptedCount = 0
    private(set) var preparation = [String]()
    private(set) var cleanup = "complete"
    private var clipboard: String?
    private let onEffect: () -> Void

    init(target: [String: Any], detail: String = "Native action dispatched; application effect unconfirmed.",
         confirmation: String = "unavailable", onEffect: @escaping () -> Void) {
        self.target = target; self.detail = detail; self.confirmation = confirmation; self.onEffect = onEffect
    }

    func recordPreparation(_ change: String) { preparation.append(change); onEffect() }
    func recordAccepted() { acceptedCount += 1 }
    func recordCleanupUnknown() { cleanup = "unknown" }

    func recordClipboardRestore(_ outcome: PasteboardTransaction.RestoreResult) {
        switch outcome {
        case .restored: clipboard = "Original clipboard contents restored."
        case .ownershipLost: clipboard = "A later or unconfirmed clipboard owner was preserved."
        case .failed:
            clipboard = "Clipboard restoration failed."
            if cleanup == "complete" { cleanup = "failed" }
        }
    }

    func recordFailure(_ error: Error) {
        errorCode = (error as? ClassifiedFailure)?.code ?? .executionFailed
        detail = String(describing: error)
        confirmation = "notRequested"
        if acceptedCount > 0 { dispatch = "partial" }
    }

    var succeeded: Bool { errorCode == nil && cleanup == "complete" }
    var wire: [String: Any] {
        var value: [String: Any] = ["target": target, "dispatch": dispatch,
            "detail": clipboard.map { "\(detail) \($0)" } ?? detail,
            "preparation": preparation, "confirmation": confirmation, "cleanup": cleanup,
            "errorCode": errorCode?.rawValue as Any? ?? NSNull(),
            "retryReason": (cleanup == "complete" ? retryReason : nil) as Any? ?? NSNull(),
            "context": context as Any? ?? NSNull()]
        if let application { value["application"] = application }
        return value
    }
}
