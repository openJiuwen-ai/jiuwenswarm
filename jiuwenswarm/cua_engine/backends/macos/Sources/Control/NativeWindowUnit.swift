import AppKit
import ApplicationServices

/// Prepare one native window through its declared title, never an invented
/// point in document content. This is a window-focus mechanism, not a semantic
/// action fallback. Its caller owns the operation and the selected window.
@MainActor struct NativeWindowUnit {
    let application: NSRunningApplication
    let window: AXUIElement

    /// Exact native window geometry, independent of PNGs or AX tree coverage.
    func mapping() throws -> WindowGeometry.WindowMapping {
        try BackgroundTarget(application).validate(input: true)
        guard let id = BackgroundEvents.nativeWindowID(window),
              let info = WindowServerInfo.window(id),
              (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == application.processIdentifier,
              let raw = info[kCGWindowBounds as String] as? [String: Any],
              let frame = CGRect(dictionaryRepresentation: raw as CFDictionary),
              frame.minX.isFinite, frame.minY.isFinite, frame.width.isFinite, frame.height.isFinite,
              frame.width > 0, frame.height > 0 else {
            throw DriverError("the selected window has no confirmed native input route", code: .inputUnavailable)
        }
        return .init(id: id, pid: application.processIdentifier, frame: frame, title: nil,
            width: max(1, Int(frame.width.rounded(.up))), height: max(1, Int(frame.height.rounded(.up))))
    }

    private struct Title {
        let element: AXUIElement
        let frame: CGRect
        let mapping: WindowGeometry.WindowMapping
        var point: CGPoint { CGPoint(x: frame.midX, y: frame.midY) }
    }

    private func title() throws -> Title {
        guard AXAccess.read(window, kAXRoleAttribute).value as? String == kAXWindowRole,
              AXAccess.read(window, kAXSubroleAttribute).value as? String == kAXStandardWindowSubrole,
              let element = AXAccess.element(window, kAXTitleUIElementAttribute),
              AXAccess.read(element, kAXRoleAttribute).value as? String == kAXStaticTextRole,
              AXAccess.element(element, kAXParentAttribute).map({ CFEqual($0, window) }) == true else {
            throw DriverError("this window has no confirmed native title focus target", code: .inputUnavailable)
        }
        var pid: pid_t = 0, actions: CFArray?
        guard AXUIElementGetPid(element, &pid) == .success, pid == application.processIdentifier,
              AXUIElementCopyActionNames(element, &actions) == .success,
              let declared = actions as? [String], declared.isEmpty,
              let frame = WindowGeometry.hint(element)?.frame,
              frame.minX.isFinite, frame.minY.isFinite, frame.width.isFinite, frame.height.isFinite,
              frame.width > 0, frame.height > 0 else {
            throw DriverError("the window title is interactive or its geometry is unavailable", code: .inputUnavailable)
        }
        let mapping = try mapping()
        guard mapping.frame.contains(frame) else {
            throw DriverError("window title geometry is outside the selected window", code: .inputUnavailable)
        }
        let result = Title(element: element, frame: frame, mapping: mapping)
        var hit: AXUIElement?
        let status = AXUIElementCopyElementAtPosition(AXUIElementCreateApplication(pid),
            Float(result.point.x), Float(result.point.y), &hit)
        guard status == .success, hit.map({ CFEqual($0, element) }) == true else {
            throw DriverError("the selected window title is not the native hit target", code: .inputUnavailable)
        }
        return result
    }

    func prepareTitle(check: @escaping @MainActor () throws -> Void,
                 onPreparation: @escaping @MainActor (String) -> Void,
                 onCleanupUnknown: @escaping @MainActor () -> Void) async throws {
        try check()
        let selected = try title(), target = BackgroundTarget(application)
        guard let process = NativeProcessIdentity.read(application.processIdentifier) else {
            throw DriverError("the title's process disappeared", code: .staleTarget)
        }
        try BackgroundTarget.validateReleasedInput()
        try await BackgroundEvents.prepare(target, window: selected.mapping, requireKeyboardFocus: false,
            onAction: { onPreparation("background window preparation") }, beforeAction: check)
        let click = try NativePointerIntent(unit: "pointer_sequence", kind: "click",
            parameters: ["x": 0, "y": 0], foreground: false)
        let sequence = try NativePointerEvents(request: click, from: selected.point, to: nil,
            viewport: selected.mapping.frame.size, source: Input.source())
        var recorded = false
        try await sequence.deliver(check: { _, _ in
            try check(); try target.validate(input: true); try BackgroundTarget.validateReleasedInput()
            let current = try title()
            guard NativeProcessIdentity.read(application.processIdentifier) == process,
                  current.mapping.id == selected.mapping.id, current.mapping.frame == selected.mapping.frame,
                  current.frame == selected.frame, CFEqual(current.element, selected.element) else {
                throw DriverError("the window title changed during focus preparation", code: .staleTarget)
            }
        }, post: { event, point in
            BackgroundEvents.post(event, window: selected.mapping, point: point, clickGroup: sequence.group)
        }, canRelease: { NativeProcessIdentity.read(application.processIdentifier) == process },
        skipped: onCleanupUnknown, onDispatch: {
            if !recorded {
                recorded = true
                onPreparation("native window title focus")
            }
        }, onPosted: {})
    }
}
