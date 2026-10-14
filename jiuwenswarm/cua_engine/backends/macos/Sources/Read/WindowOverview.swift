import AppKit
@preconcurrency import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers

/// A display-layout observation of confirmed App windows. This image never
/// creates or changes an input mapping; precise coordinates belong to app.window(ref).read().
@MainActor enum WindowOverview {
    // SCStreamConfiguration.backgroundColor is unowned(unsafe); keep this alive
    // throughout asynchronous capture rather than assigning a temporary CGColor.
    private static let background = CGColor(gray: 0.18, alpha: 1)
    static func capture(_ owner: NSRunningApplication,
                        windows: [(ref: String, native: NativeWindows.Window)]) async throws -> (image: CapturedImage, text: String)? {
        try RequestDiagnostics.checkObservationBudget()
        guard windows.count > 1 else { return nil }
        let metric = RequestDiagnostics.metrics?.start(.overviewCapture)
        defer { metric?.end() }
        guard CGPreflightScreenCaptureAccess() else { throw DriverError("screen recording permission denied") }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        try RequestDiagnostics.checkObservationBudget()
        let validation = ScreenCapture()
        func validate() throws {
            for item in windows {
                try RequestDiagnostics.checkObservationBudget()
                let app = item.native.application
                try BackgroundTarget(app, owner: app.processIdentifier == owner.processIdentifier ? nil : owner).validate()
                try validation.validate(item.native.mapping)
            }
        }
        try validate()
        try RequestDiagnostics.checkObservationBudget()
        let included = windows.compactMap { item in
            content.windows.first { $0.windowID == item.native.mapping.id
                && $0.owningApplication?.processID == item.native.process.pid
                && WindowSelection.sameFrame($0.frame, item.native.mapping.frame) }
        }
        let missing = windows.filter { item in !included.contains { $0.windowID == item.native.mapping.id } }.map(\.ref)
        guard !included.isEmpty else { throw DriverError("no windows available for overview") }
        let windowBounds = windows.reduce(CGRect.null) { $0.union($1.native.mapping.frame) }.integral
        let displayBounds = content.displays.reduce(CGRect.null) { $0.union($1.frame) }
        let bounds = windowBounds.insetBy(dx: -8, dy: -8).intersection(displayBounds).integral
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0 else { throw DriverError("application windows are outside the available displays") }
        // The overview is a locator, not a replacement for full-resolution window pixels.
        let scale = min(1, 1600 / max(bounds.width, bounds.height))
        let width = max(1, Int((bounds.width * scale).rounded(.up)))
        let height = max(1, Int((bounds.height * scale).rounded(.up)))
        guard let canvas = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw DriverError("cannot allocate window overview")
        }
        canvas.setFillColor(background)
        canvas.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let interval = CaptureInterval()
        var capturedRegions = [CGRect]()
        for display in content.displays {
            try RequestDiagnostics.checkObservationBudget()
            let region = bounds.intersection(display.frame)
            guard !region.isNull, region.width > 0, region.height > 0,
                  included.contains(where: { $0.frame.intersects(region) }) else { continue }
            let configuration = SCStreamConfiguration()
            configuration.width = max(1, Int((region.width * scale).rounded(.up)))
            configuration.height = max(1, Int((region.height * scale).rounded(.up)))
            configuration.sourceRect = region.offsetBy(dx: -display.frame.minX, dy: -display.frame.minY)
            configuration.showsCursor = false
            configuration.scalesToFit = true
            configuration.ignoreGlobalClipDisplay = true
            configuration.backgroundColor = background
            if #available(macOS 14.2, *) { configuration.includeChildWindows = true }
            // Explicitly include all discovered popups/helpers; no other app's
            // foreground window or desktop content is added to the filter.
            let filter = SCContentFilter(display: display, including: included)
            if #available(macOS 14.2, *) { filter.includeMenuBar = false }
            try RequestDiagnostics.checkObservationBudget()
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
            try RequestDiagnostics.checkObservationBudget()
            let destination = CGRect(x: (region.minX - bounds.minX) * scale,
                y: (bounds.maxY - region.maxY) * scale, width: region.width * scale, height: region.height * scale)
            canvas.draw(image, in: destination)
            capturedRegions.append(region)
        }
        try validate()
        try RequestDiagnostics.checkObservationBudget()
        guard let image = canvas.makeImage() else { throw DriverError("cannot render window overview") }
        let data = NSMutableData()
        guard let output = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else {
            throw DriverError("cannot encode window overview")
        }
        CGImageDestinationAddImage(output, image, nil)
        try RequestDiagnostics.checkObservationBudget()
        guard CGImageDestinationFinalize(output) else { throw DriverError("cannot encode window overview") }
        try RequestDiagnostics.checkCancellation()
        let png = (data as Data).base64EncodedString()
        try RequestDiagnostics.checkCancellation()
        let layout = windows.map { item in
            WindowOverviewGeometry.description(ref: item.ref, frame: item.native.mapping.frame,
                bounds: bounds, scale: scale, regions: capturedRegions,
                available: included.contains { $0.windowID == item.native.mapping.id })
        }.joined(separator: "; ")
        var text = "Overview: app layout \(width)x\(height) px; context only; use app.window(ref).read() for click coordinates."
        if !missing.isEmpty { text += " Missing image content for windows: " + missing.joined(separator: ", ") + "." }
        // WindowServer enumeration and ScreenCaptureKit's filtered composition
        // can order attached/background windows differently. These are location
        // rectangles, not evidence of stacking or native ownership.
        text += "\nOverview windows (x,y,width,height): " + layout
        text += "\nRectangles are full window bounds in overview pixels and may extend outside the image. Display coverage excludes screen gaps; it does not prove visibility through overlapping windows."
        try RequestDiagnostics.checkCancellation()
        return (CapturedImage(png: png, width: width, height: height, interval: interval), text)
    }
}
