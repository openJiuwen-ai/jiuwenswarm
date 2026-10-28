import Foundation
import ApplicationServices

@main struct NativePointerTests {
    enum Changed: Error { case geometry }
    @MainActor static func main() async throws {
        if CommandLine.arguments.dropFirst().first == "--pointer-stdio" {
            while let line = readLine() {
                do {
                    let frame = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
                    let command = frame["command"] as! [String: Any]
                    _ = try NativeUnitCommand(command)
                    let intent = try NativeBeginRequest(frame["begin"]).intent
                    _ = try NativePointerIntent(unit: command["kind"] as! String, kind: intent.kind,
                        parameters: intent.parameters, foreground: command["foreground"] as! Bool,
                        element: (command["target"] as? [String: Any])?["node"] is String)
                    print("true")
                } catch { print("false") }
            }
            return
        }
        func request(_ kind: String, _ p: [String: Any], unit: String = "pointer_sequence", foreground: Bool = false) throws -> NativePointerIntent {
            try NativePointerIntent(unit: unit, kind: kind, parameters: p, foreground: foreground)
        }
        let source = CGEventSource(stateID: .privateState)!
        // The same frontmost process and WindowServer order do not prove the
        // same keyboard document. Losing known focus evidence is not a change.
        let process = NativeProcessIdentity.read(getpid())!
        let keyboard = DesktopCapture.KeyboardDestination(process: process, window: 2, focusedWindow: 1)
        try keyboard.validate(keyboard)
        for (current, expected) in [
            (DesktopCapture.KeyboardDestination(process: process, window: 2, focusedWindow: 2), FailureCode.staleTarget),
            (DesktopCapture.KeyboardDestination(process: process, window: 2, focusedWindow: nil), FailureCode.inputUnavailable)
        ] {
            do { try keyboard.validate(current); preconditionFailure("changed/unknown keyboard window admitted") }
            catch let error as DriverError { precondition(error.code == expected) }
        }
        let visualOnly = DesktopCapture.KeyboardDestination(process: process, window: 2, focusedWindow: nil)
        try visualOnly.validate(visualOnly) // AX absence does not remove the global desktop route.
        try visualOnly.validate(keyboard)  // Additional evidence alone is not an identity change.
        for parameters in [[:], ["button": "right"], ["button": "middle", "count": 2]] as [[String: Any]] {
            let click = try NativePointerIntent(unit: "pointer_sequence", kind: "click",
                parameters: parameters, foreground: false, element: true)
            precondition(click.start == nil && click.end == nil)
        }
        for (kind, parameters) in [
            ("click", ["x": 1, "y": 2]), ("click", ["count": 3]), ("click", ["count": true]),
            ("click", ["button": "back"]), ("scroll", ["direction": "down", "pages": 1, "x": 1, "y": 2]),
            ("scroll", ["direction": "down", "pages": NSNull()]), ("scroll", ["direction": "down", "pages": 0]),
            ("scroll", ["direction": "down", "pages": true]), ("scroll", ["direction": "next", "pages": 0.5]),
            ("drag", ["fromX": 1, "fromY": 2, "toX": 3, "toY": 4])
        ] as [(String, [String: Any])] {
            do {
                _ = try NativePointerIntent(unit: "prepare_receiver", kind: kind,
                    parameters: parameters, foreground: false, element: true)
                preconditionFailure("invalid addressed pointer parameters admitted")
            } catch let error as DriverError { precondition(error.code == .invalidRequest) }
        }
        let from = CGPoint(x: 100, y: 200), to = CGPoint(x: 132, y: 224), viewport = CGSize(width: 320, height: 240)
        for unit in ["prepare_receiver", "pointer_sequence"] {
            let addressed = try NativePointerIntent(unit: unit, kind: "scroll",
                parameters: ["direction": "down"], foreground: false, element: true)
            precondition(addressed.pages == 1 && addressed.start == nil && addressed.end == nil)
            let defaultPage = try NativePointerEvents(request: addressed, from: from, to: nil, viewport: viewport, source: source)
            precondition(defaultPage.items[1].event!.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) == -240)
            precondition(defaultPage.items[1].event!.getIntegerValueField(.scrollWheelEventPointDeltaAxis2) == 0)
        }
        for (button, down, up) in [("left", CGEventType.leftMouseDown, CGEventType.leftMouseUp),
                                  ("right", .rightMouseDown, .rightMouseUp), ("middle", .otherMouseDown, .otherMouseUp)] {
            let intent = try request("click", ["x": 1, "y": 2, "button": button, "count": 2])
            let sequence = try NativePointerEvents(request: intent, from: from, to: nil, viewport: viewport, source: source)
            precondition(sequence.items.map { $0.event?.type } == [.mouseMoved, down, nil, down, nil])
            for (index, count) in [(1, 1), (3, 2)] {
                let item = sequence.items[index]
                precondition(item.release?.type == up && item.point == from)
                precondition(item.event!.getIntegerValueField(.mouseEventClickState) == count)
                precondition(item.release!.getIntegerValueField(.mouseEventClickState) == count)
                precondition(item.event!.getDoubleValueField(.mouseEventPressure) == 1)
                precondition(item.release!.getDoubleValueField(.mouseEventPressure) == 0)
                precondition(item.event!.getIntegerValueField(.mouseEventNumber) == sequence.group)
                precondition(item.release!.getIntegerValueField(.mouseEventNumber) == sequence.group)
            }
        }
        for (direction, dx, dy) in [("up", 0, 120), ("down", 0, -120), ("left", 160, 0), ("right", -160, 0)] {
            let intent = try request("scroll", ["x": 1, "y": 2, "direction": direction, "pages": 0.5])
            let sequence = try NativePointerEvents(request: intent, from: from, to: nil, viewport: viewport, source: source)
            let addressed = try NativePointerIntent(unit: "pointer_sequence", kind: "scroll",
                parameters: ["direction": direction, "pages": 0.5], foreground: false, element: true)
            precondition(addressed.start == nil && addressed.end == nil)
            let elementSequence = try NativePointerEvents(request: addressed, from: from, to: nil, viewport: viewport, source: source)
            precondition(elementSequence.items.count == 2)
            precondition(elementSequence.items[1].event!.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) == dy)
            precondition(elementSequence.items[1].event!.getIntegerValueField(.scrollWheelEventPointDeltaAxis2) == dx)
            precondition(sequence.items.count == 2 && sequence.items.allSatisfy { $0.release == nil })
            let event = sequence.items[1].event!
            precondition(event.type == .scrollWheel && event.location == from)
            precondition(event.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) == dy)
            precondition(event.getIntegerValueField(.scrollWheelEventPointDeltaAxis2) == dx)
        }
        let drag = try request("drag", ["fromX": 1, "fromY": 2, "toX": 33, "toY": 26])
        let sequence = try NativePointerEvents(request: drag, from: from, to: to, viewport: viewport, source: source)
        precondition(sequence.items.count == 11 && sequence.items.last?.point == to)
        precondition(sequence.items[1].release?.type == .leftMouseUp)
        for item in sequence.items[2..<10] {
            precondition(item.event!.type == .leftMouseDragged && item.release == nil)
            precondition(item.event!.getIntegerValueField(.mouseEventDeltaX) == 4)
            precondition(item.event!.getIntegerValueField(.mouseEventDeltaY) == 3)
            precondition(item.event!.getDoubleValueField(.mouseEventPressure) == 1)
        }
        let long = try NativePointerEvents(request: drag, from: from, to: CGPoint(x: 100000, y: 100000), viewport: viewport, source: source)
        precondition(long.items.count == 63)

        let invalid: [(String, String, [String: Any], Bool)] = [
            ("native_action", "click", ["x": 1, "y": 2], false),
            ("pointer_sequence", "click", ["x": 1, "y": 2], true),
            ("prepare_receiver", "click", ["x": 1], false),
            ("prepare_receiver", "click", ["x": 1, "y": 2, "count": true], false),
            ("prepare_receiver", "click", ["x": 1, "y": 2, "count": 3], false),
            ("prepare_receiver", "click", ["x": 1, "y": 2, "button": "back"], false),
            ("prepare_receiver", "click", ["x": -1, "y": 2], false),
            ("prepare_receiver", "click", ["x": Double.nan, "y": 2], false),
            ("prepare_receiver", "click", ["x": 1, "y": 2, "skipValidation": true], false),
            ("prepare_receiver", "scroll", ["x": 1, "y": 2, "direction": "down"], false),
            ("prepare_receiver", "scroll", ["x": 1, "y": 2, "direction": "down", "pages": 0], false),
            ("prepare_receiver", "scroll", ["x": 1, "y": 2, "direction": "next", "pages": 1], false),
            ("prepare_receiver", "drag", ["fromX": 1, "fromY": 2, "toX": 1, "toY": 2], false),
            ("pointer_sequence", "key", ["key": "Return"], false)
        ]
        for (unit, kind, parameters, foreground) in invalid {
            do {
                _ = try request(kind, parameters, unit: unit, foreground: foreground)
                preconditionFailure("invalid pointer intent admitted before preparation")
            } catch let error as DriverError { precondition(error.code == .invalidRequest) }
        }
        for point in [CGPoint(x: Double.infinity, y: 0), CGPoint(x: 1e30, y: 0)] {
            do {
                _ = try NativePointerEvents(request: drag, from: from, to: point, viewport: viewport, source: source)
                preconditionFailure("invalid native geometry admitted")
            } catch let error as DriverError { precondition(error.code == .invalidRequest) }
        }
        // Exercise the production delivery loop, using callbacks instead of OS
        // posting. Window changes must stop new input, but not required release.
        let click = try NativePointerEvents(request: request("click", ["x": 1, "y": 2]),
            from: from, to: nil, viewport: viewport, source: source)
        var events = [(CGEventType, CGPoint)](), closed = false, skipped = 0
        try await click.deliver(check: { _, _ in if closed { throw Changed.geometry } }, post: { event, point in
            events.append((event.type, point))
            if event.type == .leftMouseDown { closed = true }
        }, canRelease: { true }, skipped: { skipped += 1 }, onDispatch: {}, onPosted: {})
        precondition(events.map(\.0) == [.mouseMoved, .leftMouseDown, .leftMouseUp] && skipped == 0)
        events = []
        do {
            try await sequence.deliver(check: { _, _ in
                if events.contains(where: { $0.0 == .leftMouseDragged }) { throw Changed.geometry }
            }, post: { events.append(($0.type, $1)) }, canRelease: { true }, skipped: {}, onDispatch: {}, onPosted: {})
            preconditionFailure("changed screenshot mapping did not stop the drag")
        } catch is Changed {}
        precondition(events.map(\.0) == [.mouseMoved, .leftMouseDown, .leftMouseDragged, .leftMouseUp])
        precondition(events[2].1 == events[3].1 && events.last!.1 != to)
        events = []
        let cancelled = Task { @MainActor in
            do {
                try await sequence.deliver(check: { _, _ in }, post: { event, point in
                    events.append((event.type, point))
                    if event.type == .leftMouseDragged { withUnsafeCurrentTask { $0?.cancel() } }
                }, canRelease: { true }, skipped: {}, onDispatch: {}, onPosted: {})
                preconditionFailure("cancelled drag completed")
            } catch is CancellationError {}
        }
        try await cancelled.value
        precondition(events.map(\.0) == [.mouseMoved, .leftMouseDown, .leftMouseDragged, .leftMouseUp])
        precondition(events[2].1 == events[3].1)
        // A mouse-move callback can change the selected hit path before the
        // wheel. The production sequence rechecks before that wheel and must
        // stop after the move rather than emitting to the changed receiver.
        events = []; var barriers = 0
        let wheel = try NativePointerEvents(request: NativePointerIntent(unit: "pointer_sequence", kind: "scroll",
            parameters: ["direction": "down", "pages": 0.5], foreground: false, element: true),
            from: from, to: nil, viewport: viewport, source: source)
        do {
            try await wheel.deliver(check: { _, _ in if !events.isEmpty { throw Changed.geometry } },
                post: { events.append(($0.type, $1)) }, canRelease: { true }, skipped: {},
                onDispatch: { barriers += 1 }, onPosted: {})
            preconditionFailure("changed passive scroll path received a wheel event")
        } catch is Changed { }
        precondition(events.map(\.0) == [.mouseMoved] && barriers == 1)
        events = []; var originalProcess = true
        try await click.deliver(check: { _, _ in }, post: { event, point in
            events.append((event.type, point)); if event.type == .leftMouseDown { originalProcess = false }
        }, canRelease: { originalProcess }, skipped: { skipped += 1 }, onDispatch: {}, onPosted: {})
        precondition(events.map(\.0) == [.mouseMoved, .leftMouseDown] && skipped == 1)
        print("PASS native pointer intents, real CGEvent encoding, bounded drag, cancellation, window change and original-process release")
    }
}
