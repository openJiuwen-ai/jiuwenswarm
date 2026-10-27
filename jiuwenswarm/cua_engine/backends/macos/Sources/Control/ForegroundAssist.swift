import AppKit
import ApplicationServices

/// Execute one host-authorized foreground preparation for an exact window.
/// Python chooses this route; NativeOperation requires a preceding unavailable
/// background preparation and no requested input dispatch. This primitive owns
/// no fallback policy, request-local input state or foreground/pointer cleanup.
@MainActor
struct ForegroundAssist {
    static let enabled = ProcessInfo.processInfo.environment["COMPUTER_USE_FOREGROUND_ASSIST"] == "1"

    /// Diagnostic-only facts from the existing activation boundary. No extra
    /// AX request, activation, event, retry or wait is performed here.
    private static func recordActivation(_ phase: String, app: NSRunningApplication, window: CGWindowID,
                                         accepted: Bool? = nil, reportedActive: Bool? = nil) {
        diagnose(["kind": "foreground_activation", "phase": phase,
            "targetPID": app.processIdentifier, "window": window, "callerPID": getpid(),
            "callerPolicy": NSRunningApplication.current.activationPolicy.rawValue,
            "targetPolicy": app.activationPolicy.rawValue,
            "accepted": accepted as Any? ?? NSNull(),
            "currentEventTimestamp": NSApplication.shared.currentEvent?.timestamp as Any? ?? NSNull(),
            "uptime": ProcessInfo.processInfo.systemUptime,
            "frontmostPID": NSWorkspace.shared.frontmostApplication?.processIdentifier as Any? ?? NSNull(),
            "lastReportedActive": reportedActive as Any? ?? NSNull()])
    }

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

    private static func activate(_ target: BackgroundTarget, window: CGWindowID, pointerAt: CGPoint? = nil,
                                 onCleanupUnknown: @escaping @MainActor () -> Void) async throws {
        let app = target.application
        let pid = app.processIdentifier
        let native = try nativeWindow(pid, window)
        func checkPreparation() throws {
            try RequestDiagnostics.checkCancellation()
            try target.validate(input: true)
            guard let info = WindowServerInfo.window(window),
                  (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid else {
                throw DriverError("foreground target window disappeared or changed owner", code: .staleTarget)
            }
        }
        try checkPreparation()
        // App activation is asynchronous. WindowServer can already report the
        // process in front while AppKit has not handled activation, so sending
        // make-key records at that point can leave every content window non-key.
        // Establish application activation before preparing the exact window;
        // neither this phase nor its optional AX flag proves key-window focus.
        recordActivation("willRequest", app: app, window: window)
        let activation = try await ForegroundActivation.request(target, check: checkPreparation,
            onCleanupUnknown: onCleanupUnknown)
        let accepted = activation.accepted
        recordActivation("requestReturned", app: app, window: window, accepted: accepted)
        let root = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(root, 0.2)
        var activated = false
        var lastReportedActive: Bool?
        for _ in 0..<24 {
            try checkPreparation()
            let reported = AXAccess.read(root, kAXFrontmostAttribute).value as? Bool
            lastReportedActive = reported
            if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
               reported ?? app.isActive {
                activated = true; break
            }
            try await Task.sleep(for: .milliseconds(25))
        }
        recordActivation(activated ? "activationSatisfied" : "activationExhausted", app: app, window: window,
            accepted: accepted, reportedActive: lastReportedActive)
        guard activated else {
            throw DriverError("could not establish application activation before window preparation", code: .inputUnavailable)
        }
        try checkPreparation()
        // Activation acceptance alone is not exact-window readiness. Request
        // the exact window and raise its retained AX object, then observe the
        // destination. Preparation must not manufacture a mouse down/up pair.
        // SLPS front-process selection models mouse activation and can keep
        // AppKit's makeKey requests suppressed until a real mouse-down. The
        // normal process activation above must remain intact while selecting
        // the exact retained AX window; no synthetic mouse primer is sent.
        if let native { _ = AXMessaging.performAction(native, kAXRaiseAction as CFString) }
        if let native, AXAccess.read(native, kAXSubroleAttribute).value as? String == kAXStandardWindowSubrole {
            // Front-process selection and raising do not establish AppKit's
            // exact document context. Use addressed AX window selection,
            // never raw mouse records masquerading as make-key preparation.
            try NativeDocumentContext(application: app, window: native).select(requireSelection: true, check: checkPreparation,
                onPreparation: { diagnose("foreground document: " + $0) })
        }
        // Application activation can reinstall its remembered responder. Wait
        // BEFORE setting element focus, not between focus and the first key.
        for _ in 0..<24 {
            try checkPreparation()
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
                 onAction: () -> Void,
                 onCleanupUnknown: @escaping @MainActor () -> Void = {}) async throws {
        guard Self.enabled || BackgroundTarget.isExclusive else {
            throw DriverError("foreground input is not enabled by this host", code: .modeUnsupported)
        }
        try target.validate(input: true)
        try BackgroundTarget.validateReleasedInput()
        _ = try Self.nativeWindow(target.application.processIdentifier, window)
        // Preserve the document's existing first responder when its exact
        // foreground context is already established. No activation is needed.
        if pointerAt == nil, target.isForeground, Self.focusedWindow(target.application.processIdentifier) == window {
            return
        }
        // Mouse routing depends on the window at the point, not the keyboard's
        // key window. Menus and nonactivating panels may never become key;
        // activating them can dismiss an otherwise usable surface. The pointer
        // execution unit revalidates this exact destination before dispatch.
        if let pointerAt, target.isForeground, BackgroundEvents.inputWindow(at: pointerAt) == window {
            diagnose("foreground pointer: existing destination pid=\(target.application.processIdentifier), window=\(window)")
            return
        }
        onAction()
        try await Self.activate(target, window: window, pointerAt: pointerAt, onCleanupUnknown: onCleanupUnknown)
        try target.validate(input: true)
        diagnose("foreground assist: prepared pid=\(target.application.processIdentifier), window=\(window)")
    }
}
