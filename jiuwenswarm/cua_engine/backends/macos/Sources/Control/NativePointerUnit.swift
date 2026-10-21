import AppKit
import ApplicationServices

/// One selected native window route. No target search, foreground fallback,
/// screenshot refresh, AXPress substitution or post-dispatch retry lives here.
@MainActor struct NativePointerUnit {
    let application: NSRunningApplication
    let window: WindowGeometry.WindowMapping
    private var target: BackgroundTarget { BackgroundTarget(application) }

    init(application: NSRunningApplication, window: WindowGeometry.WindowMapping) {
        self.application = application; self.window = window
    }

    init(capture: NativeCapture.Entry) {
        self.init(application: capture.surface.native?.application ?? capture.surface.owner, window: capture.mapping)
    }

    func foregroundWindow(at point: CGPoint? = nil) -> Bool {
        target.isForeground && BackgroundEvents.inputWindow(at: point ?? CGPoint(x: window.frame.midX, y: window.frame.midY)) == window.id
    }

    func ready(at point: CGPoint? = nil) -> Bool {
        // An exact already-frontmost destination needs no background routing
        // SPI or activation. This is also the result of authorized foreground
        // preparation; readiness must describe the route delivery will use.
        foregroundWindow(at: point) || (!BackgroundTarget.isExclusive
            && BackgroundEvents.pointerPreparationIsCurrent(target, window: window))
    }

    func prepare(foreground: Bool, atNative point: CGPoint, check: () throws -> Void, onPreparation: @escaping (String) -> Void) async throws {
        try check(); try target.validate(input: true); try BackgroundTarget.validateReleasedInput()
        if ready(at: point) { return }
        if foreground {
            let assist = ForegroundAssist()
            try await assist.prepare(target, window: window.id, pointerAt: point,
                onAction: { onPreparation("foreground window activation") })
        } else {
            do {
                try await BackgroundEvents.prepare(target, window: window, requireKeyboardFocus: false,
                    onAction: { onPreparation("background pointer window preparation") }, beforeAction: check)
            } catch let error as DriverError where error.code == .inputUnavailable {
                try check(); try target.validate(input: true); try BackgroundTarget.validateReleasedInput()
                throw NativePreparationUnavailable(description: error.description)
            }
        }
        try check()
        guard ready(at: point) else { throw NativePreparationUnavailable(description: "pointer route is not ready; no pointer event sent") }
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
        guard ready(at: start) else { throw DriverError("pointer route changed before dispatch", code: .inputUnavailable) }
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
            guard NativeProcessIdentity.read(window.pid) == process else { throw DriverError("pointer recipient changed", code: .staleTarget) }
            let button: CGMouseButton = request.button == "right" ? .right : request.button == "middle" ? .center : .left
            try BackgroundTarget.validateReleasedInput(allowing: global && held ? button : nil)
            if global {
                guard target.isForeground, BackgroundEvents.inputWindow(at: point) == window.id else {
                    throw DriverError("pointer location is covered by another window", code: .inputUnavailable,
                        next: "read the desktop and inspect the covering window")
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
