import AppKit
import ApplicationServices

/// Current application-menu routing evidence, shared by both protocol adapters.
/// Sampling does not activate a window or send an action.
@MainActor struct NativeMenuContext {
    let main: AXUIElement?
    let focused: AXUIElement?
    let responderWindow: AXUIElement?
    let windows: [AXUIElement]?
    let foreground: Bool
    func document(for target: AXUIElement) -> AXUIElement? {
        guard let windows else { return nil }
        let deadline = ProcessInfo.processInfo.systemUptime + 0.2
        var pid: pid_t = 0
        guard AXUIElementGetPid(target, &pid) == .success else { return nil }
        return WindowSelection.sheetDocument(target, roots: windows, same: { CFEqual($0, $1) }, sheets: { window in
            var owner: pid_t = 0
            guard AXUIElementGetPid(window, &owner) == .success, owner == pid else { return nil }
            AXUIElementSetMessagingTimeout(window, 0.05)
            @MainActor func relationship(_ key: String, limit: Int) -> [AXUIElement]? {
                var count: CFIndex = 0
                let status = AXUIElementGetAttributeValueCount(window, key as CFString, &count)
                if [.attributeUnsupported, .notImplemented, .noValue].contains(status) { return [] }
                guard status == .success, count <= limit else { return nil }
                return AXAccess.elements(window, key, limit: limit)
            }
            guard var sheets = relationship("AXSheets", limit: 32),
                  let children = relationship(kAXChildrenAttribute, limit: 64) else { return nil }
            // AppKit also publishes sheets as direct AXChildren without
            // implementing AXSheets. Only the sheet role extends menu
            // scope here; an ordinary child window is a separate receiver.
            for child in children {
                guard ProcessInfo.processInfo.systemUptime < deadline,
                      let role = AXAccess.read(child, kAXRoleAttribute).value as? String else { return nil }
                if role == kAXSheetRole { sheets.append(child) }
            }
            return sheets
        }, withinBudget: { ProcessInfo.processInfo.systemUptime < deadline })
    }
    func confirms(_ target: AXUIElement) -> Bool {
        // A single native window has no competing document. With several
        // windows, unknown main/key state cannot identify a menu receiver.
        let soleWindow = windows.map { !$0.isEmpty && $0.allSatisfy { CFEqual($0, target) } } ?? false
        let mainMatches = main.map { current in
            CFEqual(current, target) || document(for: target).map { CFEqual(current, $0) } == true
        } ?? soleWindow
        return mainMatches
            && (focused.map { CFEqual($0, target) } ?? soleWindow)
            // Inactive providers can change both window properties while
            // keeping the old document's menu callbacks. With competing
            // windows, require independent responder ownership too. This
            // is routing evidence, not proof of the command's effect.
            && (responderWindow.map { CFEqual($0, target) } ?? (soleWindow || foreground))
    }

    init(application: NSRunningApplication) {
        let root = AXUIElementCreateApplication(application.processIdentifier)
        func element(_ key: String) -> AXUIElement? {
            guard let raw = AXAccess.read(root, key).value, CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
            return unsafeDowncast(raw, to: AXUIElement.self)
        }
        let responder = element(kAXFocusedUIElementAttribute).flatMap {
            WindowGeometry.owningWindow($0, pid: application.processIdentifier)
        }
        main = element(kAXMainWindowAttribute)
        focused = element(kAXFocusedWindowAttribute)
        responderWindow = responder
        windows = AXAccess.read(root, kAXWindowsAttribute).value as? [AXUIElement]
        foreground = BackgroundTarget(application).isForeground
    }
}
