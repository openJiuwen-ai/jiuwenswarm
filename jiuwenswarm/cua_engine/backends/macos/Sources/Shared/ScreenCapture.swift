import AppKit
import ApplicationServices

/// Capture and input use one observation identity, with independent pixel and
/// prepared-input mappings. This facade preserves existing backend callers.
@MainActor
final class ScreenCapture {
    typealias WindowMapping = WindowGeometry.WindowMapping
    static var inputDiagnosticsEnabled: Bool { WindowGeometry.inputDiagnosticsEnabled }
    private let observation: WindowObservation
    private let capture: WindowCapture
    var visualWindow: WindowMapping? { observation.visualWindow }
    init(sharing: WindowSharing? = nil) {
        let observation = WindowObservation()
        self.observation = observation
        self.capture = WindowCapture(observation: observation, sharing: sharing)
    }

    func observeWindowless(pid: pid_t) {
        observation.observeWindowless(pid: pid)
    }

    func observeApplicationMenu(_ menu: AXUIElement, pid: pid_t) {
        observation.observeApplicationMenu(menu, pid: pid)
    }

    func observeWindows(_ windows: [AXUIElement]) {
        observation.observeWindows(windows)
    }

    func observeWindow(_ window: AXUIElement?) {
        observation.observeWindow(window)
    }

    func invalidateWindow() {
        observation.invalidateWindow()
    }

    static func documentIdentity(_ window: AXUIElement?) -> String? {
        return WindowGeometry.documentIdentity(window)
    }

    func validateObservation() throws {
        try observation.validateObservation()
    }

    func captureWindow(_ app: NSRunningApplication, updateObservation: Bool = true, target: AXUIElement? = nil,
                       nativeTarget: WindowMapping? = nil, maxDimension: Int? = nil) async throws -> (image: CapturedImage, window: WindowMapping) {
        return try await capture.captureWindow(app, updateObservation: updateObservation, target: target,
                                             nativeTarget: nativeTarget, maxDimension: maxDimension)
    }

    func validate(_ mapping: WindowMapping) throws {
        try observation.validate(mapping)
    }

    func validateIdentity(_ mapping: WindowMapping) throws {
        try observation.validateIdentity(mapping)
    }

    func keyboardWindow(_ mapping: WindowMapping) throws -> WindowMapping {
        return try observation.keyboardWindow(mapping)
    }

    static func hint(_ element: AXUIElement) -> WindowSelection.Hint? {
        return WindowGeometry.hint(element)
    }

    static func isDeclaredWindowRoot(_ element: AXUIElement) -> Bool {
        return WindowGeometry.isDeclaredWindowRoot(element)
    }

    static func matches(_ element: AXUIElement, _ mapping: WindowMapping) -> Bool {
        return WindowGeometry.matches(element, mapping)
    }

    static func matches(_ hint: WindowSelection.Hint, _ mapping: WindowMapping) -> Bool {
        return WindowGeometry.matches(hint, mapping)
    }

    static func presentedWindow(_ element: AXUIElement, pid: pid_t) -> AXUIElement {
        return WindowGeometry.presentedWindow(element, pid: pid)
    }

    static func owningWindow(_ element: AXUIElement, pid: pid_t, within observed: WindowMapping? = nil,
                             in selected: AXUIElement? = nil) -> AXUIElement? {
        return WindowGeometry.owningWindow(element, pid: pid, within: observed, in: selected)
    }

    static func inputWindow(pid: pid_t, preferring expected: WindowMapping? = nil,
                            diagnosticStage: String? = nil) -> AXUIElement? {
        return WindowGeometry.inputWindow(pid: pid, preferring: expected, diagnosticStage: diagnosticStage)
    }

    func point(pid: pid_t, x: Double, y: Double) throws -> (CGPoint, WindowMapping) {
        return try observation.point(pid: pid, x: x, y: y)
    }

    func observedWindow(pid: pid_t) throws -> WindowMapping {
        return try observation.observedWindow(pid: pid)
    }

    func inputWindowMapping(pid: pid_t) async throws -> WindowMapping {
        return try await observation.inputWindowMapping(pid: pid)
    }

    func keyboardInputWindow(pid: pid_t, element: AXUIElement? = nil) async throws -> WindowMapping {
        try await observation.keyboardInputWindow(pid: pid, element: element)
    }

    func pointerInputWindow(pid: pid_t, element: AXUIElement) async throws -> WindowMapping {
        try await observation.pointerInputWindow(pid: pid, element: element)
    }

    func contains(_ element: AXUIElement) -> Bool { observation.contains(element) }

    func menuRecipient(pid: pid_t) throws -> BackgroundTarget.MenuRecipient {
        return try observation.menuRecipient(pid: pid)
    }

    func nativeActionWindow(pid: pid_t) throws -> AXUIElement {
        return try observation.nativeActionWindow(pid: pid)
    }

    func validateActionWindow(pid: pid_t) throws -> WindowMapping? {
        return try observation.validateActionWindow(pid: pid)
    }

    static func verifyInputWindow(_ window: WindowMapping) throws {
        try WindowGeometry.verifyInputWindow(window)
    }

}
