import Foundation
import CoreGraphics

/// Immutable pixel-to-desktop transform. No AX/window assumptions: holes,
/// missing captures and overlapping displays are not input destinations.
struct DesktopGeometry: Equatable {
    struct Display: Equatable {
        let id: CGDirectDisplayID
        let frame: CGRect
        let pixelsWide: Int
        let pixelsHigh: Int
        let rotation: Double
    }
    struct Invalid: ClassifiedFailure, CustomStringConvertible {
        let code: FailureCode
        let description: String
        init(_ message: String, code: FailureCode = .invalidRequest) { description = message; self.code = code }
    }
    let displays: [Display]
    let bounds: CGRect
    let width: Int
    let height: Int
    let maxDimension: Int

    init(_ displays: [Display], maxDimension: Int = 4096) throws {
        guard (1...4096).contains(maxDimension) else { throw Invalid("invalid desktop image dimension budget") }
        guard !displays.isEmpty, displays.count <= 32, Set(displays.map(\.id)).count == displays.count,
              displays.allSatisfy({ d in
                  [d.frame.minX, d.frame.minY, d.frame.width, d.frame.height, d.rotation].allSatisfy { $0.isFinite }
                    && d.frame.width > 0 && d.frame.height > 0 && d.pixelsWide > 0 && d.pixelsHigh > 0
              }) else { throw Invalid("desktop display geometry is unavailable", code: .desktopUnavailable) }
        self.displays = displays.sorted { $0.id < $1.id }
        self.maxDimension = maxDimension
        bounds = displays.reduce(CGRect.null) { $0.union($1.frame) }
        guard bounds.width.isFinite, bounds.height.isFinite else { throw Invalid("desktop extent is invalid", code: .desktopUnavailable) }
        let scale = min(1, Double(maxDimension) / max(bounds.width, bounds.height))
        width = min(maxDimension, max(1, Int(ceil(bounds.width * scale))))
        height = min(maxDimension, max(1, Int(ceil(bounds.height * scale))))
    }

    /// PNG coordinates have top-left origin; Quartz desktop coordinates may be negative.
    func rect(_ desktop: CGRect) -> CGRect {
        CGRect(x: (desktop.minX - bounds.minX) * Double(width) / bounds.width,
               y: (desktop.minY - bounds.minY) * Double(height) / bounds.height,
               width: desktop.width * Double(width) / bounds.width,
               height: desktop.height * Double(height) / bounds.height)
    }

    func point(x: Double, y: Double, captured: Set<CGDirectDisplayID>) throws -> CGPoint {
        guard x.isFinite, y.isFinite, x >= 0, y >= 0, x < Double(width), y < Double(height) else {
            throw Invalid("coordinates are outside the desktop PNG; read the desktop and use its pixels")
        }
        let point = CGPoint(x: bounds.minX + x * bounds.width / Double(width),
                            y: bounds.minY + y * bounds.height / Double(height))
        let matches = displays.filter { d in
            point.x >= d.frame.minX && point.x < d.frame.maxX && point.y >= d.frame.minY && point.y < d.frame.maxY
        }
        guard matches.count == 1, captured.contains(matches[0].id) else {
            throw Invalid("coordinates are in an uncaptured, overlapping or empty desktop region; read the desktop and choose a captured display")
        }
        return point
    }

    /// Reject the whole drag before mouse-down if any positive-length section
    /// crosses a display gap or overlapping coordinate spaces.
    func validatePath(from: CGPoint, to: CGPoint, captured: Set<CGDirectDisplayID>) throws {
        var intervals = [(Double, Double)]()
        for display in displays where captured.contains(display.id) {
            var low = 0.0, high = 1.0, valid = true
            for (start, end, minimum, maximum) in [(from.x, to.x, display.frame.minX, display.frame.maxX),
                                                   (from.y, to.y, display.frame.minY, display.frame.maxY)] {
                let delta = end - start
                if delta == 0 { if start < minimum || start >= maximum { valid = false }; continue }
                let a = (minimum - start) / delta, b = (maximum - start) / delta
                low = max(low, min(a, b)); high = min(high, max(a, b))
            }
            if valid && high > low { intervals.append((low, high)) }
        }
        var covered = 0.0
        for (low, high) in intervals.sorted(by: { $0.0 < $1.0 }) {
            guard abs(low - covered) < 1e-9 else { throw Invalid("drag crosses an uncaptured, overlapping or empty display region; choose a path on captured displays") }
            covered = high
        }
        guard abs(covered - 1) < 1e-9 else { throw Invalid("drag path leaves the captured desktop") }
    }
}
