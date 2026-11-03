import AppKit
import ApplicationServices

/// Addressed text-value replacement. This unit does not find an editor, take
/// focus, synthesize keys, or retry through another input mechanism.
extension NativeElementControl {
    struct TextValueContract: Equatable {
        let role: String
        let subrole: String?
    }

    struct TextValueFacts {
        let contract: TextValueContract?
        let enabled: Bool?
        let foreground: Bool
    }

    func nativeTextValueFacts(_ ref: String) throws -> TextValueFacts {
        guard let target = registry.target(ref) else { throw DriverError("element reference expired", code: .staleTarget) }
        let background = BackgroundTarget(target.application)
        try background.validate()
        try inspection.validateTargetIdentity(target)
        let role = inspection.attribute(target.element, kAXRoleAttribute) as? String
        let subroleRead = AXAccess.read(target.element, kAXSubroleAttribute)
        let subrole: String?
        let subroleKnown: Bool
        switch subroleRead {
        case .value(let value): subrole = value as? String; subroleKnown = subrole != nil
        case .unsupported, .unavailable(.noValue): subrole = nil; subroleKnown = true
        default: subrole = nil; subroleKnown = false
        }
        var writable = DarwinBoolean(false)
        let settable = AXUIElementIsAttributeSettable(target.element, kAXValueAttribute as CFString, &writable)
        // A text field owns its value even while a different field is editing.
        // A standalone text area may instead be a shared field editor. It needs
        // the separate contextual-edit unit, not this independent-value contract.
        // Search fields require editing callbacks; a bare value assignment is
        // not sufficient evidence of that stronger semantic contract.
        let independent = subroleKnown && [kAXTextFieldRole, kAXComboBoxRole].contains(role ?? "")
            && subrole != "AXSearchField" && subrole != "AXSecureTextField"
            && settable == .success && writable.boolValue
        return TextValueFacts(contract: independent ? TextValueContract(role: role!, subrole: subrole) : nil,
            enabled: (inspection.attribute(target.element, kAXEnabledAttribute) as? NSNumber)?.boolValue,
            foreground: background.isForeground)
    }

    func executeNativeTextValue(_ ref: String, contract: TextValueContract, text: String,
                                onDispatch: @MainActor () throws -> Void) throws -> ActionResult {
        try validateText(text)
        let target = try inputTarget(ref)
        guard try nativeTextValueFacts(ref).contract == contract else {
            throw DriverError("the selected text-value replacement contract is no longer available", code: .inputUnavailable)
        }
        try inspection.requireEnabled(target.element)
        try inspection.validateTargetIdentity(target)
        try BackgroundTarget(target.application).validate(input: true)
        try RequestDiagnostics.checkCancellation()
        try onDispatch()
        let status = AXMessaging.setAttributeValue(target.element, kAXValueAttribute as CFString, text as CFString)
        guard status == .success else {
            throw DriverError("native text replacement returned AX error \(status.rawValue); effect unknown, not retried",
                              code: .executionUnknown, effect: .possible)
        }
        // Post-edit observation cannot erase a completed dispatch. Formatting,
        // focus loss, an unreadable value, or a destroyed editor is not a reason
        // to send the same text again. Verification is local, never 'saved'.
        do {
            try RequestDiagnostics.checkCancellation()
            try inspection.validateTargetIdentity(target)
            if let actual = inspection.attribute(target.element, kAXValueAttribute) as? String, actual == text {
                return ActionResult(verified: true, detail: "Target text value verified; submission was not requested.")
            }
        } catch { /* Preserve dispatch; the application may have changed its UI. */ }
        return ActionResult(verified: false, detail: "Target text-value replacement dispatched; resulting content is unconfirmed.")
    }
}
