import Foundation

/// The caller's complete, immutable intent belongs to the operation. A later
/// step selects a native contract; it cannot replace the payload or the intent.
/// Unit-specific parameter validation still runs before any preparation/input.
struct NativeIntent {
    let kind: String
    let parameters: [String: Any]

    init(_ raw: Any?, mode: String) throws {
        let value = try object(raw, keys: ["kind", "parameters"])
        kind = try string(value["kind"], label: "intent.kind")
        guard let parameters = value["parameters"] as? [String: Any],
              JSONSerialization.isValidJSONObject(parameters),
              (mode == "read" && kind == "read") || (mode == "write" &&
                ["click", "focus", "insert", "replace", "paste", "key", "openMenu", "action", "scroll", "drag", "launch"].contains(kind)),
              !["read", "launch", "focus"].contains(kind) || parameters.isEmpty else {
            throw DriverError("invalid operation intent or mode", code: .invalidRequest)
        }
        self.parameters = parameters
    }
}

struct NativeBeginRequest {
    let value: [String: Any]
    let mode: String
    let intent: NativeIntent

    init(_ raw: Any?) throws {
        value = try object(raw, keys: ["mode", "target", "intent"])
        mode = try string(value["mode"], label: "mode")
        intent = try NativeIntent(value["intent"], mode: mode)
        _ = try nativeOperationTarget(value["target"], installation: intent.kind == "launch")
    }
}

func nativeOperationTarget(_ raw: Any?, installation: Bool) throws -> [String: Any] {
    guard installation else { return try NativeTarget(raw).wire }
    let target = try object(raw, keys: ["session", "application"])
    _ = try string(target["session"], label: "session")
    _ = try string(target["application"], label: "application")
    return target
}
