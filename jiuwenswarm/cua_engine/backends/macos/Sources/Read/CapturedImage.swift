import Foundation
import ImageIO
import UniformTypeIdentifiers

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
/// The native input mapping stays in the capture owner; metadata never reconstructs it.
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

    static func encodePNG(_ image: CGImage, creationFailure: String = "cannot create PNG encoder",
                          finalizationFailure: String = "PNG encoding failed") throws -> String {
        let bytes = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(bytes, UTType.png.identifier as CFString, 1, nil) else {
            throw DriverError(creationFailure)
        }
        CGImageDestinationAddImage(destination, image, nil)
        try RequestDiagnostics.checkObservationBudget()
        guard CGImageDestinationFinalize(destination) else { throw DriverError(finalizationFailure) }
        try RequestDiagnostics.checkCancellation()
        let result = (bytes as Data).base64EncodedString()
        try RequestDiagnostics.checkCancellation()
        return result
    }

    func protocolValue(kind: Kind, scope: String) -> [String: Any] {
        ["id": id, "kind": kind.rawValue, "scope": scope, "startedAt": startedAt, "completedAt": completedAt,
         "width": width, "height": height, "png": png]
    }
}
