import AppKit
import ApplicationServices

/// One original element and its known clipping ancestors. Ordinary wrapper
/// frames do not clip. A scroll region is a preparation target, never a click
/// substitute for the original element.
@MainActor struct NativeElementViewport {
    struct Exposure {
        let mapping: WindowGeometry.WindowMapping
        let region: AXUIElement
        let viewport: CGRect
        let frame: CGRect
        let direction: String
        let pages: Double
        let distance: Double
        var point: CGPoint { CGPoint(x: viewport.midX, y: viewport.midY) }
    }

    let frame: CGRect
    let clipped: CGRect
    let exposure: Exposure?

    static func sample(element: AXUIElement, window: AXUIElement, mapping: WindowGeometry.WindowMapping) throws
        -> NativeElementViewport
    {
        if AXAccess.read(element, kAXEnabledAttribute).value as? Bool == false {
            throw DriverError("the selected pointer element is disabled", code: .inputUnavailable)
        }
        guard let frame = WindowGeometry.hint(element)?.frame, frame.minX.isFinite, frame.minY.isFinite,
            frame.width.isFinite, frame.height.isFinite, frame.width > 0, frame.height > 0
        else { throw DriverError("the selected element has no usable pointer geometry", code: .inputUnavailable) }
        var regions = [(node: AXUIElement, bounds: CGRect, clips: Bool)]()
        var current = AXAccess.element(element, kAXParentAttribute)
        var seen = [AXUIElement]()
        let deadline = ProcessInfo.processInfo.systemUptime + 0.15
        while let node = current, seen.count < 12, !CFEqual(node, window),
            !seen.contains(where: { CFEqual($0, node) }), ProcessInfo.processInfo.systemUptime < deadline
        {
            try RequestDiagnostics.checkCancellation()
            seen.append(node)
            let role = AXAccess.read(node, kAXRoleAttribute).value as? String
            if [kAXScrollAreaRole, kAXListRole, kAXTableRole, kAXOutlineRole].contains(role ?? ""),
                let bounds = WindowGeometry.hint(node)?.frame, !bounds.isEmpty
            { regions.append((node, bounds, role == kAXScrollAreaRole)) }
            current = AXAccess.element(node, kAXParentAttribute)
        }
        let clipped = regions.filter(\.clips).reduce(frame.intersection(mapping.frame)) { $0.intersection($1.bounds) }
        var exposure: Exposure?
        if clipped.isNull || clipped.isEmpty {
            // If an inner region is itself outside an outer viewport, expose
            // the outer region first. Every subsequent step resamples the same
            // element and the live ancestor chain.
            for (index, region) in regions.enumerated() {
                let viewport = regions.dropFirst(index + 1).filter(\.clips).reduce(region.bounds.intersection(mapping.frame)) {
                    $0.intersection($1.bounds)
                }
                guard !viewport.isNull, !viewport.isEmpty,
                    let movement = movement(frame: frame, viewport: viewport)
                else { continue }
                exposure = Exposure(
                    mapping: mapping, region: region.node, viewport: viewport, frame: frame,
                    direction: movement.direction, pages: movement.pages, distance: movement.distance)
                break
            }
        }
        return NativeElementViewport(frame: frame, clipped: clipped, exposure: exposure)
    }

    /// Pixel geometry only; no item order, labels, time increments or toolkit
    /// assumptions. Limit one wheel to three viewports and measure progress.
    nonisolated static func movement(frame: CGRect, viewport: CGRect)
        -> (direction: String, pages: Double, distance: Double)?
    {
        guard !frame.isEmpty, !viewport.isEmpty, !frame.intersects(viewport) else { return nil }
        let dy = frame.maxY <= viewport.minY || frame.minY >= viewport.maxY ? frame.midY - viewport.midY : 0
        let dx = frame.maxX <= viewport.minX || frame.minX >= viewport.maxX ? frame.midX - viewport.midX : 0
        let vertical = abs(dy) / viewport.height >= abs(dx) / viewport.width
        let delta = vertical ? dy : dx, size = vertical ? viewport.height : viewport.width
        guard delta.isFinite, size.isFinite, size > 0, delta != 0 else { return nil }
        return (
            vertical ? (delta > 0 ? "down" : "up") : (delta > 0 ? "right" : "left"),
            min(3, max(1 / size, abs(delta) / size)), abs(delta))
    }
}
