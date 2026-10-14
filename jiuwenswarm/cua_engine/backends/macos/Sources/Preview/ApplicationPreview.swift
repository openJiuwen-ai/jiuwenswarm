import AppKit
@preconcurrency import ScreenCaptureKit
import CoreImage
import ImageIO
import UniformTypeIdentifiers

/// Application sharing is independent of AX, observation tokens and input routing.
/// A display-based application filter lets WindowServer include new windows and
/// hosted child dialogs automatically, with their real stacking and positions.
@MainActor
final class ApplicationPreview: NSObject, SCStreamDelegate {
    private weak var view: PreviewController?
    private var owner: NSRunningApplication?
    private var auxiliary: NSRunningApplication?
    private var task: Task<Void, Never>?
    private var generation = 0
    private var suspended = false
    private var activeStreams = Set<ObjectIdentifier>()
    private var screenObserver: NSObjectProtocol?

    init(view: PreviewController) {
        self.view = view
        super.init()
        view.onDismiss = { [weak self] in self?.stop() }
    }

    private func watchScreens() {
        guard screenObserver == nil else { return }
        screenObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in
                    guard let self, let owner = self.owner else { return }
                    self.show(owner, auxiliary: self.auxiliary, force: true)
                }
            }
    }

    func show(_ app: NSRunningApplication, auxiliary: NSRunningApplication? = nil, force: Bool = false) {
        guard !suspended, view?.isDismissed == false else { return }
        watchScreens()
        let helper = auxiliary == app ? nil : auxiliary
        guard force || owner != app || self.auxiliary != helper else { return }
        owner = app
        self.auxiliary = helper
        generation += 1
        let version = generation, previous = task
        previous?.cancel()
        let name = app.localizedName ?? app.bundleIdentifier ?? "应用"
        view?.frame(name: name, png: nil)
        task = Task { [weak self] in
            // Drain any previous stream startup/teardown before creating another.
            await previous?.value
            guard let self, !Task.isCancelled else { return }
            var streams = [SCStream]()
            do {
                guard CGPreflightScreenCaptureAccess(), !app.isTerminated else { throw CancellationError() }
                let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
                try Task.checkCancellation()
                let owners = [app, helper].compactMap { $0 }.filter { !$0.isTerminated }
                let included = content.applications.filter { candidate in owners.contains { $0.processIdentifier == candidate.processID } }
                guard included.contains(where: { $0.processID == app.processIdentifier }) else { throw CancellationError() }
                let delivery = PreviewLatestValue<String?> { [weak self] png in
                    guard let self, self.generation == version else { return }
                    self.view?.frame(name: name, png: png)
                }
                let frames = ApplicationPreviewFrames { delivery.submit($0) }
                for display in content.displays {
                    try Task.checkCancellation()
                    let filter = SCContentFilter(display: display, including: included, exceptingWindows: [])
                    if #available(macOS 14.2, *) { filter.includeMenuBar = false }
                    let config = SCStreamConfiguration()
                    let scale = min(1, 1280 / max(display.frame.width, display.frame.height))
                    config.width = max(2, Int(display.frame.width * scale))
                    config.height = max(2, Int(display.frame.height * scale))
                    config.pixelFormat = kCVPixelFormatType_32BGRA
                    // ScreenCaptureKit defaults to a transparent background.
                    config.minimumFrameInterval = CMTime(value: 1, timescale: 8)
                    config.queueDepth = 3
                    config.showsCursor = false
                    config.capturesAudio = false
                    config.ignoreShadowsDisplay = true
                    config.streamName = "Computer Use · 应用预览"
                    let stream = SCStream(filter: filter, configuration: config, delegate: self)
                    frames.register(stream, display: display.frame)
                    try stream.addStreamOutput(frames, type: .screen, sampleHandlerQueue: frames.queue)
                    streams.append(stream)
                    self.activeStreams.insert(ObjectIdentifier(stream))
                    try await stream.startCapture()
                }
                // Retain outputs, detect app exit, and stop even when no further
                // Agent reads arrive. No AX polling, activation or input here.
                while !app.isTerminated {
                    try await Task.sleep(for: .seconds(1))
                    _ = frames
                }
                if self.generation == version { self.view?.frame(name: name, png: nil) }
            } catch {
                if !Task.isCancelled, self.generation == version {
                    self.view?.frame(name: name, png: nil)
                    diagnose("application preview: \(error)")
                }
            }
            for stream in streams { self.activeStreams.remove(ObjectIdentifier(stream)); try? await stream.stopCapture() }
        }
    }

    @discardableResult func stop() -> Task<Void, Never>? {
        generation += 1
        let previous = task
        previous?.cancel()
        task = nil
        owner = nil
        auxiliary = nil
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver); self.screenObserver = nil }
        view?.reset()
        return previous
    }

    nonisolated func stream(_ stream: SCStream, didStopWithError error: any Error) {
        Task { @MainActor in
            guard self.activeStreams.contains(ObjectIdentifier(stream)) else { return }
            // Do not restart sharing after the user stops it in the system UI.
            if (error as NSError).code == SCStreamError.Code.userStopped.rawValue {
                self.suspended = true
            }
            self.stop()
            diagnose("application preview stopped: \(error)")
        }
    }
}

/// Serial frame worker. Crop only fully transparent outer pixels; the OS filter
/// decides membership. Never infer dialog ownership from AX or window titles.
/// Multiple displays retain their global relative positions in the composition.
private final class ApplicationPreviewFrames: NSObject, SCStreamOutput, @unchecked Sendable {
    let queue = DispatchQueue(label: "computer-use.application-preview", qos: .utility)
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var displays = [ObjectIdentifier: CGRect]()
    private var images = [ObjectIdentifier: (CGImage, CGRect)]()
    private var scheduled = false
    private let receive: @Sendable (String?) -> Void
    init(receive: @escaping @Sendable (String?) -> Void) { self.receive = receive }
    func register(_ stream: SCStream, display: CGRect) {
        let key = ObjectIdentifier(stream)
        queue.async { self.displays[key] = display }
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen,
              let info = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let value = info.first?[.status] as? Int, let status = SCFrameStatus(rawValue: value),
              status != .idle, status != .started else { return }
        let key = ObjectIdentifier(stream)
        guard let display = displays[key] else { return }
        images.removeValue(forKey: key)
        if status == .complete, let buffer = sampleBuffer.imageBuffer, let bounds = Self.opaqueBounds(buffer),
           let image = context.createCGImage(CIImage(cvPixelBuffer: buffer), from: CGRect(x: 0, y: 0,
                width: CVPixelBufferGetWidth(buffer), height: CVPixelBufferGetHeight(buffer)))?.cropping(to: bounds) {
            let sx = display.width / CGFloat(CVPixelBufferGetWidth(buffer)), sy = display.height / CGFloat(CVPixelBufferGetHeight(buffer))
            images[key] = (image, CGRect(x: display.minX + bounds.minX * sx, y: display.minY + bounds.minY * sy,
                                        width: bounds.width * sx, height: bounds.height * sy))
        }
        guard !scheduled else { return }
        scheduled = true
        queue.asyncAfter(deadline: .now() + 0.125) { self.scheduled = false; self.compose() }
    }

    private static func opaqueBounds(_ buffer: CVPixelBuffer) -> CGRect? {
        guard CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_32BGRA,
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let bytes = CVPixelBufferGetBaseAddress(buffer)?.assumingMemoryBound(to: UInt8.self) else { return nil }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer), stride = CVPixelBufferGetBytesPerRow(buffer)
        var left = width, top = height, right = -1, bottom = -1
        for y in 0..<height {
            let row = bytes.advanced(by: y * stride)
            var first = 0
            while first < width && row[first * 4 + 3] == 0 { first += 1 }
            guard first < width else { continue }
            var last = width - 1
            while last > first && row[last * 4 + 3] == 0 { last -= 1 }
            left = min(left, first); right = max(right, last)
            top = min(top, y); bottom = y
        }
        return right < left ? nil : CGRect(x: left, y: top, width: right - left + 1, height: bottom - top + 1)
    }
    private func compose() {
        let bounds = images.values.reduce(CGRect.null) { $0.union($1.1) }
        guard !bounds.isNull, bounds.width > 0, bounds.height > 0 else { receive(nil); return }
        let scale = min(1, 1024 / max(bounds.width, bounds.height))
        let width = max(1, Int(ceil(bounds.width * scale))), height = max(1, Int(ceil(bounds.height * scale)))
        guard let canvas = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
        for (image, rect) in images.values {
            canvas.draw(image, in: CGRect(x: (rect.minX - bounds.minX) * scale,
                y: CGFloat(height) - (rect.maxY - bounds.minY) * scale, width: rect.width * scale, height: rect.height * scale))
        }
        guard let image = canvas.makeImage() else { return }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return }
        receive(data.base64EncodedString())
    }
}
