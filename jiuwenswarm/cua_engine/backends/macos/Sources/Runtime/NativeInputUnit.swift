import Foundation

/// Closed set of native contracts. Only declaration formatting lives here;
/// readiness sampling and execution stay with the selected route.
enum NativeInputUnit: Equatable {
    case action(String, String?)
    case menuAction(String, String?)
    case textValue(NativeElementControl.TextValueContract)
    case selectedText(NativeSelectedTextUnit.Contract)
    case receiverContext(NativeReceiverPreparation.Contract)
    case keyboard(NativeKeyboardIntent.Kind, NativeReplacementUnit.Selection? = nil)
    case pointer(NativePointerIntent.Kind)
    case desktop(String)
    case scroll
    case launch(NativeInstallation)

    func declaration(id: String, supported: Bool, readiness: NativeReadiness, exclusive: Bool) -> [String: Any] {
        var wire: [String: Any] = ["id": id, "channel": "object", "supported": supported ? "yes" : "no",
            "readiness": readiness.state, "pending": readiness.pending, "backgroundPreparation": "no"]
        switch self {
        case .launch: wire["unit"] = "launch_application"
        case .action(let name, let description), .menuAction(let name, let description):
            wire["unit"] = "native_action"
            var action = ["name": name]
            if let description { action["description"] = description }
            wire["action"] = action
            if case .menuAction = self {
                wire["backgroundPreparation"] = readiness.pending.contains("receiver") ? "yes" : "no"
            }
        case .textValue: wire["unit"] = "native_text_edit"
        case .selectedText, .receiverContext:
            if case .selectedText = self { wire["unit"] = "native_text_edit" }
            else { wire["unit"] = "prepare_receiver" }
            wire["channel"] = "editor"; wire["backgroundPreparation"] = "yes"
        case .scroll: wire["unit"] = "native_scroll"
        case .keyboard(let kind, _):
            wire["unit"] = kind.unit; wire["channel"] = "keyboard"; wire["backgroundPreparation"] = "yes"
        case .pointer:
            wire["unit"] = "pointer_sequence"; wire["channel"] = "pointer"
            wire["backgroundPreparation"] = exclusive ? "no" : "yes"
        case .desktop(let intent):
            let pointer = NativePointerIntent.Kind(rawValue: intent) != nil
            wire["unit"] = pointer ? "pointer_sequence" : "key_sequence"
            wire["channel"] = pointer ? "pointer" : "keyboard"
        }
        return wire
    }
}
