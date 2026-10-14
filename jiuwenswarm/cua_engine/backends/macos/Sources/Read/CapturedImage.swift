import Foundation

/// UTC milliseconds anchored once to the monotonic clock. Captures in a single
/// process remain ordered if the system wall clock is adjusted during a read.
enum CaptureClock {
    private static let origin = (wall: Date().timeIntervalSince1970 * 1000, uptime: ProcessInfo.processInfo.systemUptime)
    static func now() -> Double { origin.wall + (ProcessInfo.processInfo.systemUptime - origin.uptime) * 1000 }
}

struct CaptureInterval {
    let startedAt = CaptureClock.now()
    func observation() -> [String: Any] {
        ["id": UUID().uuidString, "startedAt": startedAt, "completedAt": CaptureClock.now()]
    }
}

/// Pixel identity and collection interval, independent of the current tree.
/// The native input mapping stays in ScreenCapture; metadata never reconstructs it.
struct CapturedImage {
    enum Kind: String { case window, overview, desktop }
    let id = UUID().uuidString
    let png: String
    let width: Int
    let height: Int
    let startedAt: Double
    let completedAt: Double

    init(png: String, width: Int, height: Int, interval: CaptureInterval) {
        self.png = png; self.width = width; self.height = height
        startedAt = interval.startedAt; completedAt = CaptureClock.now()
    }

    func protocolValue(kind: Kind, scope: String) -> [String: Any] {
        ["id": id, "kind": kind.rawValue, "scope": scope, "startedAt": startedAt, "completedAt": completedAt,
         "width": width, "height": height, "png": png]
    }
}
