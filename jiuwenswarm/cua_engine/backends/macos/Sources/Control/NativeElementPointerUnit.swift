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
        // The window/known scroll-ancestor intersection, not a claim that all
        // pixels are visible. AX hit evidence owns coverage at the chosen point.
        let visible: CGRect
        let candidate: NativeElementHitSelection.Candidate
        var point: CGPoint { candidate.point }

        func same(as other: Geometry) -> Bool {
            mapping.id == other.mapping.id && mapping.pid == other.mapping.pid
                && mapping.frame == other.mapping.frame && frame == other.frame
                && point == other.point
        }
    }

    private struct Sample {
        let geometry: Geometry
        let selection: NativeElementHitSelection.Selection<AXUIElement>
        var hit: AXUIElement? { selection.observation.hit }
        var accepted: Bool { selection.basis != .rejected }
        var covered: Bool { selection.observation.evidence.relation == .otherWindow }
    }

    private func activationPoint() -> CGPoint? {
        guard let raw = AXAccess.read(member ?? element, "AXActivationPoint").value,
              CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
        var point = CGPoint.zero
        // Treat the client-provided AXValue as a point hint, without guessing
        // an AppKit coordinate transform. An exact AX hit must verify the hint.
        return AXValueGetValue(unsafeDowncast(raw, to: AXValue.self), .cgPoint, &point) ? point : nil
    }

    func viewport() throws -> NativeElementViewport {
        try validatePhysicalWindow()
        let mapping = try NativeWindowUnit(application: application, window: window).mapping()
        return try NativeElementViewport.sample(element: element, window: window, mapping: mapping)
    }

    func exposure() throws -> NativeElementViewport.Exposure? { try viewport().exposure }

    private func validatePhysicalWindow() throws {
        if let logicalWindow, !CFEqual(logicalWindow, window) {
            guard let current = WindowGeometry.owningWindow(element, pid: application.processIdentifier, in: window),
                CFEqual(current, window)
            else { throw DriverError("the original popup element changed its physical window", code: .staleTarget) }
        }
    }

    private func hasCurrentParentPathToWindow(until deadline: TimeInterval) throws -> Bool {
        try NativeElementHitSelection.reachesWindow(member ?? element, window: window,
            same: { CFEqual($0, $1) }, parent: { AXAccess.element($0, kAXParentAttribute) },
            isSurface: { node in
                guard let role = AXAccess.read(node, kAXRoleAttribute).value as? String else { return true }
                return [kAXWindowRole, kAXSheetRole, "AXPopover"].contains(role)
            }, canContinue: {
                try RequestDiagnostics.checkCancellation()
                return ProcessInfo.processInfo.systemUptime < deadline
            })
    }

    private func sample(frozen: Geometry? = nil) throws -> Sample {
        try RequestDiagnostics.checkCancellation()
        try validatePhysicalWindow()
        let mapping = try NativeWindowUnit(application: application, window: window).mapping()
        let viewport = try NativeElementViewport.sample(element: element, window: window, mapping: mapping)
        let frame = viewport.frame
        let deadline = ProcessInfo.processInfo.systemUptime + 0.15
        // Scroll's element is the selected wheel region, not the requested
        // click control. Preserve its center and viewport for the public hit
        // refinement contract; click-specific search must not alter pages.
        let expand = kind == .click
        let visible = expand ? viewport.clipped : frame.intersection(mapping.frame)
        guard !visible.isNull, visible.width > 0, visible.height > 0 else {
            throw DriverError("the selected element is outside its window or known scrolling bounds", code: .inputUnavailable)
        }
        let candidates: [NativeElementHitSelection.Candidate]
        if let frozen {
            guard visible.contains(frozen.point) else {
                throw DriverError("element pointer geometry changed during the sequence", code: .inputUnavailable)
            }
            candidates = [frozen.candidate]
        } else {
            candidates = NativeElementHitSelection.candidates(bounds: visible,
                activationPoint: expand ? activationPoint() : nil, expand: expand)
        }
        let pointer = NativePointerUnit(application: application, window: mapping, nativeWindow: window)
        let root = AXUIElementCreateApplication(application.processIdentifier)
        var boundedRoot: AXUIElement?
        var observations = [[String: Any]]()
        let selected = try NativeElementHitSelection.select(candidates, exclusive: BackgroundTarget.isExclusive,
            canContinue: { candidate in
                try RequestDiagnostics.checkCancellation()
                return frozen != nil || candidate.source == .center || ProcessInfo.processInfo.systemUptime < deadline
            }, query: { candidate in
                let queryRoot: AXUIElement
                if let timeout = candidate.queryTimeout {
                    if let boundedRoot { queryRoot = boundedRoot }
                    else {
                        let additional = AXUIElementCreateApplication(application.processIdentifier)
                        AXUIElementSetMessagingTimeout(additional, timeout)
                        boundedRoot = additional; queryRoot = additional
                    }
                } else { queryRoot = root }
                // Apple scopes messaging timeout to this exact AX object, not
                // equal objects. A separate root preserves the original center
                // route without relying on successful timeout restoration.
                var hit: AXUIElement?
                let status = AXUIElementCopyElementAtPosition(queryRoot, Float(candidate.point.x), Float(candidate.point.y), &hit)
                try RequestDiagnostics.checkCancellation()
                var relation: NativeElementHitEvidence.Relation?
                if status == .success, let hit {
                    if CFEqual(hit, member ?? element) { relation = .target }
                    else if let owner = WindowGeometry.owningWindow(hit, pid: application.processIdentifier, in: window) {
                        relation = CFEqual(owner, window) ? .otherControl : .otherWindow
                    } else { relation = .unknownOwner }
                } else { relation = nil }
                var systemStatus: AXError?, systemWindow = false, coarseForeground = false
                var systemRelation: String?, systemHitPID: pid_t?
                // A provider can return its original window without descending
                // into an exposed child. This is not a known sibling hit. Try
                // the independent system-wide query only for that exact case;
                // normal exact hits retain their original query and contract.
                if expand, candidate.source == .center, status == .success,
                   let current = hit, CFEqual(current, window),
                   try hasCurrentParentPathToWindow(until: deadline),
                   pointer.foregroundWindow(at: candidate.point) {
                    let system = AXUIElementCreateSystemWide()
                    // Setting timeout on a system-wide object changes this
                    // process's GLOBAL AX timeout. Keep the existing policy;
                    // the original request deadline is checked around the read.
                    try RequestDiagnostics.checkCancellation()
                    var systemHit: AXUIElement?
                    let queried = AXUIElementCopyElementAtPosition(system, Float(candidate.point.x),
                        Float(candidate.point.y), &systemHit)
                    systemStatus = queried
                    systemRelation = NativeElementHitEvidence.result(queried, relation: nil).relation.rawValue
                    try RequestDiagnostics.checkCancellation()
                    if queried == .success, let systemHit {
                        if CFEqual(systemHit, member ?? element) {
                            systemRelation = "target"
                            hit = systemHit; relation = .target
                        } else if CFEqual(systemHit, window) {
                            systemRelation = "originalWindow"
                            systemWindow = true
                            // The geometry attempt still needs the physical
                            // original foreground window after the AX query.
                            coarseForeground = pointer.foregroundWindow(at: candidate.point)
                            relation = .coarseAncestorWindow
                        } else {
                            systemRelation = "differentElement"
                            if WindowGeometry.inputDiagnosticsEnabled {
                                var pid: pid_t = 0
                                if AXUIElementGetPid(systemHit, &pid) == .success {
                                    systemHitPID = pid
                                    if pid != application.processIdentifier { systemRelation = "otherApplication" }
                                }
                            }
                        }
                    }
                    // All other system results preserve the original rejected
                    // mismatch. They cannot enter background geometry fallback.
                }
                let evidence = NativeElementHitEvidence.result(status, relation: relation)
                if WindowGeometry.inputDiagnosticsEnabled {
                    var observation: [String: Any] = ["source": candidate.source.rawValue,
                        "point": [candidate.point.x, candidate.point.y], "axStatus": status.rawValue,
                        "relation": evidence.relation.rawValue]
                    if let systemStatus {
                        observation["systemWideStatus"] = systemStatus.rawValue
                        observation["systemWideOriginalWindow"] = systemWindow
                        observation["systemWideRelation"] = systemRelation
                        if let systemHitPID { observation["systemWideHitPID"] = systemHitPID }
                    }
                    observations.append(observation)
                }
                // The coarse ancestor case above and existing background
                // geometry fallback use physical-window evidence. Exact hits
                // and ordinary rejected points add no such query here; every
                // delivery route still checks its final window later.
                let foreground = evidence.relation == .coarseAncestorWindow ? coarseForeground
                    : !BackgroundTarget.isExclusive && candidate.permitsGeometryFallback
                    && evidence.permitsBackgroundGeometry ? pointer.foregroundWindow(at: candidate.point) : true
                return .init(evidence: evidence, hit: status == .success ? hit : nil, foreground: foreground)
            })
        guard let selected else {
            throw DriverError("the selected element has no usable pointer point", code: .inputUnavailable)
        }
        if WindowGeometry.inputDiagnosticsEnabled {
            diagnose(["kind": "element_pointer_hit", "window": mapping.id,
                "frozen": frozen != nil, "basis": selected.basis.rawValue,
                "selectedPoint": [selected.candidate.point.x, selected.candidate.point.y],
                "candidates": observations])
        }
        try RequestDiagnostics.checkCancellation()
        return Sample(geometry: Geometry(mapping: mapping, frame: frame, visible: visible, candidate: selected.candidate),
            selection: selected)
    }

    func geometry() throws -> Geometry {
        let sampled = try sample()
        // A distinct member is selected by the Python semantic resolver and
        // guarded by its native fact dependencies. This unit never searches
        // children or accepts a hit merely because it lies in the rectangle.
        guard sampled.accepted else {
            throw DriverError("no sampled pointer point belongs to this native target or its available window route", code: .inputUnavailable)
        }
        return sampled.geometry
    }

    /// Query projection keeps a mismatching native hit visible so the public
    /// resolver can prove an existing control-part relationship. It grants no
    /// permission to dispatch to that hit or to the owner's rectangle.
    func conditions() throws -> (readiness: NativeReadiness, foreground: Bool, geometry: Bool, route: Bool, hit: AXUIElement?, reason: String?) {
        let sampled: Sample
        do {
            if kind == .click, let exposure = try exposure() {
                let pointer = NativePointerUnit(application: application, window: exposure.mapping, nativeWindow: window)
                return (NativeReadiness([("receiver", "no")]), pointer.foregroundWindow(at: exposure.point),
                        true, false, nil, "The original element needs to be scrolled into view.")
            }
            sampled = try sample()
        }
        catch let error as DriverError where error.code == .inputUnavailable || error.code == .modeUnsupported {
            return (NativeReadiness([("geometry", "unknown")]), false, false, false, nil, error.description)
        }
        guard sampled.accepted else {
            if sampled.covered {
                return (NativeReadiness([("receiver", "no")]), false, true, false, sampled.hit,
                        "Another window covers the element's native hit target.")
            }
            return (NativeReadiness([("geometry", "unknown")]), false, false, false, sampled.hit,
                    "The element's center is not confirmed as this native hit target.")
        }
        let pointer = NativePointerUnit(application: application, window: sampled.geometry.mapping, nativeWindow: window)
        let foreground = pointer.foregroundWindow(at: sampled.geometry.point), route = pointer.ready(at: sampled.geometry.point, kind: kind)
        var required = [("receiver", route ? "yes" : "no")]
        if BackgroundTarget.isExclusive { required.append(("foreground", foreground ? "yes" : "no")) }
        return (NativeReadiness(required), foreground, true, route, sampled.hit, nil)
    }

    private func validate(_ selected: Geometry, check: () throws -> Void) throws {
        try check()
        let sampled = try sample(frozen: selected)
        guard sampled.accepted, sampled.geometry.same(as: selected) else {
            throw DriverError("element pointer geometry changed during the sequence", code: .inputUnavailable)
        }
    }

    func prepare(foreground: Bool, check: @escaping @MainActor () throws -> Void,
                 onPreparation: @escaping @MainActor (String) -> Void,
                 onContentPreparation: @escaping @MainActor () throws -> Void,
                 onCleanupUnknown: @escaping @MainActor () -> Void) async throws {
        try check()
        if kind == .click, let exposure = try exposure() {
            let pointer = NativePointerUnit(application: application, window: exposure.mapping, nativeWindow: window)
            try await pointer.prepare(foreground: foreground, atNative: exposure.point, kind: .scroll,
                check: check, onPreparation: onPreparation, onCleanupUnknown: onCleanupUnknown)
            try await expose(check: check, onPreparation: onPreparation,
                onContentPreparation: onContentPreparation, onCleanupUnknown: onCleanupUnknown)
        }
        let sampled = try sample()
        let selected = sampled.geometry
        if !sampled.accepted {
            guard sampled.covered else {
                throw DriverError("no sampled pointer point belongs to this native target", code: .inputUnavailable)
            }
            guard foreground else {
                throw NativePreparationUnavailable(description:
                    "another window covers the element's native hit target; no pointer event sent")
            }
        }
        let pointer = NativePointerUnit(application: application, window: selected.mapping, nativeWindow: window)
        try await pointer.prepare(foreground: foreground, atNative: selected.point, kind: kind,
            check: check, onPreparation: onPreparation, onCleanupUnknown: onCleanupUnknown)
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
            let pointer = NativePointerUnit(application: application, window: exposure.mapping, nativeWindow: window)
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
        let pointer = NativePointerUnit(application: application, window: selected.mapping, nativeWindow: window)
        try await pointer.deliver(request, from: selected.point, to: nil, viewport: selected.visible.size,
            check: { try validate(selected, check: check) }, onDispatch: onDispatch,
            onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
    }
}
