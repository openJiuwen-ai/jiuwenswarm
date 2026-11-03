import AppKit
import ApplicationServices

/// Identity retained with a window image. It validates that image's document,
/// owner and geometry; input routing is owned by the current execution units.
@MainActor final class CapturedWindowIdentity {
    private var nativeWindow: AXUIElement?
    private var document: String?

    func observeCapturedWindow(_ mapping: WindowGeometry.WindowMapping, target: AXUIElement?) {
        let window = target.flatMap { WindowGeometry.matches($0, mapping) ? $0 : nil }
        // A retry for the same object must not accept a different document.
        if let window, let nativeWindow, CFEqual(window, nativeWindow) { return }
        nativeWindow = window
        document = WindowGeometry.documentIdentity(window)
    }

    func validateObservation() throws {
        let metric = RequestDiagnostics.metrics?.start(.documentValidation)
        defer { metric?.end() }
        if let document, WindowGeometry.documentIdentity(nativeWindow) != document {
            throw DriverError("the observed window now represents another document or its identity is unavailable", code: .staleTarget, next: "read this window again before input")
        }
    }

    func validate(_ mapping: WindowGeometry.WindowMapping) throws {
        let metric = RequestDiagnostics.metrics?.start(.windowValidation)
        defer { metric?.end() }
        try validateObservation()
        guard let window = WindowServerInfo.window(mapping.id),
              (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == mapping.pid else {
            throw ObservationChanged("window disappeared or owner changed; read the app again")
        }
        guard let rawBounds = window[kCGWindowBounds as String] as? [String: Any],
              let bounds = CGRect(dictionaryRepresentation: rawBounds as CFDictionary), bounds == mapping.frame else {
            throw ObservationChanged("window disappeared or geometry changed; read the app again")
        }
    }
}
