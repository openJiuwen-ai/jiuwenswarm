import Foundation

enum FailureCode: String {
    case staleTarget = "stale_target", snapshotExpired = "snapshot_expired", ambiguousTarget = "ambiguous_target"
    case elementNotFound = "element_not_found", observationIncomplete = "observation_incomplete"
    case blockedByModal = "blocked_by_modal", inputUnavailable = "input_unavailable", modeUnsupported = "mode_unsupported"
    case desktopUnavailable = "desktop_unavailable", invalidRequest = "invalid_request", executionFailed = "execution_failed"
    case executionUnknown = "execution_unknown"

    var next: String {
        switch self {
        case .elementNotFound: return "read the same window and correct the observed path; no input was sent by this lookup"
        case .observationIncomplete: return "inspect the relevant subtree or screenshot; narrow the path instead of assuming the target is absent or unique"
        case .staleTarget: return "read the intended window again and choose a current target"
        case .snapshotExpired: return "read the element again to capture a new text snapshot"
        case .ambiguousTarget: return "inspect the listed candidates and select the intended application, window or element"
        case .blockedByModal: return "read the application and handle its blocking dialog before continuing"
        case .inputUnavailable: return "read the target and inspect available controls or its screenshot before continuing"
        case .modeUnsupported: return "use operations supported by the configured desktop mode; do not switch modes automatically"
        case .desktopUnavailable: return "resume an available desktop session and check its required permissions"
        case .invalidRequest: return "correct the request using the documented API and observed targets"
        case .executionFailed, .executionUnknown: return "read the target before continuing; do not replay input automatically"
        }
    }
}

enum FailureEffect: String {
    case none, possible, partial

    func including(_ other: FailureEffect) -> FailureEffect {
        if self == .partial || other == .partial { return .partial }
        if self == .possible || other == .possible { return .possible }
        return .none
    }
}

protocol ClassifiedFailure: Error { var code: FailureCode { get } }

/// A selected route could not be prepared before requested input. This marker
/// is deliberately narrower than inputUnavailable: boundary/focus continuity
/// failures must not grant permission to activate another receiver or replay.
struct NativePreparationUnavailable: ClassifiedFailure, CustomStringConvertible {
    let description: String
    var code: FailureCode { .inputUnavailable }
}

struct DriverError: ClassifiedFailure, CustomStringConvertible, LocalizedError {
    let description: String
    let code: FailureCode
    let effect: FailureEffect?
    let next: String

    // Observation gaps sometimes pass through Foundation's Error bridge.
    // Preserve the same classified cause instead of a generic NSError message.
    var errorDescription: String? { description }

    init(_ message: String, code: FailureCode = .executionFailed, effect: FailureEffect? = nil, next: String? = nil) {
        description = message; self.code = code; self.effect = effect; self.next = next ?? code.next
    }

}
