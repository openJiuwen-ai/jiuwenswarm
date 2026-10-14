import Foundation

/// A window identity/geometry changed during collection. Input commands still
/// surface this error immediately; only a new read may collect a new snapshot.
struct ObservationChanged: ClassifiedFailure, CustomStringConvertible {
    let code = FailureCode.staleTarget
    let description: String
    init(_ message: String) { description = message }
}
