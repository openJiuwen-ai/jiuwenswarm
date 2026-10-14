import AppKit
import ApplicationServices

@MainActor
final class WindowObservation {
    typealias WindowMapping = WindowGeometry.WindowMapping
    private var windowMapping: WindowMapping?
    private var nativeWindow: AXUIElement?
    private var document: String?
    private var inputMapping: WindowMapping?
    private var mappingFailure: String?
    private var observedWindows = [AXUIElement]()
    private var windowlessPID: pid_t?
    private var applicationMenu: AXUIElement?

    // Display hint only: no refresh or validation, and never authorizes input.
    var visualWindow: WindowMapping? { inputMapping ?? windowMapping }

    /// Only a complete catalogue without working windows authorizes app menus.
    /// Clearing a closed/failed window mapping must never grant this scope.
    func observeWindowless(pid: pid_t) { windowlessPID = pid }
    func observeApplicationMenu(_ menu: AXUIElement, pid: pid_t) {
        windowlessPID = pid; applicationMenu = menu
    }

    func observeWindows(_ windows: [AXUIElement]) { observedWindows = windows }
    func observeWindow(_ window: AXUIElement?) {
        windowlessPID = nil
        applicationMenu = nil
        // Capture and tree construction share one observation. Rebinding the
        // same object must not silently accept a document change between them.
        if let window, let nativeWindow, CFEqual(window, nativeWindow) { return }
        nativeWindow = window
        document = WindowGeometry.documentIdentity(window)
        inputMapping = nil
    }

    func invalidateWindow() { windowMapping = nil; nativeWindow = nil; document = nil; inputMapping = nil; mappingFailure = nil; windowlessPID = nil; applicationMenu = nil }

    /// Window identity alone does not identify a document. Use this at input
    /// boundaries as well as observation publication, including after focus
    /// preparation and between text events. Unknown document identity stays unknown.
    func validateObservation() throws {
        let metric = RequestDiagnostics.metrics?.start(.documentValidation)
        defer { metric?.end() }
        if let document, WindowGeometry.documentIdentity(nativeWindow) != document {
            throw DriverError("the observed window now represents another document or its identity is unavailable", code: .staleTarget, next: "read this window again before input")
        }
    }

    func validate(_ mapping: WindowMapping) throws {
        try validateWindow(mapping, requireGeometry: true)
    }

    // Semantic AX edits do not use pixel coordinates. A content-sized window
    // may resize during an edit without changing its identity or document.
    func validateIdentity(_ mapping: WindowMapping) throws {
        try validateWindow(mapping, requireGeometry: false)
    }

    /// Prepared keyboard input follows a confirmed native window identity, not
    /// old screenshot pixels. Content-sized editors may resize after each key.
    /// Keep the original capture/input caches unchanged: coordinate input must
    /// still reject their stale geometry. Without an exact AX native ID, retain
    /// the original geometry proof rather than guessing a replacement window.
    func keyboardWindow(_ mapping: WindowMapping) throws -> WindowMapping {
        try validateIdentity(mapping)
        guard let nativeWindow, BackgroundEvents.nativeWindowID(nativeWindow) == mapping.id else {
            try validate(mapping)
            return mapping
        }
        guard let info = WindowServerInfo.window(mapping.id),
              (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == mapping.pid,
              let bounds = info[kCGWindowBounds as String] as? [String: Any],
              let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary),
              frame.minX.isFinite, frame.minY.isFinite, frame.width.isFinite, frame.height.isFinite,
              frame.width > 0, frame.height > 0 else {
            throw ObservationChanged("keyboard window disappeared or its current bounds are unavailable; read the app again")
        }
        return WindowMapping(id: mapping.id, pid: mapping.pid, frame: frame, title: mapping.title,
                             width: mapping.width, height: mapping.height)
    }

    private func validateWindow(_ mapping: WindowMapping, requireGeometry: Bool) throws {
        let metric = RequestDiagnostics.metrics?.start(.windowValidation)
        defer { metric?.end() }
        try validateObservation()
        guard let window = WindowServerInfo.window(mapping.id),
              (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == mapping.pid else {
            throw ObservationChanged("window disappeared or owner changed; read the app again")
        }
        guard requireGeometry else { return }
        guard let rawBounds = window[kCGWindowBounds as String] as? [String: Any],
              let bounds = CGRect(dictionaryRepresentation: rawBounds as CFDictionary), bounds == mapping.frame else {
            throw ObservationChanged("window disappeared or geometry changed; read the app again")
        }
    }

    func point(pid: pid_t, x: Double, y: Double) throws -> (CGPoint, WindowMapping) {
        let mapping = try observedWindow(pid: pid)
        guard x >= 0, y >= 0, x < Double(mapping.width), y < Double(mapping.height) else {
            throw DriverError("click: (\(x), \(y)) is outside window PNG \(mapping.width)x\(mapping.height); expected 0<=x<\(mapping.width) and 0<=y<\(mapping.height)", code: .invalidRequest, next: "read this App and choose coordinates from its latest window PNG; desktop and preview coordinates are not valid here")
        }
        return (CGPoint(x: mapping.frame.minX + x * mapping.frame.width / Double(mapping.width),
                        y: mapping.frame.minY + y * mapping.frame.height / Double(mapping.height)), mapping)
    }

    func observedWindow(pid: pid_t) throws -> WindowMapping {
        guard let mapping = windowMapping, mapping.pid == pid else {
            throw DriverError("no valid window screenshot; read this app again" + (mappingFailure.map { "; last capture: " + $0 } ?? ""), code: .staleTarget)
        }
        try validate(mapping)
        try validateInputBoundary(pid: pid, isTarget: { WindowGeometry.matches($0, mapping) })
        return mapping
    }

    /// Build an input route from the observed native window, independently of
    /// capture permissions or the last PNG. This never updates screenshot coordinates.
    func inputWindowMapping(pid: pid_t) async throws -> WindowMapping {
        guard let nativeWindow else { return try observedWindow(pid: pid) }
        try validateInputBoundary(pid: pid, isTarget: { CFEqual($0, nativeWindow) })
        if let mapping = inputMapping, mapping.pid == pid, WindowGeometry.matches(nativeWindow, mapping),
           (try? validate(mapping)) != nil { return mapping }
        inputMapping = nil
        var previous: WindowMapping?
        // AX can expose the final frame while WindowServer still animates a new
        // window. Wait for the same identity to agree; never widen geometry tolerance.
        for attempt in 0...10 {
            try validateInputBoundary(pid: pid, isTarget: { CFEqual($0, nativeWindow) })
            if let mapping = try nativeInputMapping(nativeWindow, pid: pid) {
                if let previous, previous.id == mapping.id, previous.frame == mapping.frame {
                    inputMapping = mapping
                    return mapping
                }
                previous = mapping
            } else { previous = nil }
            if attempt < 10 { try await Task.sleep(for: .milliseconds(50)) }
        }
        throw DriverError("native input window mapping is unavailable or ambiguous; read its window again", code: .inputUnavailable)
    }

    /// Keyboard events address the responder's native window. The selected AX
    /// scope can instead be a popover hosted by that document. Only containment
    /// of the requested/current control establishes this route.
    func keyboardInputWindow(pid: pid_t, element: AXUIElement? = nil) async throws -> WindowMapping {
        try validateObservation()
        if let selected = nativeWindow,
           let receiver = element ?? AXAccess.element(AXUIElementCreateApplication(pid), kAXFocusedUIElementAttribute),
           WindowGeometry.contains(receiver, in: selected),
           let owner = WindowGeometry.owningWindow(receiver, pid: pid), !CFEqual(owner, selected),
           let mapping = try nativeInputMapping(owner, pid: pid) {
            return mapping
        }
        return try await inputWindowMapping(pid: pid)
    }

    /// Element pointer input derives geometry from its nearest surface, not
    /// the screenshot or keyboard window selected for the enclosing App.
    func pointerInputWindow(pid: pid_t, element: AXUIElement) async throws -> WindowMapping {
        try validateObservation()
        if let selected = nativeWindow, WindowGeometry.contains(element, in: selected),
           let surface = WindowGeometry.pointerSurface(element, pid: pid), !CFEqual(surface, selected),
           let mapping = try nativeInputMapping(surface, pid: pid) { return mapping }
        return try await inputWindowMapping(pid: pid)
    }

    func contains(_ element: AXUIElement) -> Bool {
        nativeWindow.map { WindowGeometry.contains(element, in: $0) } ?? false
    }

    private func nativeInputMapping(_ nativeWindow: AXUIElement, pid: pid_t) throws -> WindowMapping? {
        var owner: pid_t = 0
        guard AXUIElementGetPid(nativeWindow, &owner) == .success, owner == pid,
              let hint = WindowGeometry.hint(nativeWindow), hint.frame != nil else {
            throw DriverError("observed input window identity is unavailable; read its window again", code: .staleTarget)
        }
        let windows = (CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []).compactMap { info -> WindowMapping? in
            guard (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  let id = (info[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let raw = info[kCGWindowBounds as String] as? [String: Any],
                  let frame = CGRect(dictionaryRepresentation: raw as CFDictionary), frame.width > 1, frame.height > 1 else { return nil }
            return WindowMapping(id: id, pid: pid, frame: frame, title: info[kCGWindowName as String] as? String,
                width: Int(frame.width.rounded(.up)), height: Int(frame.height.rounded(.up)))
        }
        let candidates = windows.map { WindowSelection.Candidate(id: $0.id, frame: $0.frame, title: $0.title ?? "", visible: true) }
        guard let id = WindowSelection.select(candidates, hint: hint, requireUnique: true),
              let mapping = windows.first(where: { $0.id == id }) else {
            diagnose("input window mapping: pid=\(pid) hint=\(hint); candidates=\(candidates)")
            return nil
        }
        do { try validate(mapping) } catch { return nil } // A moving frame is an observation to retry, not an input to replay.
        return mapping
    }

    private func validateInputBoundary(pid: pid_t, isTarget: (AXUIElement) -> Bool) throws {
        try validateObservation()
        // A different known document can be prepared for input. An unobserved
        // window or a dialog still requires a fresh observation of that target.
        if let current = WindowGeometry.inputWindow(pid: pid), !isTarget(current) {
            var subrole: CFTypeRef?, modal: CFTypeRef?
            _ = AXUIElementCopyAttributeValue(current, kAXSubroleAttribute as CFString, &subrole)
            _ = AXUIElementCopyAttributeValue(current, kAXModalAttribute as CFString, &modal)
            guard observedWindows.contains(where: { CFEqual($0, current) }),
                  (subrole as? String == kAXStandardWindowSubrole || (modal as? NSNumber)?.boolValue == false),
                  (modal as? NSNumber)?.boolValue != true else {
                throw DriverError("application input window changed to a new window or dialog; read the app before input", code: .staleTarget, next: "read() to inspect the current input window; operate or dismiss its dialog before returning to another window")
            }
        }
    }

    // Native actions can work without a screenshot; when a mapping exists,
    // they obey the same window/modal boundary as keyboard and pointer input.
    func menuRecipient(pid: pid_t) throws -> BackgroundTarget.MenuRecipient {
        if nativeWindow != nil { return .window(try nativeActionWindow(pid: pid)) }
        let menuMapping = applicationMenu.flatMap { menu in windowMapping.map { NativeMenuSurfaces.matches(menu, window: $0) } } == true
        guard windowlessPID == pid, (windowMapping == nil || menuMapping), observedWindows.isEmpty else {
            throw DriverError("application menu has no confirmed receiving scope", code: .inputUnavailable,
                next: "read the application and select its working window, if any")
        }
        return .application
    }

    func nativeActionWindow(pid: pid_t) throws -> AXUIElement {
        guard let nativeWindow else {
            throw DriverError("application menu target window has no observed native identity", code: .inputUnavailable,
                next: "read the intended window before using its application menu")
        }
        var owner: pid_t = 0
        guard AXUIElementGetPid(nativeWindow, &owner) == .success, owner == pid else {
            throw DriverError("application menu target window changed", code: .staleTarget)
        }
        try validateInputBoundary(pid: pid, isTarget: { CFEqual($0, nativeWindow) })
        return nativeWindow
    }

    func validateActionWindow(pid: pid_t) throws -> WindowMapping? {
        if let nativeWindow {
            try validateInputBoundary(pid: pid, isTarget: { CFEqual($0, nativeWindow) })
            // Direct native actions don't need a WindowServer coordinate route.
            return try? nativeInputMapping(nativeWindow, pid: pid)
        }
        return windowMapping == nil ? nil : try observedWindow(pid: pid)
    }


    // Capture can publish or invalidate its mapping, but cannot mutate input
    // preparation caches or document identity directly.
    var capturedMapping: WindowMapping? { windowMapping }
    func beginCapture() { windowMapping = nil }
    func failCapture(_ error: Error) { mappingFailure = String(describing: error) }
    func publishCapture(_ mapping: WindowMapping) { windowMapping = mapping; mappingFailure = nil }
    func observeCapturedWindow(_ mapping: WindowMapping, target: AXUIElement?) {
        let matches = observedWindows.filter { WindowGeometry.matches($0, mapping) }
        let native = target.flatMap { WindowGeometry.matches($0, mapping) ? $0 : nil }
            ?? (matches.count == 1 ? matches[0] : nil)
        observeWindow(native)
    }
}
