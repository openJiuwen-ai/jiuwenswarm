import AppKit
import ApplicationServices

/// Addressed pointer input on one retained native geometry object. A click
/// uses its control; a scroll uses Python's selected scroll area and hit member.
/// Scroll preparation exposes this same object; it never selects a replacement.
@MainActor struct NativeElementPointerUnit {
    let application: NSRunningApplication
    let window: AXUIElement
    let element: AXUIElement
    var member: AXUIElement? = nil
    var kind: NativePointerIntent.Kind = .click
    var logicalWindow: AXUIElement? = nil

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

    func viewport() throws -> NativeElementViewport {
        if let logicalWindow, !CFEqual(logicalWindow, window) {
            guard let current = WindowGeometry.owningWindow(element, pid: application.processIdentifier, in: window),
                  CFEqual(current, window) else {
                throw DriverError("the original popup element changed its physical window", code: .staleTarget)
            }
        }
        let mapping = try NativeWindowUnit(application: application, window: window).mapping()
        return try NativeElementViewport.sample(element: element, window: window, mapping: mapping)
    }

    func exposure() throws -> NativeElementViewport.Exposure? { try viewport().exposure }

    private func sample() throws -> (geometry: Geometry, hit: AXUIElement) {
        try RequestDiagnostics.checkCancellation()
        let mapping = try NativeWindowUnit(application: application, window: window).mapping()
        let viewport = try viewport()
        let frame = viewport.frame
        let visible = kind == .click ? viewport.clipped : frame.intersection(mapping.frame)
        guard !visible.isNull, visible.width > 0, visible.height > 0 else {
            throw DriverError("the selected element is outside its window or known scrolling bounds", code: .inputUnavailable)
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

    func conditions() throws -> (readiness: NativeReadiness, foreground: Bool, geometry: Bool, route: Bool, hit: AXUIElement?, reason: String?) {
        let sampled: (geometry: Geometry, hit: AXUIElement)
        do {
            if kind == .click, let exposure = try exposure() {
                let pointer = NativePointerUnit(application: application, window: exposure.mapping)
                return (NativeReadiness([("receiver", "no")]), pointer.foregroundWindow(at: exposure.point),
                        true, false, nil, "The original element needs to be scrolled into view.")
            }
            sampled = try sample()
        }
        catch let error as DriverError where error.code == .inputUnavailable || error.code == .modeUnsupported {
            return (NativeReadiness([("geometry", "unknown")]), false, false, false, nil, error.description)
        }
        guard CFEqual(sampled.hit, member ?? element) else {
            if coveredByAnotherWindow(sampled.hit) {
                return (NativeReadiness([("receiver", "no")]), false, true, false, sampled.hit,
                        "Another window covers the element's native hit target.")
            }
            return (NativeReadiness([("geometry", "unknown")]), false, false, false, sampled.hit,
                    "The element's center is not confirmed as this native hit target.")
        }
        let pointer = NativePointerUnit(application: application, window: sampled.geometry.mapping)
        let foreground = pointer.foregroundWindow(at: sampled.geometry.point), route = pointer.ready(at: sampled.geometry.point)
        var required = [("receiver", route ? "yes" : "no")]
        if BackgroundTarget.isExclusive { required.append(("foreground", foreground ? "yes" : "no")) }
        return (NativeReadiness(required), foreground, true, route, sampled.hit, nil)
    }

    private func validate(_ selected: Geometry, check: () throws -> Void) throws {
        try check()
        guard try geometry().same(as: selected) else {
            throw DriverError("element pointer geometry changed during the sequence", code: .inputUnavailable)
        }
    }

    func prepare(foreground: Bool, check: @escaping @MainActor () throws -> Void,
                 onPreparation: @escaping @MainActor (String) -> Void,
                 onContentPreparation: @escaping @MainActor () throws -> Void,
                 onCleanupUnknown: @escaping @MainActor () -> Void) async throws {
        try check()
        if kind == .click, let exposure = try exposure() {
            let pointer = NativePointerUnit(application: application, window: exposure.mapping)
            try await pointer.prepare(foreground: foreground, atNative: exposure.point,
                check: check, onPreparation: onPreparation)
            try await expose(check: check, onPreparation: onPreparation,
                onContentPreparation: onContentPreparation, onCleanupUnknown: onCleanupUnknown)
        }
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

    /// Preparation has its own bounded wheel loop, using the same input route
    /// and live target checks as the requested click. It never clicks a proxy.
    private func expose(check: @escaping @MainActor () throws -> Void,
                        onPreparation: @escaping @MainActor (String) -> Void,
                        onContentPreparation: @escaping @MainActor () throws -> Void,
                        onCleanupUnknown: @escaping @MainActor () -> Void) async throws {
        let deadline = min(RequestDiagnostics.deadline ?? .infinity, ProcessInfo.processInfo.systemUptime + 3)
        var steps = 0
        while let exposure = try exposure() {
            try check()
            guard steps < 8, ProcessInfo.processInfo.systemUptime < deadline else {
                throw DriverError("the original element could not be scrolled into view within the click budget", code: .inputUnavailable)
            }
            let pointer = NativePointerUnit(application: application, window: exposure.mapping)
            let request = try NativePointerIntent(unit: "pointer_sequence", kind: "scroll",
                parameters: ["direction": exposure.direction, "pages": exposure.pages], foreground: false, element: true)
            var entered = false
            try await pointer.deliver(request, from: exposure.point, to: nil, viewport: exposure.viewport.size,
                check: check, onDispatch: {
                    if !entered {
                        try onContentPreparation()
                        entered = true
                        onPreparation("scroll original element into view")
                    }
                }, onPosted: {}, onCleanupUnknown: onCleanupUnknown)
            let waitUntil = min(deadline, ProcessInfo.processInfo.systemUptime + 0.4)
            var frame = exposure.frame, changedAt = ProcessInfo.processInfo.systemUptime
            var settled = false
            repeat {
                try check()
                let sampled = try viewport(), now = ProcessInfo.processInfo.systemUptime
                if sampled.frame != frame { frame = sampled.frame; changedAt = now }
                let progressed = sampled.exposure.map {
                    !CFEqual($0.region, exposure.region) || $0.distance < exposure.distance - 0.5
                } ?? true
                settled = progressed && now - changedAt >= 0.06
                if settled { break }
                try await Task.sleep(for: .milliseconds(20))
            } while ProcessInfo.processInfo.systemUptime < waitUntil
            guard settled else {
                throw DriverError("the original element's scroll region did not settle after moving toward the target", code: .inputUnavailable)
            }
            steps += 1
        }
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
