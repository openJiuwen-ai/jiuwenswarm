import Foundation

/// Explicit capture scopes; a session discovery binding never becomes a
/// window or desktop input target by interpreting missing identities.
struct NativeCaptureRequest {
    enum Kind: String { case window, desktop }
    let operation: String
    let target: [String: Any]
    let kind: Kind
    let maxDimension: Int

    init(_ raw: Any?) throws {
        let p = try object(raw, keys: ["operation", "target", "kind", "maxDimension"])
        operation = try string(p["operation"], label: "operation")
        guard let kind = Kind(rawValue: try string(p["kind"], label: "capture.kind")) else {
            throw DriverError("unsupported capture kind", code: .invalidRequest)
        }
        self.kind = kind
        target = try NativeTarget(p["target"]).wire
        guard target["node"] is NSNull, target["capture"] is NSNull else {
            throw DriverError("capture needs a read scope, not an element or old image", code: .invalidRequest)
        }
        for key in ["instance", "rootWindow", "surface"] {
            if kind == .desktop {
                guard target[key] is NSNull else {
                    throw DriverError("desktop capture needs a session read scope", code: .invalidRequest)
                }
            } else { _ = try string(target[key], label: key) }
        }
        let dimension = try number(p["maxDimension"], label: "maxDimension")
        guard (1...4096).contains(dimension), dimension.rounded() == dimension else {
            throw DriverError("capture maxDimension must be an integer in 1...4096", code: .invalidRequest)
        }
        maxDimension = Int(dimension)
    }
}
