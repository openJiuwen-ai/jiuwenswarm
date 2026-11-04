import AppKit
import ApplicationServices

/// Establish AX context on an already selected object. This is preparation,
/// not a declaration that text can be edited or keyboard events delivered.
@MainActor struct NativeReceiverPreparation {
    struct Contract: Equatable { let role: String }
    let application: NSRunningApplication
    let window: AXUIElement
    let element: AXUIElement?
    var receiverRelation: NativeReceiverRelation? = nil

    func contract() -> Contract? {
        guard let element, let role = AXAccess.read(element, kAXRoleAttribute).value as? String, !role.isEmpty,
              AXAccess.writable(element, kAXFocusedAttribute).value == true else { return nil }
        return Contract(role: role)
    }

    func readiness() throws -> NativeReadiness {
        NativeReadiness([("receiver", try NativeReceiverSnapshot.read(application: application, window: window, receiverRelation: receiverRelation).receiver(element))])
    }

    func validate(_ expected: Contract, check: @MainActor () throws -> Void) throws {
        try check()
        guard let element, AXAccess.read(element, kAXRoleAttribute).value as? String == expected.role,
              AXAccess.read(element, kAXEnabledAttribute).value as? Bool != false else {
            throw DriverError("the selected receiver preparation contract changed", code: .inputUnavailable)
        }
    }

    /// The caller supplies its route-specific readiness and boundary guard.
    /// Selection editing and keyboard delivery must not share a readiness flag.
    func prepare(foreground: Bool, check: @escaping @MainActor () throws -> Void,
                 ready: @escaping @MainActor () throws -> Bool,
                 onPreparation: @escaping @MainActor (String) -> Void) async throws {
        func validate() throws {
            try check()
            try BackgroundTarget(application).validate(input: true)
            try RequestDiagnostics.checkCancellation()
        }
        try validate()
        try BackgroundTarget.validateReleasedInput()
        if try ready() { return }
        if foreground {
            let mapping = try NativeWindowUnit(application: application, window: window).mapping()
            try await ForegroundAssist().prepare(BackgroundTarget(application), window: mapping.id,
                onAction: { onPreparation("foreground window activation") })
        }
        let root = AXUIElementCreateApplication(application.processIdentifier)
        if AXAccess.element(root, kAXFocusedWindowAttribute).map({ CFEqual($0, window) }) != true,
           AXAccess.writable(window, kAXFocusedAttribute).value == true {
            try validate()
            onPreparation("native window focus")
            _ = AXMessaging.setAttributeValue(window, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        }
        if let element, try NativeReceiverSnapshot.read(application: application, window: window, receiverRelation: receiverRelation).receiver(element) != "yes" {
            guard AXAccess.writable(element, kAXFocusedAttribute).value == true else {
                throw NativePreparationUnavailable(description: "the selected receiver does not provide direct native focus")
            }
            try validate()
            onPreparation("native element focus")
            _ = AXMessaging.setAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        }
        for attempt in 0..<12 {
            try validate()
            if try ready() { return }
            if attempt < 11 { try await Task.sleep(for: .milliseconds(25)) }
        }
        throw NativePreparationUnavailable(description: "native receiver context is not ready; no requested input was sent")
    }
}
