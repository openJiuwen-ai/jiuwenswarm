import AppKit
import ApplicationServices
@preconcurrency import ScreenCaptureKit

@MainActor
final class WindowCapture {
    typealias WindowMapping = WindowGeometry.WindowMapping
    private let observation = CapturedWindowIdentity()
    private let sharing: WindowSharing?
    init(sharing: WindowSharing? = nil) { self.sharing = sharing }

    func validate(_ mapping: WindowMapping) throws { try observation.validate(mapping) }

    func captureWindow(_ app: NSRunningApplication, target: AXUIElement? = nil,
                       nativeTarget: WindowMapping? = nil, maxDimension: Int? = nil) async throws -> (image: CapturedImage, window: WindowMapping) {
        let metric = RequestDiagnostics.metrics?.start(.windowCapture)
        defer { metric?.end() }
        try RequestDiagnostics.checkObservationBudget()
        guard CGPreflightScreenCaptureAccess() else { throw DriverError("screen recording permission denied", code: .desktopUnavailable) }
        // Retry observations only, never input. Modal transitions can leave a brief
        // mismatch between AX and ScreenCaptureKit's window lists.
        for attempt in 0..<4 {
            try RequestDiagnostics.checkObservationBudget()
            do {
                let result = try await captureWindowOnce(app, target: target,
                                                        nativeTarget: nativeTarget, maxDimension: maxDimension)
                try RequestDiagnostics.checkCancellation()
                return result
            }
            catch {
                try RequestDiagnostics.rethrowCancellation(error)
                if error is ObservationBudgetExceeded { throw error }
                try RequestDiagnostics.checkObservationBudget()
                // A supplied native mapping cannot become current again by
                // recapturing it; the enclosing read must rediscover windows.
                if nativeTarget != nil && error is ObservationChanged { throw error }
                if attempt == 3 { throw error }
                try RequestDiagnostics.checkObservationBudget()
                try await Task.sleep(for: .milliseconds(80 * (1 << attempt)))
                try RequestDiagnostics.checkObservationBudget()
            }
        }
        throw DriverError("window capture unavailable")
    }

    private func captureWindowOnce(_ app: NSRunningApplication, target: AXUIElement?, nativeTarget: WindowMapping?, maxDimension: Int?) async throws -> (image: CapturedImage, window: WindowMapping) {
        try RequestDiagnostics.checkObservationBudget()
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        try RequestDiagnostics.checkObservationBudget()
        var windows = content.windows.filter {
            $0.owningApplication?.processID == app.processIdentifier && $0.frame.width > 1 && $0.frame.height > 1
        }
        if let nativeTarget {
            guard nativeTarget.pid == app.processIdentifier else { throw DriverError("native screenshot target belongs to another process", code: .staleTarget) }
            try observation.validate(nativeTarget)
            windows = windows.filter { $0.windowID == nativeTarget.id }
        }
        // A popup can own the focused control while AXFocusedWindow still names
        // its underlying dialog. Capture the window containing the actual focus.
        let hint = nativeTarget.map { WindowSelection.Hint(frame: $0.frame, title: $0.title ?? "", id: $0.id) }
            ?? (target ?? WindowGeometry.inputWindow(pid: app.processIdentifier)).flatMap { WindowGeometry.hint($0) }
        if target != nil && hint == nil { throw DriverError("selected window has no usable screenshot identity") }
        // Native modal panels may be above layer 0. Include them when AX (or an
        // existing observation) identifies the window, not as arbitrary fallbacks.
        if hint == nil { windows = windows.filter { $0.windowLayer == 0 } }
        let candidates = windows.map { WindowSelection.Candidate(id: $0.windowID, frame: $0.frame, title: $0.title ?? "", visible: $0.isOnScreen) }
        guard let id = WindowSelection.select(candidates, hint: hint, requireUnique: target != nil),
              let window = windows.first(where: { $0.windowID == id }) else {
            if let conflict = WindowSelection.geometryConflict(candidates, hint: hint),
               let expected = hint?.frame {
                let source = nativeTarget == nil ? "accessibility" : "observed"
                throw DriverError("window screenshot geometry disagrees for native window \(conflict.id): \(source)=\(expected), ScreenCaptureKit=\(conflict.frame); window identity is known, but a safe screenshot coordinate mapping is unavailable; read the window again after its layout settles")
            }
            let details = candidates.sorted { $0.visible && !$1.visible }.prefix(6).map { "\($0.id):\(String(reflecting: $0.title)) \($0.frame) visible=\($0.visible)" }.joined(separator: "; ")
            let focused = hint.map { " focused=\(String(reflecting: $0.title)) \(String(describing: $0.frame));" } ?? ""
            throw DriverError("\(windows.isEmpty ? "no capturable application window" : "window screenshot target is ambiguous or not ready");\(focused) candidates=\(candidates.count) [\(details)]")
        }
        let configuration = SCStreamConfiguration()
        let scale: CGFloat
        if let maxDimension { scale = min(1, CGFloat(maxDimension) / max(window.frame.width, window.frame.height)) }
        else { scale = 1 }
        configuration.width = max(1, min(maxDimension ?? Int.max, Int((window.frame.width * scale).rounded(.up))))
        configuration.height = max(1, min(maxDimension ?? Int.max, Int((window.frame.height * scale).rounded(.up))))
        configuration.showsCursor = false
        configuration.scalesToFit = true
        configuration.ignoreShadowsSingleWindow = true
        configuration.ignoreGlobalClipSingleWindow = true
        if #available(macOS 14.2, *) {
            // Otherwise a sheet may be rendered together with its parent, scaled
            // into the sheet-sized PNG. That would silently corrupt click coordinates.
            configuration.includeChildWindows = false
        }
        let mapping = WindowMapping(id: window.windowID, pid: app.processIdentifier, frame: window.frame, title: window.title, width: configuration.width, height: configuration.height)
        try RequestDiagnostics.checkObservationBudget()
        observation.observeCapturedWindow(mapping, target: target)
        try observation.validateObservation()
        try RequestDiagnostics.checkObservationBudget()
        try await sharing?.include(window)
        try RequestDiagnostics.checkObservationBudget()
        let interval = CaptureInterval()
        let image = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: window), configuration: configuration)
        try RequestDiagnostics.checkObservationBudget()
        try observation.validate(mapping)
        guard image.width == mapping.width, image.height == mapping.height else { throw DriverError("unexpected window screenshot dimensions") }
        try RequestDiagnostics.checkObservationBudget()
        let result = try CapturedImage.encodePNG(image)
        // Completed pixels and their validated identity can be published during
        // the request's finalization reserve without starting another capture.
        try RequestDiagnostics.checkCancellation()
        return (CapturedImage(png: result, width: image.width, height: image.height, interval: interval), mapping)
    }

}
