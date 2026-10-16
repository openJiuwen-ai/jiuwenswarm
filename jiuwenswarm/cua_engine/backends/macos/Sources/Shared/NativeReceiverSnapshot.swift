import AppKit
import ApplicationServices

/// Current AX receiver in one fixed window. This establishes object/editor
/// context only; posting keyboard events additionally needs a delivery route.
@MainActor struct NativeReceiverSnapshot {
    let windowRoute: String
    let focus: AXUIElement?
    let ancestors: [AXUIElement]
    let foreground: Bool
    var relatedEditor: AXUIElement? = nil
    var window: AXUIElement? = nil

    func receives(_ element: AXUIElement?) -> Bool { receiver(element) == "yes" }

    func receiver(_ element: AXUIElement?) -> String {
        guard windowRoute == "yes" else { return windowRoute }
        guard let element else { return "yes" }
        if let window, CFEqual(window, element) { return "yes" }
        guard let focus else { return "unknown" }
        if let relatedEditor, CFEqual(relatedEditor, element) { return "yes" }
        // Parentage is observation, not authority to send input to an
        // independent nested control. Python must select any related focus.
        return CFEqual(focus, element) ? "yes" : "no"
    }

    static func read(application: NSRunningApplication, window: AXUIElement,
                     receiverRelation: NativeReceiverRelation? = nil) throws -> Self {
        let background = BackgroundTarget(application)
        try background.validate(input: true)
        try RequestDiagnostics.checkCancellation()
        let root = AXUIElementCreateApplication(application.processIdentifier)
        AXUIElementSetMessagingTimeout(root, 0.15)
        let focusedWindow = AXAccess.element(root, kAXFocusedWindowAttribute)
        let focused = AXAccess.element(root, kAXFocusedUIElementAttribute)
        let owner = focused.flatMap { WindowGeometry.owningWindow($0, pid: application.processIdentifier, in: window) }
        let localFocus = focused.flatMap { AXAccess.read($0, kAXFocusedAttribute).value as? Bool }
        let route = NativeReceiverEvidence.windowRoute(
            focusedWindowMatches: focusedWindow.map { CFEqual($0, window) },
            focusPresent: focused != nil, focusWindowMatches: owner.map { CFEqual($0, window) })
        let usable = route == "yes" && localFocus != false ? focused : nil
        var ancestors = [AXUIElement]()
        if var current = usable {
            for _ in 0..<40 {
                try RequestDiagnostics.checkCancellation()
                guard !CFEqual(current, window), let parent = AXAccess.element(current, kAXParentAttribute),
                      !CFEqual(parent, current), !ancestors.contains(where: { CFEqual($0, parent) }) else { break }
                let role = AXAccess.read(parent, kAXRoleAttribute).value as? String
                guard role != kAXApplicationRole,
                      ![kAXWindowRole, kAXSheetRole].contains(role ?? "") || CFEqual(parent, window) else { break }
                ancestors.append(parent); current = parent
            }
        }
        let relatedEditor: AXUIElement?
        if let usable, let receiverRelation,
           try receiverRelation.matches(focused: usable, window: window, pid: application.processIdentifier) {
            relatedEditor = receiverRelation.editor
        } else { relatedEditor = nil }
        return Self(windowRoute: route, focus: usable, ancestors: ancestors,
                    foreground: background.isForeground && route == "yes", relatedEditor: relatedEditor, window: window)
    }
}
