import AppKit
import ApplicationServices

@MainActor
enum WindowGeometry {
    // Controlled diagnostics only: extra AX reads can change timing-sensitive
    // behavior. Ordinary input must retain its original query/wait sequence.
    static let inputDiagnosticsEnabled = ProcessInfo.processInfo.environment["COMPUTER_USE_DIAGNOSE_INPUT"] == "1"
    // Optional macOS SPI maps an AX window to its WindowServer identity. It is
    // shared by capture and input; if absent, keep the unique public-data route.
    private typealias NativeWindowID = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> Int32
    private static let accessibilityLibrary = dlopen("/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework/HIServices", RTLD_LAZY | RTLD_LOCAL)
    private static let getNativeWindowID: NativeWindowID? = accessibilityLibrary
        .flatMap { dlsym($0, "_AXUIElementGetWindow") }.map { unsafeBitCast($0, to: NativeWindowID.self) }
    struct WindowMapping {
        let id: CGWindowID
        let pid: pid_t
        let frame: CGRect
        let title: String?
        let width: Int
        let height: Int
    }
    /// This window's own raw AXDocument. Unknown metadata stays unknown; no
    /// parent lookup, URI normalization or filesystem access is performed.
    static func documentIdentity(_ window: AXUIElement?) -> String? {
        guard let window else { return nil }
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(window, kAXDocumentAttribute as CFString, &raw) == .success else { return nil }
        if let value = raw as? String, !value.isEmpty { return value }
        if let raw, CFGetTypeID(raw) == CFURLGetTypeID() { return (raw as! URL).absoluteString }
        return nil
    }

    static func hint(_ element: AXUIElement) -> WindowSelection.Hint? {
        func attribute(_ key: String) -> CFTypeRef? {
            var value: CFTypeRef?
            return AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success ? value : nil
        }
        var origin = CGPoint.zero, size = CGSize.zero
        let title = attribute(kAXTitleAttribute) as? String ?? ""
        var number: CGWindowID = 0
        let id = getNativeWindowID?(element, &number) == AXError.success.rawValue && number != 0 ? number : nil
        // AXWindows can expose a content element as its top-level surface (for
        // example an inline editor). Its AX frame describes content, not window
        // pixels. Declared roots and popovers may use WindowServer geometry only
        // with an exact native ID; arbitrary controls retain their AX bounds.
        if let id, ![kAXWindowRole, kAXSheetRole].contains(attribute(kAXRoleAttribute) as? String ?? ""),
           (attribute(kAXRoleAttribute) as? String == "AXPopover" || isDeclaredWindowRoot(element)),
           let info = WindowServerInfo.window(id) {
            var pid: pid_t = 0
            if AXUIElementGetPid(element, &pid) == .success,
               (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
               let bounds = info[kCGWindowBounds as String] as? [String: Any],
               let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary), frame.width > 1, frame.height > 1 {
                return WindowSelection.Hint(frame: frame, title: title, id: id)
            }
        }
        if let position = attribute(kAXPositionAttribute), CFGetTypeID(position) == AXValueGetTypeID(),
           let dimensions = attribute(kAXSizeAttribute), CFGetTypeID(dimensions) == AXValueGetTypeID(),
           AXValueGetValue(unsafeDowncast(position, to: AXValue.self), .cgPoint, &origin),
           AXValueGetValue(unsafeDowncast(dimensions, to: AXValue.self), .cgSize, &size),
           origin.x.isFinite, origin.y.isFinite, size.width.isFinite, size.height.isFinite,
           size.width > 0, size.height > 0 {
            return WindowSelection.Hint(frame: CGRect(origin: origin, size: size), title: title, id: id)
        }
        return title.isEmpty && id == nil ? nil : WindowSelection.Hint(frame: nil, title: title, id: id)
    }

    static func isDeclaredWindowRoot(_ element: AXUIElement) -> Bool {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success else { return false }
        let root = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(root, 0.1)
        return (AXAccess.read(root, kAXWindowsAttribute).value as? [AXUIElement])?
            .contains(where: { CFEqual($0, element) }) == true
    }

    /// A logical interaction scope is an AX subtree, not a WindowServer ID.
    /// Use native object relationships; neither matching titles nor a common
    /// process authorizes input in another document.
    static func contains(_ element: AXUIElement, in root: AXUIElement) -> Bool {
        var current: AXUIElement? = element, seen = [AXUIElement]()
        while let node = current, seen.count < 32, !seen.contains(where: { CFEqual($0, node) }) {
            if CFEqual(node, root) { return true }
            seen.append(node)
            current = AXAccess.element(node, kAXParentAttribute)
        }
        for key in [kAXWindowAttribute, kAXTopLevelUIElementAttribute] {
            if let owner = AXAccess.element(element, key), CFEqual(owner, root) { return true }
        }
        // Some providers omit parent links but still expose their children.
        let deadline = ProcessInfo.processInfo.systemUptime + 0.15
        return WindowSelection.containing(seen, in: [root], same: { CFEqual($0, $1) },
            isWindow: { _ in false }, children: { AXAccess.elements($0, kAXChildrenAttribute, limit: 64) ?? [] },
            withinBudget: { ProcessInfo.processInfo.systemUptime < deadline }) != nil
    }

    static func matches(_ element: AXUIElement, _ mapping: WindowMapping) -> Bool {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success, pid == mapping.pid,
              let hint = hint(element) else { return false }
        return matches(hint, mapping)
    }

    static func matches(_ hint: WindowSelection.Hint, _ mapping: WindowMapping) -> Bool {
        guard let frame = hint.frame else { return false }
        guard WindowSelection.sameFrame(frame, mapping.frame) else { return false }
        if let id = hint.id { return id == mapping.id }
        if let title = mapping.title, !title.isEmpty, !hint.title.isEmpty { return title == hint.title }
        return true
    }

    /// A view service can report its backing window as AX focus while the host
    /// exposes the visible window and forwards the exact same AX children. Keep
    /// service identity for controls/events, but use the confirmed presenter for
    /// window containment, capture and modal boundaries. Never match by pixels.
    static func presentedWindow(_ element: AXUIElement, pid: pid_t) -> AXUIElement {
        guard let service = BackgroundEvents.accessibilityProcessID(element), service != pid,
              let role = AXAccess.read(element, kAXRoleAttribute).value as? String,
              [kAXWindowRole, kAXSheetRole].contains(role) else { return element }
        var presenter: pid_t = 0
        guard AXUIElementGetPid(element, &presenter) == .success, presenter == pid else { return element }
        func children(_ node: AXUIElement) -> [AXUIElement]? {
            var count: CFIndex = 0, raw: CFArray?
            guard AXUIElementGetAttributeValueCount(node, kAXChildrenAttribute as CFString, &count) == .success,
                  count > 0, count <= 64,
                  AXUIElementCopyAttributeValues(node, kAXChildrenAttribute as CFString, 0, count, &raw) == .success,
                  let result = raw as? [AXUIElement], result.count == count else { return nil }
            return result
        }
        let root = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(root, 0.1)
        var pending = AXAccess.read(root, kAXWindowsAttribute).value as? [AXUIElement] ?? []
        for key in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
            if let raw = AXAccess.read(root, key).value, CFGetTypeID(raw) == AXUIElementGetTypeID() {
                pending.append(unsafeDowncast(raw, to: AXUIElement.self))
            }
        }
        var seen = [AXUIElement](), candidates = [AXUIElement](), cursor = 0
        let deadline = ProcessInfo.processInfo.systemUptime + 0.2
        while cursor < pending.count, seen.count < 32, ProcessInfo.processInfo.systemUptime < deadline {
            let candidate = pending[cursor]; cursor += 1
            guard !seen.contains(where: { CFEqual($0, candidate) }) else { continue }
            seen.append(candidate)
            AXUIElementSetMessagingTimeout(candidate, 0.05)
            let candidateRole = AXAccess.read(candidate, kAXRoleAttribute).value as? String
            guard [kAXWindowRole, kAXSheetRole].contains(candidateRole ?? "") else { continue }
            if candidateRole == role, BackgroundEvents.accessibilityProcessID(candidate) == pid,
               let id = BackgroundEvents.nativeWindowID(candidate), let info = WindowServerInfo.window(id),
               (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid {
                candidates.append(candidate)
            }
            pending += (AXAccess.read(candidate, "AXSheets").value as? [AXUIElement] ?? [])
                + (children(candidate) ?? []).filter {
                    [kAXWindowRole, kAXSheetRole].contains(AXAccess.read($0, kAXRoleAttribute).value as? String ?? "")
                }
        }
        // Incomplete candidate enumeration cannot establish uniqueness.
        guard cursor == pending.count, let host = WindowSelection.contentHost(element, candidates: candidates,
            children: children, same: { CFEqual($0, $1) }),
              BackgroundEvents.accessibilityProcessID(element) == service else { return element }
        return host
    }

    /// Resolve native containment without borrowing screenshot identity or the
    /// application's current keyboard window. Sheets precede their owner document.
    static func owningWindow(_ element: AXUIElement, pid: pid_t, within observed: WindowMapping? = nil,
                             in selected: AXUIElement? = nil) -> AXUIElement? {
        let metric = RequestDiagnostics.metrics?.start(.windowMembership)
        defer { metric?.end() }
        if let selected, AXAccess.read(selected, kAXRoleAttribute).value as? String == "AXPopover",
           contains(element, in: selected) { return selected }
        func value(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
            var raw: CFTypeRef?
            return AXUIElementCopyAttributeValue(element, key as CFString, &raw) == .success ? raw : nil
        }
        func native(_ element: AXUIElement, _ key: String) -> AXUIElement? {
            guard let raw = value(element, key), CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
            let result = unsafeDowncast(raw, to: AXUIElement.self)
            var owner: pid_t = 0
            return AXUIElementGetPid(result, &owner) == .success && owner == pid ? result : nil
        }
        var declaredRoots: [AXUIElement]?
        func isWindow(_ element: AXUIElement) -> Bool {
            if [kAXWindowRole, kAXSheetRole].contains(value(element, kAXRoleAttribute) as? String ?? "") { return true }
            if declaredRoots == nil {
                declaredRoots = value(AXUIElementCreateApplication(pid), kAXWindowsAttribute) as? [AXUIElement] ?? []
            }
            return declaredRoots!.contains { CFEqual($0, element) }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + 0.3
        var ancestors = [AXUIElement](), links = [AXUIElement](), current: AXUIElement? = element
        while let node = current, ancestors.count < 32, ProcessInfo.processInfo.systemUptime < deadline {
            guard !ancestors.contains(where: { CFEqual($0, node) }) else { break }
            ancestors.append(node)
            if isWindow(node) { return presentedWindow(node, pid: pid) }
            for key in [kAXTopLevelUIElementAttribute, kAXWindowAttribute] {
                if let owner = native(node, key), isWindow(owner), !links.contains(where: { CFEqual($0, owner) }) {
                    if value(owner, kAXRoleAttribute) as? String == kAXSheetRole { return presentedWindow(owner, pid: pid) }
                    links.append(owner)
                }
            }
            current = native(node, kAXParentAttribute)
        }
        if let linked = links.first { return presentedWindow(linked, pid: pid) }
        // AXWindows often lists documents but omits attached file panels.
        // Accessory controls can also omit their parent/window links. Resolve
        // the native window catalogue before searching containment, using the
        // same declared sheet edges as observation. Main/key status contributes
        // candidates only; it never establishes the control's membership.
        func windowRoots() -> [AXUIElement] {
            let root = AXUIElementCreateApplication(pid)
            var pending = value(root, kAXWindowsAttribute) as? [AXUIElement] ?? []
            for key in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
                if let window = native(root, key) { pending.append(window) }
            }
            var result = [AXUIElement](), seen = [AXUIElement](), cursor = 0
            while cursor < pending.count, seen.count < 32, ProcessInfo.processInfo.systemUptime < deadline {
                let candidate = presentedWindow(pending[cursor], pid: pid); cursor += 1
                guard !seen.contains(where: { CFEqual($0, candidate) }) else { continue }
                seen.append(candidate)
                var owner: pid_t = 0
                guard AXUIElementGetPid(candidate, &owner) == .success, owner == pid, isWindow(candidate) else { continue }
                result.append(candidate)
                pending += value(candidate, "AXSheets") as? [AXUIElement] ?? []
                pending += (value(candidate, kAXChildrenAttribute) as? [AXUIElement] ?? []).prefix(64).filter {
                    [kAXWindowRole, kAXSheetRole].contains(value($0, kAXRoleAttribute) as? String ?? "")
                }
            }
            return result
        }
        // When an action already names its observed window, only search that
        // native subtree. Unrelated documents must not consume the bounded
        // membership query. This is still an identity lookup, not permission
        // to assume containment; conflicting direct parent/window links above
        // remain authoritative and are rejected by the caller.
        let candidates: [AXUIElement]
        if let selected {
            var owner: pid_t = 0
            guard AXUIElementGetPid(selected, &owner) == .success, owner == pid, isWindow(selected) else { return nil }
            candidates = [selected]
        } else if let observed {
            guard observed.pid == pid else { return nil }
            candidates = windowRoots().filter { matches($0, observed) }
            guard candidates.count == 1 else { return nil }
        } else {
            candidates = windowRoots()
        }
        return WindowSelection.containing(ancestors, in: candidates, same: { CFEqual($0, $1) }, isWindow: isWindow, children: { node in
            var raw: CFArray?, count: CFIndex = 0
            guard AXUIElementGetAttributeValueCount(node, kAXChildrenAttribute as CFString, &count) == .success,
                  count > 0, AXUIElementCopyAttributeValues(node, kAXChildrenAttribute as CFString, 0, min(count, 64), &raw) == .success else { return [] }
            return raw as? [AXUIElement] ?? []
        }, withinBudget: { ProcessInfo.processInfo.systemUptime < deadline })
    }

    /// Only native identity and AX status are logged. Titles, values, document
    /// URLs and text are deliberately excluded from focus diagnostics.
    private static func focusDiagnostic(_ element: AXUIElement?) -> [String: Any] {
        guard let element else { return ["present": false] }
        var pid: pid_t = 0, id: CGWindowID = 0, role: CFTypeRef?
        let pidStatus = AXUIElementGetPid(element, &pid)
        let roleStatus = AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
        var result: [String: Any] = ["present": true, "pidStatus": pidStatus.rawValue,
            "roleStatus": roleStatus.rawValue]
        if pidStatus == .success { result["pid"] = pid }
        if roleStatus == .success { result["role"] = role as? String ?? "<not a string>" }
        if let getNativeWindowID {
            let status = getNativeWindowID(element, &id)
            result["windowStatus"] = status
            if status == 0 { result["windowID"] = id }
        } else { result["windowStatus"] = "unavailable" }
        return result
    }

    static func inputWindow(pid: pid_t, preferring expected: WindowMapping? = nil,
                            diagnosticStage: String? = nil) -> AXUIElement? {
        let diagnosticStage = inputDiagnosticsEnabled ? diagnosticStage : nil
        let diagnosticStarted = diagnosticStage == nil ? nil : ProcessInfo.processInfo.systemUptime
        let root = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(root, 0.1)
        var evidence = [String: Any]()
        func attribute(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
            var raw: CFTypeRef?
            return AXUIElementCopyAttributeValue(element, key as CFString, &raw) == .success ? raw : nil
        }
        func nativeElement(_ element: AXUIElement, _ key: String) -> AXUIElement? {
            var raw: CFTypeRef?
            let status = AXUIElementCopyAttributeValue(element, key as CFString, &raw)
            let isElement = raw.map { CFGetTypeID($0) == AXUIElementGetTypeID() } ?? false
            if diagnosticStage != nil {
                evidence[key] = ["status": status.rawValue, "isElement": isElement,
                    "element": focusDiagnostic(isElement ? unsafeDowncast(raw!, to: AXUIElement.self) : nil)] as [String: Any]
            }
            guard status == .success, let raw, isElement else { return nil }
            let result = unsafeDowncast(raw, to: AXUIElement.self)
            var owner: pid_t = 0
            guard AXUIElementGetPid(result, &owner) == .success, owner == pid else { return nil }
            return result
        }
        // Preserve the main-window fallback when a bridge leaves a defunct
        // AXFocusedWindow object without any usable capture hint.
        let fallback = [kAXFocusedWindowAttribute, kAXMainWindowAttribute]
            .compactMap { key in nativeElement(root, key).map { (key, $0) } }
            .first { Self.hint($0.1) != nil }
        func finish(_ selected: AXUIElement?, source: String) -> AXUIElement? {
            if let diagnosticStage {
                var fields: [String: Any] = ["kind": "input_window_resolution", "stage": diagnosticStage,
                    "pid": pid, "foregroundPID": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0,
                    "attributes": evidence, "source": source, "selected": focusDiagnostic(selected)]
                if let expected {
                    fields["expectedWindowID"] = expected.id
                    fields["matchesExpected"] = selected.map { matches($0, expected) } ?? false
                }
                if let diagnosticStarted { fields["durationMs"] = (ProcessInfo.processInfo.systemUptime - diagnosticStarted) * 1000 }
                diagnose(fields)
            }
            return selected
        }
        func attachedSheet(_ window: AXUIElement?) -> AXUIElement? {
            guard var window else { return nil }
            var seen = [AXUIElement]()
            while seen.count < 16 && !seen.contains(where: { CFEqual($0, window) }) {
                seen.append(window)
                let children = (attribute(window, "AXSheets") as? [AXUIElement] ?? [])
                    + (attribute(window, kAXChildrenAttribute) as? [AXUIElement] ?? [])
                var sheets = [AXUIElement]()
                for child in children where attribute(child, kAXRoleAttribute) as? String == kAXSheetRole
                    && Self.hint(child) != nil && !sheets.contains(where: { CFEqual($0, child) }) { sheets.append(child) }
                guard sheets.count == 1 else { break }
                window = sheets[0]
            }
            return window
        }
        let focus = nativeElement(root, kAXFocusedUIElementAttribute)
        guard let focus, attribute(focus, kAXRoleAttribute) is String else {
            let selected = attachedSheet(fallback?.1)
            let source = fallback.map { entry in
                selected.map { CFEqual($0, entry.1) } == true ? entry.0 : entry.0 + "/attached-sheet"
            } ?? "none"
            return finish(selected, source: source)
        }
        // Confirm a known receiver in its own subtree first. If it is absent,
        // still look for a different actual owner; a preference never proves
        // focus and must not mask a new popup or another document.
        let owner = owningWindow(focus, pid: pid, within: expected)
            ?? (expected == nil ? nil : owningWindow(focus, pid: pid))
        let base = owner ?? fallback?.1
        let selected = attachedSheet(base)
        var source = owner == nil ? fallback?.0 ?? "none" : "AXFocusedUIElement/owning-window"
        if let base, let selected, !CFEqual(base, selected) { source += "/attached-sheet" }
        return finish(selected, source: source)
    }

    static func verifyInputWindow(_ window: WindowMapping) throws {
        guard let current = inputWindow(pid: window.pid, preferring: window,
            diagnosticStage: inputDiagnosticsEnabled ? "verify-input-window" : nil), matches(current, window) else {
            throw DriverError("target window could not be prepared for input; window focus may have changed; remaining input was not dispatched", code: .inputUnavailable, next: "read the app to inspect the active window before continuing")
        }
    }
}
