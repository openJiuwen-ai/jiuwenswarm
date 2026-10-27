import AppKit
import ApplicationServices

/// A transport receipt does not prove that a first mouse-down reaches content.
/// Passive wheel delivery has no initial button event to consume for activation.
enum NativePointerReadiness {
    enum WindowKind { case keyRequiring, otherDeclared, visualOnly, identityConflict }
    enum WindowFocus: Equatable {
        case target, otherWindow, absent, unresolved, failed, notRequired
        var reported: Bool { self == .target || self == .otherWindow }
    }

    /// A positive native mapping takes precedence over object equality. A
    /// missing optional bridge is not a conflicting identity.
    static func focusIdentity(nativeID: CGWindowID?, retainedEqual: Bool,
                              targetID: CGWindowID) -> WindowFocus {
        if let nativeID { return nativeID == targetID ? .target : .otherWindow }
        return retainedEqual ? .target : .unresolved
    }

    private enum Field: Equatable {
        case declared(String), absent, failed
        init(_ read: AXRead<CFTypeRef>) {
            switch read {
            case .unsupported, .unavailable(.noValue): self = .absent
            case .value(let value):
                if let value = value as? String {
                    self = value.isEmpty || value == kAXUnknownSubrole ? .absent : .declared(value)
                } else { self = .failed }
            default: self = .failed
            }
        }
    }

    static func windowKind(role: AXRead<CFTypeRef>, subrole: AXRead<CFTypeRef>, nativeWindowMatches: Bool?,
                           observed: [String: AXRead<CFTypeRef>]? = nil) -> WindowKind {
        guard nativeWindowMatches != false else { return .identityConflict }
        let role = Field(role), subrole = Field(subrole)
        // Labels choose preparation, not window identity. Use a current
        // declaration even if its semantics changed; retain an original only
        // when the provider is temporarily unable to report the field.
        func declaration(_ field: Field, original: AXRead<CFTypeRef>?) -> String? {
            if case .declared(let value) = field { return value }
            if let original, case .declared(let value) = Field(original) { return value }
            return nil
        }
        guard let role = declaration(role, original: observed?[kAXRoleAttribute]) else { return .visualOnly }
        // Menus and declared content scopes need not expose an AX window ID.
        // Their retained capture/element boundary still owns the physical hit.
        guard role == kAXWindowRole else { return .otherDeclared }
        guard let subrole = declaration(subrole, original: observed?[kAXSubroleAttribute]) else { return .visualOnly }
        // A nonactivating NSPanel can itself declare AXSystemDialog. Do not
        // infer key eligibility from dialog names or impose a key requirement
        // on menus/panels. This strong gate covers standard documents only.
        return subrole == kAXStandardWindowSubrole ? .keyRequiring : .otherDeclared
    }

    static func ready(kind: NativePointerIntent.Kind, foreground: Bool, windowKind: WindowKind,
                      focus: WindowFocus, initialFocus: WindowFocus? = nil,
                      backgroundPrepared: Bool, exclusive: Bool) -> Bool {
        if foreground {
            if kind == .scroll { return true }
            switch windowKind {
            case .keyRequiring:
                switch focus {
                case .target: return true
                case .otherWindow: return false
                case .absent: return initialFocus?.reported != true
                // A provider failure or missing private bridge does not prove
                // a different key window. Keep the exact physical visual route.
                case .unresolved, .failed, .notRequired: return true
                }
            case .otherDeclared, .visualOnly: return true
            case .identityConflict: return false
            }
        }
        return kind == .scroll && !exclusive && backgroundPrepared
    }
}

/// One selected native window route. No target search, foreground fallback,
/// screenshot refresh, AXPress substitution or post-dispatch retry lives here.
@MainActor struct NativePointerUnit {
    let application: NSRunningApplication
    let window: WindowGeometry.WindowMapping
    let nativeWindow: AXUIElement?
    private let observedWindowAttributes: [String: AXRead<CFTypeRef>]?
    private var target: BackgroundTarget { BackgroundTarget(application) }

    init(application: NSRunningApplication, window: WindowGeometry.WindowMapping, nativeWindow: AXUIElement? = nil,
         observedWindowAttributes: [String: AXRead<CFTypeRef>]? = nil) {
        self.application = application; self.window = window; self.nativeWindow = nativeWindow
        self.observedWindowAttributes = observedWindowAttributes
    }

    init(capture: NativeCapture.Entry) {
        self.init(application: capture.surface.native?.application ?? capture.surface.owner,
            window: capture.mapping, nativeWindow: capture.surface.element)
    }

    func foregroundWindow(at point: CGPoint? = nil) -> Bool {
        target.isForeground && inputWindowHit(at: point).windowID == window.id
    }

    func inputWindowHit(at point: CGPoint? = nil) -> NativeInputWindowHit {
        BackgroundEvents.inputWindowHit(at: point ?? CGPoint(x: window.frame.midX, y: window.frame.midY),
            retained: window, nativeWindow: nativeWindow)
    }

    private var windowKind: NativePointerReadiness.WindowKind {
        // Standard documents need exact-window preparation before their first
        // button. Initial AX absence retains the foreground visual route.
        guard let nativeWindow else { return .visualOnly }
        var pid: pid_t = 0
        guard AXUIElementGetPid(nativeWindow, &pid) == .success else { return .visualOnly }
        guard pid == window.pid else { return .identityConflict }
        let role = AXAccess.read(nativeWindow, kAXRoleAttribute)
        let subrole = AXAccess.read(nativeWindow, kAXSubroleAttribute)
        return NativePointerReadiness.windowKind(role: role, subrole: subrole,
            nativeWindowMatches: BackgroundEvents.nativeWindowID(nativeWindow).map { $0 == window.id },
            observed: observedWindowAttributes)
    }

    func validateWindowIdentity() throws {
        guard let nativeWindow else { return }
        var pid: pid_t = 0
        let owner = AXUIElementGetPid(nativeWindow, &pid)
        if (owner == .success && pid != window.pid)
            || BackgroundEvents.nativeWindowID(nativeWindow).map({ $0 != window.id }) == true {
            throw DriverError("the retained accessibility window no longer matches the pointer destination", code: .staleTarget)
        }
    }

    func foregroundPreparationPoint(at point: CGPoint) -> CGPoint? {
        windowKind == .keyRequiring ? nil : point
    }

    private func windowFocus() -> NativePointerReadiness.WindowFocus {
        // Timeout belongs only to this fresh root proxy, never the retained
        // target or another equal AX object used by observation/input.
        let root = AXUIElementCreateApplication(window.pid)
        AXUIElementSetMessagingTimeout(root, 0.1)
        switch AXAccess.read(root, kAXFocusedWindowAttribute) {
        case .unsupported, .unavailable(.noValue): return .absent
        case .value(let raw):
            guard CFGetTypeID(raw) == AXUIElementGetTypeID() else { return .failed }
            let focused = unsafeDowncast(raw, to: AXUIElement.self)
            var pid: pid_t = 0
            guard AXUIElementGetPid(focused, &pid) == .success, pid == window.pid else { return .unresolved }
            let equal = nativeWindow.map { CFEqual(focused, $0) } ?? false
            // Bound this new non-target proxy before even the optional native
            // bridge. Never change the retained target's messaging policy.
            if !equal { AXUIElementSetMessagingTimeout(focused, 0.05) }
            var id = BackgroundEvents.nativeWindowID(focused)
            if id == nil && !equal {
                id = NativeInputWindowQuery.publicIdentity(of: focused, pid: window.pid).window?.id
            }
            return NativePointerReadiness.focusIdentity(nativeID: id, retainedEqual: equal, targetID: window.id)
        default: return .failed
        }
    }

    /// Process-addressed notifications and AXMain cannot establish a cold
    /// content window's real key state. Already-foreground window selection
    /// remains available; passive wheel input retains its background route.
    func canPrepareWindowDirected(kind: NativePointerIntent.Kind = .click) -> Bool {
        kind == .scroll || target.isForeground
    }

    func requireWindowDirectedPreparation(kind: NativePointerIntent.Kind = .click) throws {
        try validateWindowIdentity()
        guard canPrepareWindowDirected(kind: kind) else {
            throw NativePreparationUnavailable(description:
                "the first mouse-down may only activate this window; foreground preparation is required before sending pointer input")
        }
    }

    /// The caller supplies an already-checked physical foreground/window hit.
    /// Reuse this for offer sampling, preparation and final event validation.
    private struct Readiness {
        let ready: Bool
        let foreground: Bool
        let focus: NativePointerReadiness.WindowFocus
        let windowKind: NativePointerReadiness.WindowKind
    }

    private func contentReadiness(foreground: Bool, kind: NativePointerIntent.Kind,
                                  declaredWindow: NativePointerReadiness.WindowKind? = nil,
                                  initialFocus: NativePointerReadiness.WindowFocus? = nil) -> Readiness {
        // The declaration class is gesture-local. Rechecking labels for every
        // down/up would add slow provider RPCs to the event cadence. The real
        // object/process/window and physical hit remain checked for every event.
        let declared = declaredWindow ?? (foreground && kind != .scroll ? windowKind : .visualOnly)
        let focus = foreground && declared == .keyRequiring ? windowFocus() : .notRequired
        let prepared = !foreground && kind == .scroll && !BackgroundTarget.isExclusive
            && BackgroundEvents.pointerPreparationIsCurrent(target, window: window)
        return Readiness(ready: NativePointerReadiness.ready(kind: kind, foreground: foreground, windowKind: declared,
            focus: focus, initialFocus: initialFocus, backgroundPrepared: prepared, exclusive: BackgroundTarget.isExclusive),
            foreground: foreground, focus: focus, windowKind: declared)
    }

    private func sampleReadiness(at point: CGPoint? = nil, kind: NativePointerIntent.Kind) -> Readiness {
        let foreground = foregroundWindow(at: point)
        return contentReadiness(foreground: foreground, kind: kind)
    }

    func ready(at point: CGPoint? = nil, kind: NativePointerIntent.Kind = .click) -> Bool {
        sampleReadiness(at: point, kind: kind).ready
    }

    func prepare(foreground: Bool, atNative point: CGPoint, kind: NativePointerIntent.Kind = .click,
                 check: @escaping @MainActor () throws -> Void, onPreparation: @escaping @MainActor (String) -> Void,
                 onCleanupUnknown: @escaping @MainActor () -> Void = {}) async throws {
        try check(); try target.validate(input: true); try BackgroundTarget.validateReleasedInput()
        try validateWindowIdentity()
        let currentReady = ready(at: point, kind: kind)
        try check()
        if currentReady { return }
        if foreground {
            let assist = ForegroundAssist()
            // The point alone cannot prepare a different standard document:
            // the first mouse-down may only make it key. Keep hit-only
            // preparation for non-key panels, menus and visual-only surfaces.
            try await assist.prepare(target, window: window.id, pointerAt: kind != .scroll && windowKind == .keyRequiring ? nil : point,
                onAction: { onPreparation("foreground window activation") }, onCleanupUnknown: onCleanupUnknown)
        } else {
            try requireWindowDirectedPreparation(kind: kind)
            do {
                try await BackgroundEvents.prepare(target, window: window, requireKeyboardFocus: kind != .scroll,
                    onAction: { onPreparation("background pointer window preparation") }, beforeAction: check)
            } catch let error as DriverError where error.code == .inputUnavailable {
                try check(); try target.validate(input: true); try BackgroundTarget.validateReleasedInput()
                throw NativePreparationUnavailable(description: error.description)
            }
        }
        try check()
        guard ready(at: point, kind: kind) else { throw NativePreparationUnavailable(description: "pointer route is not ready; no pointer event sent") }
    }

    func point(_ pixel: CGPoint) throws -> CGPoint {
        guard pixel.x >= 0, pixel.y >= 0, pixel.x < Double(window.width), pixel.y < Double(window.height) else {
            throw DriverError("pointer coordinates are outside the retained window image", code: .invalidRequest)
        }
        return CGPoint(x: window.frame.minX + pixel.x * window.frame.width / Double(window.width),
                       y: window.frame.minY + pixel.y * window.frame.height / Double(window.height))
    }

    func validateParameters(_ request: NativePointerIntent) throws {
        guard let start = request.start else {
            throw DriverError("window image input requires image coordinates", code: .invalidRequest)
        }
        _ = try point(start)
        if let end = request.end { _ = try point(end) }
        if request.kind == .scroll {
            _ = try NativePointerEvents.scrollDelta(direction: request.direction, pages: request.pages, viewport: window.frame.size)
        }
    }

    func deliver(_ request: NativePointerIntent, check: @escaping @MainActor () throws -> Void,
                 onDispatch: () throws -> Void, onPosted: () -> Void, onCleanupUnknown: @escaping () -> Void) async throws {
        try validateParameters(request)
        try await deliver(request, from: point(request.start!), to: request.end.map(point), viewport: window.frame.size,
            check: check, onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
    }

    /// Geometry has one owner: an immutable image transform or a freshly
    /// resolved Element. Both use this exact event route and paired cleanup.
    func deliver(_ request: NativePointerIntent, from start: CGPoint, to end: CGPoint?, viewport: CGSize,
                 check: @escaping @MainActor () throws -> Void, onDispatch: () throws -> Void,
                 onPosted: () -> Void, onCleanupUnknown: @escaping () -> Void) async throws {
        try check(); try target.validate(input: true); try BackgroundTarget.validateReleasedInput()
        try validateWindowIdentity()
        let initial = sampleReadiness(at: start, kind: request.kind)
        guard initial.ready else { throw DriverError("pointer route changed before dispatch", code: .inputUnavailable) }
        // Freeze one route for the entire gesture. A visible foreground target
        // receives ordinary HID input (including the global pointer position
        // some toolkits poll). Background windows remain process-addressed;
        // this branch never activates, retries or changes desktop mode.
        let source = try Input.source(), global = BackgroundTarget.isExclusive || foregroundWindow(at: start)
        guard let process = NativeProcessIdentity.read(window.pid) else {
            throw DriverError("pointer recipient disappeared", code: .staleTarget)
        }
        let sequence = try NativePointerEvents(request: request, from: start, to: end,
            viewport: viewport, source: source)
        let entry: CGEvent?
        if global { entry = nil }
        else {
            guard let value = NSEvent.enterExitEvent(with: .mouseEntered,
                location: NSPoint(x: start.x - window.frame.minX, y: window.frame.maxY - start.y),
                modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                windowNumber: Int(window.id), context: nil, eventNumber: 0, trackingNumber: 0, userData: nil)?.cgEvent else {
                throw DriverError("cannot allocate pointer entry event", code: .inputUnavailable)
            }
            entry = value
        }
        func post(_ event: CGEvent, point: CGPoint) {
            AgentPointer.current?.move(to: point, window: window,
                pulse: request.kind == .click && [.leftMouseDown, .rightMouseDown, .otherMouseDown].contains(event.type),
                count: request.count,
                dragging: request.kind == .drag ? event.type != .leftMouseUp : nil)
            event.location = point
            if global { event.post(tap: .cghidEventTap) }
            else { BackgroundEvents.post(event, window: window, point: point, clickGroup: sequence.group) }
        }
        func validate(_ point: CGPoint, held: Bool) throws {
            try check(); try target.validate(input: true)
            try validateWindowIdentity()
            guard NativeProcessIdentity.read(window.pid) == process else { throw DriverError("pointer recipient changed", code: .staleTarget) }
            let button: CGMouseButton = request.button == "right" ? .right : request.button == "middle" ? .center : .left
            try BackgroundTarget.validateReleasedInput(allowing: global && held ? button : nil)
            if global {
                guard target.isForeground, inputWindowHit(at: point).windowID == window.id else {
                    throw DriverError("pointer location is covered by another window", code: .inputUnavailable,
                        next: "read the desktop and inspect the covering window")
                }
                if !held && !contentReadiness(foreground: true, kind: request.kind,
                    declaredWindow: initial.windowKind, initialFocus: initial.focus).ready {
                    throw DriverError("pointer content window changed before input", code: .inputUnavailable)
                }
            } else if !BackgroundEvents.pointerPreparationIsCurrent(target, window: window) {
                throw DriverError("background pointer route changed", code: .inputUnavailable)
            }
        }
        try await sequence.deliver(entry: entry, check: validate, post: post,
            canRelease: { global || NativeProcessIdentity.read(window.pid) == process }, skipped: onCleanupUnknown,
            onDispatch: onDispatch, onPosted: onPosted)
    }
}

/// Event construction and bounded delivery. The chosen route supplies posting
/// and live checks; tests exercise the same sequence without posting OS input.
@MainActor struct NativePointerEvents {
    struct Item { let event: CGEvent?; let release: CGEvent?; let point: CGPoint; let delayMs: Int }
    let items: [Item]
    let group: Int64
    private let source: CGEventSource
    private let settleUnpaired: Bool

    static func scrollDelta(direction: String?, pages: Double?, viewport: CGSize) throws -> (vertical: Bool, delta: Int32) {
        guard let direction, ["up", "down", "left", "right"].contains(direction), let pages, pages.isFinite, pages > 0 else {
            throw DriverError("scroll input requires a valid direction and positive finite pages", code: .invalidRequest)
        }
        let vertical = ["up", "down"].contains(direction)
        let size = vertical ? viewport.height : viewport.width
        let distance = pages * size
        guard size.isFinite, size > 0, distance.isFinite, distance > 0, distance < Double(Int32.max) else {
            throw DriverError("invalid native scroll distance", code: .invalidRequest)
        }
        let delta = Int32(max(1, distance.rounded())) * (["down", "right"].contains(direction) ? -1 : 1)
        return (vertical, delta)
    }

    init(request: NativePointerIntent, from: CGPoint, to: CGPoint?, viewport: CGSize, source: CGEventSource,
         durationMs: Int? = nil) throws {
        let points = [from] + (to.map { [$0] } ?? [])
        guard points.allSatisfy({ $0.x.isFinite && $0.y.isFinite }),
              viewport.width.isFinite, viewport.height.isFinite, viewport.width > 0, viewport.height > 0 else {
            throw DriverError("invalid native pointer geometry", code: .invalidRequest)
        }
        self.source = source; settleUnpaired = request.kind == .scroll
        // CGEvent's mouse event number is represented as a signed 16-bit
        // NSEvent number; larger values are silently truncated by CoreGraphics.
        let group = Int64.random(in: 1...Int64(Int16.max)); self.group = group
        let button: CGMouseButton = request.button == "right" ? .right : request.button == "middle" ? .center : .left
        let down: CGEventType = request.button == "right" ? .rightMouseDown : request.button == "middle" ? .otherMouseDown : .leftMouseDown
        let up: CGEventType = request.button == "right" ? .rightMouseUp : request.button == "middle" ? .otherMouseUp : .leftMouseUp
        func mouse(_ type: CGEventType, at point: CGPoint, count: Int = 1) throws -> CGEvent {
            guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: button) else {
                throw DriverError("cannot allocate pointer event", code: .inputUnavailable)
            }
            event.flags = []; event.setIntegerValueField(.mouseEventClickState, value: Int64(count))
            event.setIntegerValueField(.mouseEventNumber, value: group)
            event.setDoubleValueField(.mouseEventPressure, value: type == down || type == .leftMouseDragged ? 1 : 0)
            return event
        }
        var result = [Item(event: try mouse(.mouseMoved, at: from), release: nil, point: from, delayMs: 0)]
        switch request.kind {
        case .click:
            for count in 1...request.count {
                result.append(.init(event: try mouse(down, at: from, count: count),
                    release: try mouse(up, at: from, count: count), point: from, delayMs: count == 1 ? 15 : 45))
                result.append(.init(event: nil, release: nil, point: from, delayMs: 25))
            }
        case .scroll:
            let scroll = try Self.scrollDelta(direction: request.direction, pages: request.pages, viewport: viewport)
            guard let event = CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2,
                wheel1: scroll.vertical ? scroll.delta : 0, wheel2: scroll.vertical ? 0 : scroll.delta, wheel3: 0) else {
                throw DriverError("cannot allocate scroll event", code: .inputUnavailable)
            }
            event.flags = []; event.location = from
            result.append(.init(event: event, release: nil, point: from, delayMs: 15))
        case .drag:
            guard let to else { throw DriverError("drag endpoint missing", code: .invalidRequest) }
            result.append(.init(event: try mouse(down, at: from), release: try mouse(up, at: from), point: from, delayMs: 15))
            let count = max(8, Int(min(60, ceil(hypot(to.x - from.x, to.y - from.y) / 8))))
            var previous = from
            for step in 1...count {
                let fraction = Double(step) / Double(count)
                let point = CGPoint(x: from.x + (to.x - from.x) * fraction, y: from.y + (to.y - from.y) * fraction)
                // Check before converting to native integer deltas. Real window
                // geometry is much smaller; malformed input must never trap.
                let dx = (point.x - previous.x).rounded(), dy = (point.y - previous.y).rounded()
                guard dx.isFinite, dy.isFinite, abs(dx) < Double(Int64.max), abs(dy) < Double(Int64.max) else {
                    throw DriverError("drag delta exceeds native event range", code: .invalidRequest)
                }
                let dragType: CGEventType = request.button == "right" ? .rightMouseDragged : request.button == "middle" ? .otherMouseDragged : .leftMouseDragged
                let event = try mouse(dragType, at: point)
                event.setIntegerValueField(.mouseEventDeltaX, value: Int64(dx))
                event.setIntegerValueField(.mouseEventDeltaY, value: Int64(dy))
                result.append(.init(event: event, release: nil, point: point,
                    delayMs: durationMs.map { max(0, $0) / count } ?? 12)); previous = point
            }
            result.append(.init(event: nil, release: nil, point: to, delayMs: 12))
        }
        items = result
    }

    func deliver(entry: CGEvent? = nil, check: (CGPoint, Bool) throws -> Void,
                 post: @escaping (CGEvent, CGPoint) -> Void, canRelease: @escaping () -> Bool,
                 skipped: @escaping () -> Void, onDispatch: () throws -> Void, onPosted: () -> Void) async throws {
        var lastPoint = items[0].point
        let pair = PairedInput<CGEvent>(source: source, post: { post($0, lastPoint) },
            canRelease: canRelease, skipped: skipped, settleUnpaired: settleUnpaired)
        try await pair.run {
            var held = false
            if let entry {
                try check(lastPoint, false); try onDispatch(); post(entry, lastPoint); onPosted()
            }
            for item in items {
                if item.delayMs > 0 { try await Task.sleep(for: .milliseconds(item.delayMs)) }
                try Task.checkCancellation()
                // Mouse-up belongs to the original event route, even when the
                // down closed its window. Do not require post-action UI state.
                guard let event = item.event else { pair.release(); held = false; continue }
                try check(item.point, held)
                lastPoint = item.point
                try onDispatch()
                if let release = item.release { pair.press(event, release: release); held = true }
                else { post(event, lastPoint) }
                onPosted()
            }
        }
    }
}
