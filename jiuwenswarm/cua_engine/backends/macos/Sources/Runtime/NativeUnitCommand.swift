import Foundation

/// The write envelope references a contract already bound by inspect. Live
/// receiver/selection/geometry checks belong to the selected native unit, not
/// a client-supplied copy of InputContext. Shared with the wire contract tests.
struct NativeUnitCommand {
    let value: [String: Any]
    let kind: String
    let primitiveID: String
    let target: [String: Any]
    let foreground: Bool

    init(_ raw: Any?) throws {
        value = try object(raw, keys: ["kind", "primitiveId", "target", "foreground"])
        kind = try string(value["kind"], label: "kind")
        guard ["native_action", "native_text_edit", "key_sequence", "clipboard_paste", "pointer_sequence",
               "native_scroll", "prepare_receiver", "launch_application"].contains(kind) else {
            throw DriverError("unknown native unit", code: .invalidRequest)
        }
        primitiveID = try string(value["primitiveId"], label: "primitiveId")
        let launch = kind == "launch_application"
        target = try nativeOperationTarget(value["target"], installation: launch)
        foreground = try boolean(value["foreground"], label: "foreground")
        guard !launch || !foreground else {
            throw DriverError("application launch requires only its observed installation contract", code: .invalidRequest)
        }
    }
}
