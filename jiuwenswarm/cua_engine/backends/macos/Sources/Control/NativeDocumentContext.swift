import AppKit
import ApplicationServices

/// Exact standard-document context; never focuses an editor or sends a gesture.
@MainActor struct NativeDocumentContext {
    enum Focus { case target, other, unavailable }
    let application: NSRunningApplication
    let window: AXUIElement

    static func permitsDispatch(_ focus: Focus, prepared: Bool) -> Bool {
        focus == .target || (!prepared && focus == .unavailable)
    }

    func focus() throws -> Focus {
        try BackgroundTarget(application).validate(input: true)
        let root = AXUIElementCreateApplication(application.processIdentifier)
        AXUIElementSetMessagingTimeout(root, 0.1)
        guard let current = AXAccess.element(root, kAXFocusedWindowAttribute) else { return .unavailable }
        if CFEqual(current, window) { return .target }
        guard let expected = BackgroundEvents.nativeWindowID(window),
              let actual = BackgroundEvents.nativeWindowID(current) else { return .unavailable }
        return actual == expected ? .target : .other
    }

    func select(requireSelection: Bool = false, check: () throws -> Void, onPreparation: (String) -> Void) throws {
        let root = AXUIElementCreateApplication(application.processIdentifier)
        var selected = false
        for (object, attribute, value): (AXUIElement, String, CFTypeRef) in [
            (window, kAXMainAttribute, kCFBooleanTrue),
            (window, kAXFocusedAttribute, kCFBooleanTrue),
            (root, kAXFocusedWindowAttribute, window)
        ] {
            if try focus() == .target, !requireSelection || selected { return }
            guard AXAccess.writable(object, attribute).value == true else { continue }
            try check()
            try BackgroundTarget(application).validate(input: true)
            let detail = "native document context " + attribute
            onPreparation(detail)
            let status = AXMessaging.setAttributeValue(object, attribute as CFString, value)
            selected = true
            if status == .invalidUIElement { throw DriverError("action document disappeared", code: .staleTarget) }
            try check()
        }
    }
}
