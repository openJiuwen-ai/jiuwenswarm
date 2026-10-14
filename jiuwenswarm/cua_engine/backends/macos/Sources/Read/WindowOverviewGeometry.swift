import Foundation
import CoreGraphics

/// Describes image coverage only; never establishes an input coordinate mapping.
enum WindowOverviewGeometry {
    enum Coverage: Equatable { case full, clipped, omitted }

    /// Subtract actual display regions instead of their bounding rectangle:
    /// a window can be inside that rectangle yet wholly in a gap between screens.
    /// Subtraction also handles overlapping/mirrored displays without double counting.
    static func coverage(of frame: CGRect, regions: [CGRect]) -> Coverage {
        guard usable(frame) else { return .omitted }
        var remaining = [frame]
        var intersects = false
        for region in regions where usable(region) {
            remaining = remaining.flatMap { part -> [CGRect] in
                let overlap = part.intersection(region)
                guard usable(overlap) else { return [part] }
                intersects = true
                return [
                    CGRect(x: part.minX, y: part.minY, width: part.width, height: overlap.minY - part.minY),
                    CGRect(x: part.minX, y: overlap.maxY, width: part.width, height: part.maxY - overlap.maxY),
                    CGRect(x: part.minX, y: overlap.minY, width: overlap.minX - part.minX, height: overlap.height),
                    CGRect(x: overlap.maxX, y: overlap.minY, width: part.maxX - overlap.maxX, height: overlap.height)
                ].filter(usable)
            }
            if remaining.isEmpty { return .full }
        }
        return intersects ? .clipped : .omitted
    }

    static func description(ref: String, frame: CGRect, bounds: CGRect, scale: Double,
                            regions: [CGRect], available: Bool) -> String {
        let values = [frame.minX - bounds.minX, frame.minY - bounds.minY, frame.width, frame.height]
            .map { String(Int(($0 * scale).rounded())) }.joined(separator: ",")
        let status: String
        if !available {
            status = "omitted: capture source unavailable"
        } else {
            switch coverage(of: frame, regions: regions) {
            case .full: status = "display coverage: full"
            case .clipped: status = "clipped: only the part on captured displays is included"
            case .omitted: status = "omitted: outside captured displays"
            }
        }
        return "\(ref)=(\(values)) [\(status)]"
    }

    private static func usable(_ rect: CGRect) -> Bool {
        !rect.isNull && [rect.minX, rect.minY, rect.width, rect.height].allSatisfy(\.isFinite)
            && rect.width > 0 && rect.height > 0
    }
}
