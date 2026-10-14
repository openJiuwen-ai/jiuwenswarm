import AppKit
@preconcurrency import ScreenCaptureKit
import CoreMedia

/// Session-owned, real ScreenCaptureKit streams make macOS show its sharing UI.
/// Frames are discarded: these streams never replace observed screenshots or
/// coordinate mappings, record files, move focus, or include other windows.
@MainActor
final class WindowSharing: NSObject, SCStreamDelegate, SCStreamOutput {
    private struct Entry { let stream: SCStream; let owner: NSRunningApplication }
    private var windows: [CGWindowID: Entry] = [:]
    private let queue = DispatchQueue(label: "computer-use.sharing-frames")
    private var stoppedByUser = false
    private var closing = false
    var onUserStop: (() -> Void)?

    func checkAvailable() throws {
        if stoppedByUser {
            throw DriverError("window sharing was stopped by the user; this execution session is suspended",
                code: .desktopUnavailable, next: "ask whether to resume; if authorized, call computer_use_reset and read the app again")
        }
    }

    func include(_ window: SCWindow) async throws {
        try checkAvailable()
        guard !closing else { throw CancellationError() }
        guard let pid = window.owningApplication?.processID, let owner = NSRunningApplication(processIdentifier: pid), !owner.isTerminated else {
            throw ObservationChanged("capture indicator target exited")
        }
        if let entry = windows[window.windowID], entry.owner == owner, !entry.owner.isTerminated { return }
        if let previous = windows.removeValue(forKey: window.windowID) { try await previous.stream.stopCapture() }
        let config = SCStreamConfiguration()
        // A low-rate preview is enough for the system sharing UI. Full-resolution
        // screenshots continue through the existing one-shot observation route.
        let scale = min(1, 640 / max(window.frame.width, window.frame.height))
        config.width = max(2, Int(window.frame.width * scale))
        config.height = max(2, Int(window.frame.height * scale))
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 3
        config.showsCursor = false
        config.capturesAudio = false
        config.streamName = "Computer Use"
        if #available(macOS 14.2, *) { config.includeChildWindows = false }
        let stream = SCStream(filter: SCContentFilter(desktopIndependentWindow: window), configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        windows[window.windowID] = Entry(stream: stream, owner: owner)
        do {
            try await stream.startCapture()
            diagnose(["kind": "window_sharing_started", "window": window.windowID, "pid": pid])
        } catch {
            windows.removeValue(forKey: window.windowID)
            throw DriverError("system capture indicator could not start: \(error)")
        }
    }

    func stop() async {
        closing = true
        let entries = windows
        windows.removeAll()
        for (id, entry) in entries {
            do {
                try await entry.stream.stopCapture()
                diagnose(["kind": "window_sharing_stopped", "window": id])
            } catch {
                // Shutdown still exits the owning native process. MCP waits for
                // that exit instead of claiming a failed stop completed.
                diagnose(["kind": "window_sharing_stop_failed", "window": id, "error": String(describing: error)])
            }
        }
    }

    nonisolated func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {}

    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        Task { @MainActor in
            guard let id = self.windows.first(where: { $0.value.stream === stream })?.key else { return }
            self.windows.removeValue(forKey: id)
            let nsError = error as NSError
            diagnose(["kind": "window_sharing_interrupted", "window": id, "error": nsError.localizedDescription, "code": nsError.code])
            if nsError.domain == SCStreamErrorDomain && nsError.code == SCStreamError.Code.userStopped.rawValue {
                self.stoppedByUser = true
                self.onUserStop?()
                await self.stop()
            }
        }
    }
}
