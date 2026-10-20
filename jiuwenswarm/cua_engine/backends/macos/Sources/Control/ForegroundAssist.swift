import AppKit
import ApplicationServices

/// Execute one host-authorized foreground preparation for an exact window.
/// Python chooses this route; NativeOperation requires a preceding unavailable
/// background preparation and no requested input dispatch. This primitive owns
/// no fallback policy, request-local input state or foreground/pointer cleanup.
@MainActor
struct ForegroundAssist {
    static let enabled = ProcessInfo.processInfo.environment["COMPUTER_USE_FOREGROUND_ASSIST"] == "1"

    private static func focusedWindow(_ pid: pid_t) -> CGWindowID? {
        let root = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(root, 0.2)
        guard let raw = AXAccess.read(root, kAXFocusedWindowAttribute).value,
              CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
        return BackgroundEvents.nativeWindowID(unsafeDowncast(raw, to: AXUIElement.self))
    }

    static func nativeWindow(_ pid: pid_t, _ id: CGWindowID) throws -> AXUIElement? {
        guard let info = WindowServerInfo.window(id),
              (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid else {
            throw DriverError("foreground target window disappeared or changed owner", code: .staleTarget)
        }
        let root = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(root, 0.2)
        // AXWindows commonly lists documents but omits their sheets. Resolve
        // the requested native ID through declared focus and attached surfaces
        // too, so activation can raise the actual input surface. Never choose a
        // different document by title, bounds or whichever window is focused.
        var pending = AXAccess.read(root, kAXWindowsAttribute).value as? [AXUIElement] ?? []
        for key in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
            if let raw = AXAccess.read(root, key).value, CFGetTypeID(raw) == AXUIElementGetTypeID() {
                pending.insert(unsafeDowncast(raw, to: AXUIElement.self), at: 0)
            }
        }
        var seen = [AXUIElement](), cursor = 0
        let deadline = ProcessInfo.processInfo.systemUptime + 0.2
        while cursor < pending.count, seen.count < 32, ProcessInfo.processInfo.systemUptime < deadline {
            let candidate = WindowGeometry.presentedWindow(pending[cursor], pid: pid); cursor += 1
            guard !seen.contains(where: { CFEqual($0, candidate) }) else { continue }
            seen.append(candidate)
            var owner: pid_t = 0
            guard AXUIElementGetPid(candidate, &owner) == .success, owner == pid else { continue }
            if BackgroundEvents.nativeWindowID(candidate) == id { return candidate }
            AXUIElementSetMessagingTimeout(candidate, 0.05)
            pending += AXAccess.read(candidate, "AXSheets").value as? [AXUIElement] ?? []
            pending += (AXAccess.read(candidate, kAXChildrenAttribute).value as? [AXUIElement] ?? []).filter {
                [kAXWindowRole, kAXSheetRole].contains(AXAccess.read($0, kAXRoleAttribute).value as? String ?? "")
            }
        }
        return nil
    }

    private static func activate(_ app: NSRunningApplication, window: CGWindowID, pointerAt: CGPoint? = nil) async throws {
        let pid = app.processIdentifier
        let native = try nativeWindow(pid, window)
        guard !app.isTerminated else { throw DriverError("foreground application exited", code: .staleTarget) }
        // A synthetic background activation can leave AppKit's isActive cached
        // true; activate() may then return false. WindowServer is authoritative.
        try BackgroundEvents.makeKeyWindow(window)
        if let native { _ = AXMessaging.performAction(native, kAXRaiseAction as CFString) }
        // Application activation can reinstall its remembered responder. Wait
        // BEFORE setting element focus, not between focus and the first key.
        for _ in 0..<24 {
            try Task.checkCancellation()
            // A mouse destination can be an ordinary-level non-key panel.
            // AXFocusedWindow correctly keeps reporting its document in that
            // case. The actual input hit is the relevant proof for mouse input;
            // keyboard preparation retains its focused-window requirement.
            // The pointer execution unit rechecks the hit before
            // sending each button, so activation alone never authorizes a click.
            let destinationReady: Bool
            if let pointerAt, let hit = BackgroundEvents.inputWindow(at: pointerAt) {
                destinationReady = hit == window
            } else {
                destinationReady = focusedWindow(pid) == nil || focusedWindow(pid) == window
            }
            if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
               destinationReady {
                try await Task.sleep(for: .milliseconds(60))
                return
            }
            try await Task.sleep(for: .milliseconds(25))
        }
        if let receiving = focusedWindow(pid), receiving != window,
           let info = WindowServerInfo.window(receiving),
           (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid {
            let title = info[kCGWindowName as String] as? String ?? ""
            let label = title.isEmpty ? "another window of this application" : DiagnosticText.quoted(DiagnosticText.bounded(title, limit: 160))
            throw DriverError("could not establish the intended foreground window; the application reports keyboard input in \(label)",
                code: .inputUnavailable, next: "read that receiving window from the app's window list and handle its UI before retrying this document")
        }
        throw DriverError("could not establish the intended foreground window", code: .inputUnavailable,
            next: "read the desktop and inspect the application's other windows before retrying")
    }

    func prepare(_ target: BackgroundTarget, window: CGWindowID, pointerAt: CGPoint? = nil,
                 onAction: () -> Void) async throws {
        guard Self.enabled || BackgroundTarget.isExclusive else {
            throw DriverError("foreground input is not enabled by this host", code: .modeUnsupported)
        }
        try target.validate(input: true)
        try BackgroundTarget.validateReleasedInput()
        _ = try Self.nativeWindow(target.application.processIdentifier, window)
        // Mouse routing depends on the window at the point, not the keyboard's
        // key window. Menus and nonactivating panels may never become key;
        // activating them can dismiss an otherwise usable surface. The pointer
        // execution unit revalidates this exact destination before dispatch.
        if let pointerAt, target.isForeground, BackgroundEvents.inputWindow(at: pointerAt) == window {
            diagnose("foreground pointer: existing destination pid=\(target.application.processIdentifier), window=\(window)")
            return
        }
        onAction()
        try await Self.activate(target.application, window: window, pointerAt: pointerAt)
        try target.validate(input: true)
        diagnose("foreground assist: prepared pid=\(target.application.processIdentifier), window=\(window)")
    }
}
