import CoreGraphics

/// Display-only arithmetic. Coordinates are Quartz screen points until the
/// final AppKit conversion; image pixels and Retina scale never enter here.
enum AgentPointerGeometry {
    static func valid(_ rect: CGRect) -> Bool {
        [rect.minX, rect.minY, rect.width, rect.height].allSatisfy(\.isFinite)
            && rect.width > 0 && rect.height > 0
    }

    static func appKit(_ rect: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    }

    /// Keep the interaction hotspot at its original window-local position.
    /// Clip/hide the glyph at edges; never move the hotspot to fit its artwork.
    /// This position is decoration, never a coordinate mapping for input.
    static func restingPoint(offset: CGPoint, in frame: CGRect) -> CGPoint {
        CGPoint(x: frame.minX + offset.x, y: frame.minY + offset.y)
    }

}
