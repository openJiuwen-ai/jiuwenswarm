import AppKit
import CoreGraphics
@preconcurrency import ScreenCaptureKit

/// Desktop observation does not traverse AX, select an application or change
/// focus. Exclusive input retains optional focused-window evidence separately.
@MainActor final class DesktopCapture {
    private let applications: ApplicationDiscovery
    init(applications: ApplicationDiscovery) { self.applications = applications }
    struct Mapping {
        let geometry: DesktopGeometry
        let captured: Set<CGDirectDisplayID>
        let surfaces: [Surface]
        let processes: Set<NativeProcessIdentity>
        let inputIssue: String?
    }
    struct Surface: Equatable {
        let id: CGWindowID
        let pid: pid_t
        let frame: CGRect
    }
    let scope = UUID().uuidString
    private var mappings = [String: Mapping]()
    private var order = [String]()
    struct KeyboardDestination: Equatable {
        let process: NativeProcessIdentity
        let window: CGWindowID?
        let focusedWindow: CGWindowID?

        func validate(_ current: Self?) throws {
            guard let current else {
                throw DriverError("desktop keyboard destination is unavailable; observe before input", code: .inputUnavailable)
            }
            guard current.process == process, current.window == window,
                  focusedWindow == nil || current.focusedWindow == nil || focusedWindow == current.focusedWindow else {
                throw DriverError("desktop keyboard destination changed; read the desktop before input", code: .staleTarget)
            }
            if focusedWindow != nil && current.focusedWindow == nil {
                throw DriverError("the observed keyboard window cannot be confirmed; read the desktop before input", code: .inputUnavailable)
            }
        }
    }
    private var keyboards = [String: KeyboardDestination]()
    private static let background = CGColor(gray: 0.18, alpha: 1)

    static func geometry(maxDimension: Int = 4096) throws -> DesktopGeometry {
        var count: UInt32 = 0
        guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0, count <= 32 else {
            throw DriverError("active desktop displays unavailable", code: .desktopUnavailable)
        }
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
        guard CGGetActiveDisplayList(count, &ids, &count) == .success else { throw DriverError("cannot enumerate displays", code: .desktopUnavailable) }
        let displays = try ids.prefix(Int(count)).filter { CGDisplayMirrorsDisplay($0) == kCGNullDirectDisplay }.map { id in
            guard let mode = CGDisplayCopyDisplayMode(id) else { throw DriverError("display mode unavailable", code: .desktopUnavailable) }
            return DesktopGeometry.Display(id: id, frame: CGDisplayBounds(id), pixelsWide: mode.pixelWidth,
                                           pixelsHigh: mode.pixelHeight, rotation: CGDisplayRotation(id))
        }
        return try DesktopGeometry(displays, maxDimension: maxDimension)
    }

    func mapping(_ ref: String) throws -> Mapping {
        let value = try observed(ref)
        guard try Self.geometry(maxDimension: value.geometry.maxDimension) == value.geometry else { throw ObservationChanged("desktop display layout changed; read the desktop again") }
        return value
    }

    func observed(_ ref: String) throws -> Mapping {
        guard let value = mappings[ref] else { throw DriverError("desktop observation expired; read the desktop again", code: .staleTarget) }
        if let issue = value.inputIssue {
            throw DriverError(issue, code: .staleTarget,
                next: "read the desktop again before desktop input, or use an independently observed App target")
        }
        return value
    }

    static func recipient() -> NativeProcessIdentity? {
        NSWorkspace.shared.frontmostApplication.flatMap { NativeProcessIdentity.read($0.processIdentifier) }
    }

    /// Z-order alone is not keyboard focus. Use a bounded, optional focused-
    /// window attribute as additional evidence; never require an AX tree.
    static func destination() -> KeyboardDestination? {
        guard let process = recipient(),
              let raw = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
        let windows = raw.filter { ($0[kCGWindowLayer as String] as? NSNumber)?.intValue == 0 }
        let window = surfaces(windows).first { $0.pid == process.pid }?.id
        var focusedWindow: CGWindowID?
        if BackgroundTarget.isExclusive, AXIsProcessTrusted() {
            let app = AXUIElementCreateApplication(process.pid)
            AXUIElementSetMessagingTimeout(app, 0.05)
            var focused: CFTypeRef?
            if AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &focused) == .success,
               let focused, CFGetTypeID(focused) == AXUIElementGetTypeID() {
                focusedWindow = BackgroundEvents.nativeWindowID(unsafeDowncast(focused, to: AXUIElement.self))
            }
        }
        guard recipient() == process else { return nil }
        return KeyboardDestination(process: process, window: window, focusedWindow: focusedWindow)
    }

    func keyboardDestination(_ ref: String) throws -> KeyboardDestination {
        _ = try observed(ref)
        guard let expected = keyboards[ref] else {
            throw DriverError("desktop keyboard destination changed or is unknown; read the desktop before input", code: .staleTarget)
        }
        try expected.validate(Self.destination())
        return expected
    }

    /// A normal desktop interaction can focus another already observed application.
    /// A newly launched process must first appear in a new observation.
    func inputFinished(_ ref: String) {
        let current = Self.destination()
        keyboards[ref] = current.flatMap { mappings[ref]?.processes.contains($0.process) == true ? $0 : nil }
    }

    func inputFailed() { keyboards.removeAll() }

    static func surfaces(_ raw: [[String: Any]]) -> [Surface] {
        raw.compactMap { item in
            guard !AgentPointer.isDecoration(item), (item[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1 > 0,
                  let id = (item[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let pid = (item[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
                  let rect = item[kCGWindowBounds as String] as? [String: Any],
                  let frame = CGRect(dictionaryRepresentation: rect as CFDictionary), frame.width > 0, frame.height > 0 else { return nil }
            return Surface(id: id, pid: pid, frame: frame)
        }
    }

    func validateHit(_ ref: String, point: CGPoint) throws {
        let mapping = try mapping(ref)
        guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            throw DriverError("desktop window catalogue unavailable before pointer input; read the desktop again", code: .inputUnavailable)
        }
        let expected = mapping.surfaces.first { $0.frame.contains(point) }
        let current = Self.surfaces(windows).first { $0.frame.contains(point) }
        guard expected == current else { throw ObservationChanged("window at the desktop coordinates moved or changed; read the desktop again") }
        if let expected, let process = mapping.processes.first(where: { $0.pid == expected.pid }), NativeProcessIdentity.read(process.pid) != process {
            throw ObservationChanged("process at the desktop coordinates was replaced; read the desktop again")
        }
    }

    private func rectangle(_ rect: CGRect) -> [String: Double] {
        ["x": rect.minX, "y": rect.minY, "width": rect.width, "height": rect.height]
    }

    func read(maxDimension: Int = 4096) async throws -> [String: Any] {
        AgentPointer.beginDesktopCapture()
        defer { AgentPointer.endDesktopCapture() }
        try RequestDiagnostics.checkObservationBudget()
        try BackgroundTarget.validateDesktop()
        let geometry = try Self.geometry(maxDimension: maxDimension)
        let beforeRecipient = Self.recipient()
        let beforeWindows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
        let permission = CGPreflightScreenCaptureAccess()
        var failures = [CGDirectDisplayID: String](), captured = Set<CGDirectDisplayID>()
        var screenshot: CapturedImage?
        let interval = CaptureInterval()
        if permission {
            do {
                try RequestDiagnostics.checkObservationBudget()
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                try RequestDiagnostics.checkObservationBudget()
                guard let canvas = CGContext(data: nil, width: geometry.width, height: geometry.height, bitsPerComponent: 8,
                    bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
                    throw DriverError("cannot allocate desktop canvas")
                }
                canvas.setFillColor(Self.background)
                canvas.fill(CGRect(x: 0, y: 0, width: geometry.width, height: geometry.height))
                for display in geometry.displays {
                    try RequestDiagnostics.checkObservationBudget()
                    do {
                        guard let native = content.displays.first(where: { $0.displayID == display.id }), native.frame == display.frame else {
                            throw DriverError("display is absent or changed in capture service")
                        }
                        let rect = geometry.rect(display.frame)
                        let configuration = SCStreamConfiguration()
                        configuration.width = max(1, Int(ceil(rect.width)))
                        configuration.height = max(1, Int(ceil(rect.height)))
                        configuration.showsCursor = false
                        configuration.scalesToFit = true
                        // Only our click-through decoration is absent from both
                        // pixels and hit-test catalogues. Real occlusion, popups
                        // and interactive preview retain their existing policy.
                        let decorations = content.windows.filter { window in
                            AgentPointer.isDecoration(id: window.windowID,
                                pid: window.owningApplication?.processID ?? 0, title: window.title)
                        }
                        let filter = SCContentFilter(display: native, excludingWindows: decorations)
                        if #available(macOS 14.2, *) { filter.includeMenuBar = true }
                        try RequestDiagnostics.checkObservationBudget()
                        let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
                        try RequestDiagnostics.checkObservationBudget()
                        guard image.width == configuration.width, image.height == configuration.height else {
                            throw DriverError("unexpected display image dimensions")
                        }
                        canvas.draw(image, in: CGRect(x: rect.minX, y: Double(geometry.height) - rect.maxY,
                                                    width: rect.width, height: rect.height))
                        captured.insert(display.id)
                    } catch {
                        try RequestDiagnostics.rethrowCancellation(error)
                        if error is ObservationBudgetExceeded { throw error }
                        try RequestDiagnostics.checkObservationBudget()
                        failures[display.id] = String(describing: error)
                    }
                }
                if !captured.isEmpty {
                    try RequestDiagnostics.checkObservationBudget()
                    guard let image = canvas.makeImage() else { throw DriverError("cannot render desktop canvas") }
                    let png = try CapturedImage.encodePNG(image, creationFailure: "cannot encode desktop PNG",
                        finalizationFailure: "cannot encode desktop PNG")
                    screenshot = CapturedImage(png: png, width: image.width, height: image.height, interval: interval)
                }
            } catch {
                try RequestDiagnostics.rethrowCancellation(error)
                // Optional capture may exhaust Dc while the request still has
                // time to validate and return its desktop catalogue under D.
                // Never start another display capture or publish partial pixels.
                for display in geometry.displays { failures[display.id] = String(describing: error) }
                captured.removeAll()
                screenshot = nil
            }
        } else {
            for display in geometry.displays { failures[display.id] = "screen recording permission denied" }
        }
        try RequestDiagnostics.checkCancellation()
        try BackgroundTarget.validateDesktop()
        guard try Self.geometry(maxDimension: maxDimension) == geometry else { throw ObservationChanged("desktop layout changed during capture") }
        let frontmost = NSWorkspace.shared.frontmostApplication
        let rawWindows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
        let recipient = Self.recipient()
        var changes = [String]()
        if beforeRecipient != recipient { changes.append("foreground application changed") }
        if let beforeWindows, let rawWindows {
            if Self.surfaces(beforeWindows) != Self.surfaces(rawWindows) { changes.append("windows moved or changed") }
        } else { changes.append("window catalogue unavailable") }
        // Seeing a changing desktop is useful, especially for understanding a
        // covering popup. It does not prove a stable input target. Retain the
        // pixels and uncertainty, but never authorize input from this capture.
        let inputIssue = changes.isEmpty ? nil : "Desktop screenshot is observation-only: \(changes.joined(separator: "; ")) during capture. Window catalogue describes the end of capture and may differ from the image."
        let windows: [[String: Any]] = (rawWindows ?? []).compactMap { raw in
            guard !AgentPointer.isDecoration(raw), (raw[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1 > 0,
                  let rawRect = raw[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: rawRect as CFDictionary), rect.width > 0, rect.height > 0,
                  geometry.displays.contains(where: { $0.frame.intersects(rect) }) else { return nil }
            let pid = (raw[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value
            let app = pid.flatMap { NSRunningApplication(processIdentifier: $0) }
            var item: [String: Any] = ["app": String((app?.localizedName ?? raw[kCGWindowOwnerName as String] as? String ?? "Unknown application").prefix(512)),
                "title": String((raw[kCGWindowName as String] as? String ?? "").prefix(512)),
                "rect": rectangle(geometry.rect(rect.intersection(geometry.bounds)))]
            if let app, let selector = applications.selector(for: app) { item["selector"] = selector }
            return item
        }
        let displays: [[String: Any]] = geometry.displays.enumerated().map { index, display in
            var item: [String: Any] = ["name": "Display \(index + 1)", "rect": rectangle(geometry.rect(display.frame)), "captured": captured.contains(display.id)]
            if let error = failures[display.id] { item["error"] = String(error.prefix(1024)) }
            return item
        }
        var observation: [String: Any] = ["mode": BackgroundTarget.isExclusive ? "exclusive" : "shared",
            "canvas": ["width": geometry.width, "height": geometry.height], "displays": displays,
            "windows": Array(windows.prefix(256)), "omittedWindows": max(0, windows.count - 256)]
        if let frontmost {
            var app: [String: Any] = ["name": frontmost.localizedName ?? "Unknown application"]
            if let selector = applications.selector(for: frontmost) { app["selector"] = selector }
            observation["frontmost"] = app
        } else { observation["frontmost"] = NSNull() }
        var catalogueNotes = [String]()
        if let inputIssue { catalogueNotes.append(inputIssue) }
        if !permission { catalogueNotes.append("window titles may be unavailable without screen recording permission") }
        if !catalogueNotes.isEmpty { observation["catalogError"] = catalogueNotes.joined(separator: " ") }
        // Pixels and their retained mapping have one opaque identity. A
        // catalogue-only observation still has a reference but no image.
        let ref = screenshot?.id ?? UUID().uuidString
        let surfaces = Self.surfaces(rawWindows ?? [])
        var processes = Set(Set(surfaces.map(\.pid)).compactMap(NativeProcessIdentity.read))
        if let recipient { processes.insert(recipient) }
        try RequestDiagnostics.checkCancellation()
        mappings[ref] = Mapping(geometry: geometry, captured: captured, surfaces: surfaces, processes: processes, inputIssue: inputIssue)
        order.append(ref)
        while order.count > 64 {
            let expired = order.removeFirst()
            mappings.removeValue(forKey: expired); keyboards.removeValue(forKey: expired)
        }
        let destination = Self.destination()
        keyboards[ref] = inputIssue == nil && destination?.process == recipient ? destination : nil
        return ["desktopRef": ref, "scope": scope, "observation": observation,
                "screenshot": screenshot?.protocolValue(kind: .desktop, scope: scope) as Any? ?? NSNull()]
    }
}
