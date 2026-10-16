import AppKit
import ApplicationServices

/// Addressed pointer input on one retained native geometry object. A click
/// uses its control; a scroll uses Python's selected scroll area and hit member.
/// This unit neither finds an ancestor/member nor invokes a semantic AX action.
@MainActor struct NativeElementPointerUnit {
    let application: NSRunningApplication
    let window: AXUIElement
    let element: AXUIElement
    var member: AXUIElement? = nil
    var kind: NativePointerIntent.Kind = .click

    struct Geometry {
        let mapping: WindowGeometry.WindowMapping
        let frame: CGRect
        let visible: CGRect
        var point: CGPoint { CGPoint(x: visible.midX, y: visible.midY) }

        func same(as other: Geometry) -> Bool {
            mapping.id == other.mapping.id && mapping.pid == other.mapping.pid
                && mapping.frame == other.mapping.frame && frame == other.frame
        }
    }

    private func sample() throws -> (geometry: Geometry, hit: AXUIElement) {
        try RequestDiagnostics.checkCancellation()
        if AXAccess.read(element, kAXEnabledAttribute).value as? Bool == false {
            throw DriverError("the selected pointer element is disabled", code: .inputUnavailable)
        }
        let mapping = try NativeWindowUnit(application: application, window: window).mapping()
        guard let frame = WindowGeometry.hint(element)?.frame,
              frame.minX.isFinite, frame.minY.isFinite, frame.width.isFinite, frame.height.isFinite,
              frame.width > 0, frame.height > 0 else {
            throw DriverError("the selected element has no usable pointer geometry", code: .inputUnavailable)
        }
        let visible = frame.intersection(mapping.frame)
        guard !visible.isNull, visible.width > 0, visible.height > 0 else {
            throw DriverError("the selected element is outside its window", code: .inputUnavailable)
        }
        let value = Geometry(mapping: mapping, frame: frame, visible: visible)
        var hit: AXUIElement?
        let status = AXUIElementCopyElementAtPosition(AXUIElementCreateApplication(application.processIdentifier),
            Float(value.point.x), Float(value.point.y), &hit)
        guard status == .success, let hit else {
            throw DriverError("the element's native pointer hit cannot be read", code: .inputUnavailable)
        }
        try RequestDiagnostics.checkCancellation()
        return (value, hit)
    }

    func geometry() throws -> Geometry {
        let sampled = try sample()
        // A distinct member is selected by the Python semantic resolver and
        // guarded by its native fact dependencies. This unit never searches
        // children or accepts a hit merely because it lies in the rectangle.
        guard CFEqual(sampled.hit, member ?? element) else {
            throw DriverError("the element's center is not confirmed as this native hit target", code: .inputUnavailable)
        }
        return sampled.geometry
    }

    private func coveredByAnotherWindow(_ hit: AXUIElement) -> Bool {
        // AXUIElementCopyElementAtPosition takes an application, not a window;
        // Apple specifies that it resolves the application's topmost window.
        // A confirmed different owner is recoverable window occlusion. A
        // different/unknown hit within this window still needs member evidence
        // and must not be treated as permission to click its rectangle.
        guard let owner = WindowGeometry.owningWindow(hit, pid: application.processIdentifier, in: window) else { return false }
        return !CFEqual(owner, window)
    }

    func conditions() throws -> (readiness: NativeReadiness, foreground: Bool, geometry: Bool, route: Bool, hit: AXUIElement?) {
        let sampled: (geometry: Geometry, hit: AXUIElement)
        do { sampled = try sample() }
        catch let error as DriverError where error.code == .inputUnavailable || error.code == .modeUnsupported {
            return (NativeReadiness([("geometry", "unknown")]), false, false, false, nil)
        }
        guard CFEqual(sampled.hit, member ?? element) else {
            if coveredByAnotherWindow(sampled.hit) {
                return (NativeReadiness([("receiver", "no")]), false, true, false, sampled.hit)
            }
            return (NativeReadiness([("geometry", "unknown")]), false, false, false, sampled.hit)
        }
        let pointer = NativePointerUnit(application: application, window: sampled.geometry.mapping)
        let foreground = pointer.foregroundWindow(at: sampled.geometry.point), route = pointer.ready(at: sampled.geometry.point)
        var required = [("receiver", route ? "yes" : "no")]
        if BackgroundTarget.isExclusive { required.append(("foreground", foreground ? "yes" : "no")) }
        return (NativeReadiness(required), foreground, true, route, sampled.hit)
    }

    private func validate(_ selected: Geometry, check: () throws -> Void) throws {
        try check()
        guard try geometry().same(as: selected) else {
            throw DriverError("element pointer geometry changed during the sequence", code: .inputUnavailable)
        }
    }

    func prepare(foreground: Bool, check: @escaping @MainActor () throws -> Void,
                 onPreparation: @escaping (String) -> Void) async throws {
        try check()
        let sampled = try sample()
        let selected = sampled.geometry
        if !CFEqual(sampled.hit, member ?? element) {
            guard coveredByAnotherWindow(sampled.hit) else {
                throw DriverError("the element's center is not confirmed as this native hit target", code: .inputUnavailable)
            }
            guard foreground else {
                // No background primitive in this unit changes window order.
                // Record unavailability before Python may choose authorized
                // foreground preparation; no requested input has been sent.
                throw NativePreparationUnavailable(description:
                    "another window covers the element's native hit target; no pointer event sent")
            }
            // Window-directed delivery alone cannot confirm this Element's
            // hit through an overlapping document. Prepare the original window
            // under the operation's foreground permission, then sample again.
            let assist = ForegroundAssist()
            try await assist.prepare(BackgroundTarget(application), window: selected.mapping.id,
                pointerAt: selected.point, onAction: { onPreparation("foreground window activation") })
        } else {
            let pointer = NativePointerUnit(application: application, window: selected.mapping)
            try await pointer.prepare(foreground: foreground, atNative: selected.point,
                check: check, onPreparation: onPreparation)
        }
        // Preparation may move the same element or window. No requested mouse
        // event has been sent; the following inspect/delivery samples its new
        // geometry. Only an actual gesture freezes its own native coordinates.
        try check()
        _ = try geometry()
    }

    func deliver(_ request: NativePointerIntent, check: @escaping @MainActor () throws -> Void,
                 onDispatch: () throws -> Void, onPosted: () -> Void, onCleanupUnknown: @escaping () -> Void) async throws {
        guard request.kind == kind, request.start == nil, request.end == nil else {
            throw DriverError("element pointer contract requires its original addressed intent", code: .invalidRequest)
        }
        try check()
        let selected = try geometry()
        let pointer = NativePointerUnit(application: application, window: selected.mapping)
        try await pointer.deliver(request, from: selected.point, to: nil, viewport: selected.visible.size,
            check: { try validate(selected, check: check) }, onDispatch: onDispatch,
            onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
    }
}
