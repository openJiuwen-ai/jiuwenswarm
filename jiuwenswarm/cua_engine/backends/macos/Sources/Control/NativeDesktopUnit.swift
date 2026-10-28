import AppKit
import CoreGraphics

/// The global route of one selected v5 contract. It neither prepares an App
/// nor searches AX, refreshes images, restores focus or retries an input.
@MainActor struct NativeDesktopUnit {
    let desktop: DesktopCapture
    let ref: String

    func check() throws {
        try Input.requireDesktopInput()
        _ = try desktop.observed(ref)
    }

    func pointer(_ request: NativePointerIntent) throws -> (from: CGPoint, to: CGPoint?, viewport: CGSize) {
        try check()
        let mapping = try desktop.mapping(ref)
        guard let start = request.start else { throw DriverError("desktop pointer needs image coordinates", code: .invalidRequest) }
        func point(_ pixel: CGPoint) throws -> CGPoint {
            try mapping.geometry.point(x: pixel.x, y: pixel.y, captured: mapping.captured)
        }
        let from = try point(start), to = try request.end.map(point)
        try desktop.validateHit(ref, point: from)
        if let to {
            try mapping.geometry.validatePath(from: from, to: to, captured: mapping.captured)
            try desktop.validateHit(ref, point: to)
        }
        guard let display = mapping.geometry.displays.first(where: { $0.frame.contains(from) }) else {
            throw DriverError("desktop point has no display", code: .invalidRequest)
        }
        let viewport = mapping.surfaces.first { $0.frame.contains(from) }?.frame.intersection(display.frame) ?? display.frame
        return (from, to, viewport.size)
    }

    func deliver(_ request: NativePointerIntent, check boundary: @escaping @MainActor () throws -> Void,
                 onDispatch: () throws -> Void, onPosted: () -> Void,
                 onCleanupUnknown: @escaping () -> Void) async throws {
        let geometry = try pointer(request)
        try BackgroundTarget.validateReleasedInput()
        let sequence = try NativePointerEvents(request: request, from: geometry.from, to: geometry.to,
            viewport: geometry.viewport, source: Input.source())
        var completed = false
        defer { if completed { desktop.inputFinished(ref) } else { desktop.inputFailed() } }
        try await sequence.deliver(check: { point, held in
            try boundary(); try check()
            let button: CGMouseButton = request.button == "right" ? .right : request.button == "middle" ? .center : .left
            try BackgroundTarget.validateReleasedInput(allowing: held ? button : nil)
            let mapping = try desktop.mapping(ref)
            if request.kind != .drag || !held { try desktop.validateHit(ref, point: point) }
            // During a drag the OS owns mouse capture. Windows may move with
            // the gesture; the display transform and captured path must stay valid.
            let pixel = mapping.geometry.rect(CGRect(origin: point, size: .zero)).origin
            _ = try mapping.geometry.point(x: pixel.x, y: pixel.y, captured: mapping.captured)
        }, post: { event, point in
            AgentPointer.current?.move(to: point, window: nil,
                pulse: request.kind == .click && [.leftMouseDown, .rightMouseDown, .otherMouseDown].contains(event.type),
                count: request.count,
                dragging: request.kind == .drag ? event.type != .leftMouseUp : nil)
            event.location = point; event.post(tap: .cghidEventTap)
        }, canRelease: { true }, skipped: onCleanupUnknown, onDispatch: onDispatch, onPosted: onPosted)
        completed = true
    }

    func deliver(_ request: NativeKeyboardIntent, check boundary: @escaping @MainActor () throws -> Void,
                 onDispatch: @escaping @MainActor () throws -> Void, onPosted: @escaping @MainActor () -> Void,
                 onCleanupUnknown: @escaping () -> Void) async throws {
        try check(); try BackgroundTarget.validateReleasedInput()
        guard request.kind == .key || request.kind == .insert else {
            throw DriverError("desktop keyboard supports insertion and keys", code: .invalidRequest)
        }
        let recipient = try desktop.keyboardDestination(ref)
        var completed = false
        defer { if completed { desktop.inputFinished(ref) } else { desktop.inputFailed() } }
        func validate() throws {
            try boundary(); try check()
            try recipient.validate(DesktopCapture.destination())
        }
        if request.kind == .key {
            try await Input.preparedKey(request.value!, recipient: nil, validate: validate,
                onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
        } else {
            try await Input.preparedText(request.value!, recipient: nil, applicationPID: nil, validate: {
                try validate(); try BackgroundTarget.validateReleasedInput()
            }, onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
        }
        completed = true
    }
}
