import Foundation
import AppKit
import ApplicationServices

@main struct NativeObjectIntentTests {
    @MainActor static func main() throws {
        if CommandLine.arguments.dropFirst().first == "--scroll-stdio" {
            while let line = readLine() {
                do {
                    let frame = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
                    let begin = try NativeBeginRequest(frame["begin"])
                    let command = try NativeUnitCommand(frame["command"])
                    _ = try NativeScrollIntent(unit: command.kind, kind: begin.intent.kind,
                        parameters: begin.intent.parameters, foreground: command.foreground)
                    print("true")
                } catch { print("false") }
            }
            return
        }
        if CommandLine.arguments.dropFirst().first == "--keyboard-stdio" {
            while let line = readLine() {
                let frame = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
                let command = frame["command"] as! [String: Any]
                let intent = try NativeBeginRequest(frame["begin"]).intent
                do {
                    _ = try NativeUnitCommand(command)
                    _ = try NativeKeyboardIntent(unit: command["kind"] as! String, kind: intent.kind,
                        parameters: intent.parameters, foreground: command["foreground"] as! Bool)
                    print("true")
                } catch { print("false") }
            }
            return
        }
        let clicks: [[String: Any]] = [[:], ["button": "left"], ["count": 1], ["button": "left", "count": 1]]
        // Plain text and Return need the addressed receiver. Command-key
        // equivalents additionally prepare the window route before dispatch;
        // neither route requires AXFocused on the window node.
        let element = AXUIElementCreateApplication(123)
        let keyboardUnit = NativeKeyboardUnit(application: .current,
            window: AXUIElementCreateApplication(124), element: element)
        func readiness(_ kind: NativeKeyboardIntent.Kind, key: String? = nil, prepared: Bool = false,
                       route: String = "yes", foreground: Bool = false) throws -> NativeReadiness {
            try keyboardUnit.readiness(kind: kind, key: key, prepared: prepared, state: .init(windowRoute: route, focus: element,
                ancestors: [], foreground: foreground))
        }
        for kind: NativeKeyboardIntent.Kind in [.focus, .insert, .key] {
            precondition(try! readiness(kind).ready)
            precondition(try! readiness(kind, foreground: true).ready)
            precondition(try! readiness(kind, route: "no", foreground: true).state == "no")
            precondition(try! readiness(kind, route: "unknown").state == "unknown")
        }
        for (kind, key) in [(NativeKeyboardIntent.Kind.paste, nil), (.key, "super+p"), (.key, "shift+super+v")] {
            precondition(try! readiness(kind, key: key).pending == ["receiver"])
            precondition(try! readiness(kind, key: key, prepared: true).ready)
            precondition(try! readiness(kind, key: key, foreground: true).ready)
            precondition(try! readiness(kind, key: key, prepared: true, route: "no").state == "no")
            precondition(try! readiness(kind, key: key, prepared: true, route: "unknown").state == "unknown")
        }
        precondition(try! readiness(.key, key: "Return").ready)
        precondition(try! readiness(.key, key: "ctrl+a").ready)
        precondition(NativeKeyboardIntent.usesWindowCommand(.replace, key: nil, replacementShortcut: true))
        precondition(!NativeKeyboardIntent.usesWindowCommand(.replace, key: nil, replacementShortcut: false))
        // Final receiver validation uses the chosen editor, not another child
        // of the same owner. This belongs to the native unit; Python no longer
        // duplicates that decision from the diagnostic InputContext snapshot.
        let owner = AXUIElementCreateApplication(125)
        let sibling = AXUIElementCreateApplication(126)
        func receiver(_ focus: AXUIElement?, ancestors: [AXUIElement] = [], route: String = "yes") -> String {
            NativeKeyboardUnit.State(windowRoute: route, focus: focus, ancestors: ancestors,
                foreground: false).receiver(element)
        }
        precondition(receiver(sibling, ancestors: [owner]) == "no")
        precondition(receiver(element, ancestors: [owner]) == "yes")
        precondition(receiver(sibling, ancestors: [element, owner]) == "no")
        precondition(NativeKeyboardUnit.State(windowRoute: "yes", focus: sibling, ancestors: [element],
            foreground: false, relatedEditor: element).receiver(element) == "yes")
        precondition(NativeKeyboardUnit.State(windowRoute: "yes", focus: sibling, ancestors: [],
            foreground: false, window: element).receiver(element) == "yes")
        precondition(NativeKeyboardUnit.State(windowRoute: "no", focus: sibling, ancestors: [],
            foreground: false, window: element).receiver(element) == "no")
        precondition(receiver(element, route: "no") == "no")
        precondition(receiver(nil) == "unknown")
        // App-level key/insert/paste freeze their receiver only for this
        // delivery, after preparation. Equal windows do not make siblings the
        // same recipient; losing known AX evidence cannot become window-only.
        let window = AXUIElementCreateApplication(127)
        func state(_ focus: AXUIElement?, related: AXUIElement? = nil) -> NativeReceiverSnapshot {
            .init(windowRoute: "yes", focus: focus, ancestors: [], foreground: false,
                  relatedEditor: related, window: window)
        }
        func lost(_ continuity: NativeReceiverContinuity, _ current: NativeReceiverSnapshot, code: FailureCode) {
            do {
                try continuity.validate(current)
                preconditionFailure("a changed keyboard recipient was accepted")
            } catch let error as NativeReceiverContinuity.Lost { precondition(error.code == code) }
            catch { preconditionFailure("wrong receiver failure: \(error)") }
        }
        for requested in [nil, window] as [AXUIElement?] {
            let continuity = NativeReceiverContinuity(element: requested, window: window, state: state(element))!
            try continuity.validate(state(element))
            lost(continuity, state(sibling), code: .staleTarget)
            lost(continuity, state(nil), code: .inputUnavailable)
            lost(continuity, state(window), code: .staleTarget)
            // No native shortcut invents a virtual-focus relation for an App.
            lost(continuity, state(sibling, related: element), code: .staleTarget)
            try continuity.validate(state(element))
            // A new method after Tab/Return samples anew, rather than inheriting
            // a global cached recipient from the preceding primitive.
            let next = NativeReceiverContinuity(element: requested, window: window, state: state(sibling))!
            try next.validate(state(sibling))
            lost(next, state(element), code: .staleTarget)
            // Unknown initial AX focus retains window-only visual input, with
            // ordinary window/event-route guards still owned by the unit.
            let visual = NativeReceiverContinuity(element: requested, window: window, state: state(nil))!
            try visual.validate(state(nil))
            try visual.validate(state(element))
            // A reported window is not a specific input control. Do not reject
            // a later editor just because the provider exposed only its window
            // at the start; concrete -> window still fails above.
            let windowOnly = NativeReceiverContinuity(element: requested, window: window, state: state(window))!
            try windowOnly.validate(state(window))
            try windowOnly.validate(state(nil))
            try windowOnly.validate(state(element))
        }
        // Explicit Element contracts retain existing selected contained/virtual
        // relations; raw AX focus identity does not override their semantics.
        precondition(NativeReceiverContinuity(element: element, window: window, state: state(sibling)) == nil)
        precondition(state(sibling, related: element).receiver(element) == "yes")
        for direction in ["up", "down", "left", "right"] {
            let request = try NativeScrollIntent(unit: "native_scroll", kind: "scroll", parameters: ["direction": direction, "pages": 2], foreground: false)
            precondition(request.direction == direction && request.pages == 2)
        }
        for parameters: [String: Any] in [[:], ["direction": "back"], ["direction": "down", "pages": 0],
                ["direction": "up", "pages": 0.5], ["direction": "up", "pages": true],
                ["direction": "left", "pages": Double.infinity], ["direction": "right", "x": 10]] {
            do {
                _ = try NativeScrollIntent(unit: "native_scroll", kind: "scroll", parameters: parameters, foreground: false)
                preconditionFailure("invalid page scroll accepted")
            } catch let error as DriverError { precondition(error.code == .invalidRequest) }
        }
        for p in clicks {
            for unit in ["native_action", "prepare_receiver"] {
                let request = try NativeObjectIntent(unit: unit, kind: "click", parameters: p)
                precondition(request.value == nil)
            }
        }
        let preparedMenu = try NativeObjectIntent(unit: "prepare_receiver", kind: "openMenu", parameters: [:])
        let preparedAction = try NativeObjectIntent(unit: "prepare_receiver", kind: "action", parameters: ["action": "AXPress"])
        precondition(preparedMenu.value == nil && preparedAction.value == "AXPress")
        let menu = try NativeObjectIntent(unit: "native_action", kind: "openMenu", parameters: [:])
        let action = try NativeObjectIntent(unit: "native_action", kind: "action", parameters: ["action": "AXPress"])
        let clearing = try NativeObjectIntent(unit: "native_text_edit", kind: "replace", parameters: ["text": ""])
        let inserting = try NativeObjectIntent(unit: "native_text_edit", kind: "insert", parameters: ["text": "中文🙂"])
        precondition(inserting.kind == "insert" && inserting.value == "中文🙂")
        precondition(menu.value == nil && action.value == "AXPress" && clearing.value == "")
        let invalid: [(String, String, [String: Any])] = [
            ("prepare_receiver", "click", ["count": 2]),
            ("prepare_receiver", "click", ["x": 1, "y": 2]),
            ("prepare_receiver", "replace", ["text": "not a menu intent"]),
            ("native_action", "click", ["x": 1, "y": 2]),
            ("native_action", "click", ["button": "right"]),
            ("native_action", "click", ["button": "middle"]),
            ("native_action", "click", ["count": 2]),
            ("native_action", "click", ["count": true]),
            ("native_action", "click", ["button": NSNull()]),
            ("native_action", "openMenu", ["action": "Delete"]),
            ("native_action", "action", ["action": ""]),
            ("native_action", "replace", ["text": "wrong unit"]),
            ("native_text_edit", "click", [:]),
            ("native_action", "click", ["skipValidation": true]),
            ("native_text_edit", "replace", ["text": "line\ncommand"])
        ]
        for (unit, kind, p) in invalid {
            do {
                _ = try NativeObjectIntent(unit: unit, kind: kind, parameters: p)
                preconditionFailure("accepted incompatible native object command")
            } catch let error as DriverError { precondition(error.code == .invalidRequest || error.code == .executionFailed) }
        }
        for unit in ["prepare_receiver", "key_sequence"] {
            let request = try NativeKeyboardIntent(unit: unit, kind: "key", parameters: ["key": "super+p"], foreground: false)
            precondition(request.key == "super+p")
        }
        let focus = try NativeKeyboardIntent(unit: "prepare_receiver", kind: "focus", parameters: [:], foreground: true)
        precondition(focus.key == nil)
        for (kind, unit, text) in [("insert", "key_sequence", "Hello 中文 e\u{301} 😀"), ("paste", "clipboard_paste", "A\tB\nC\rD"),
                                   ("replace", "key_sequence", "Replace 中文 😀"), ("replace", "key_sequence", "")] {
            for selected in ["prepare_receiver", unit] {
                let request = try NativeKeyboardIntent(unit: selected, kind: kind, parameters: ["text": text], foreground: false)
                precondition(request.kind.rawValue == kind && request.value == text && request.key == nil)
            }
        }
        let invalidKeys: [(String, String, [String: Any], Bool)] = [
            ("key_sequence", "focus", [:], false),
            ("key_sequence", "key", ["key": "Return"], true),
            ("prepare_receiver", "key", ["key": "super+super+p"], false),
            ("prepare_receiver", "key", ["key": "not-a-key"], false),
            ("prepare_receiver", "focus", ["key": "Return"], false),
            ("prepare_receiver", "insert", ["text": "text\nsubmit"], false),
            ("prepare_receiver", "paste", ["text": "text\u{0}"], false),
            ("prepare_receiver", "insert", ["text": ""], false),
            ("clipboard_paste", "paste", ["text": ""], false),
            ("key_sequence", "paste", ["text": "paste"], false),
            ("clipboard_paste", "insert", ["text": "insert"], false),
            ("clipboard_paste", "replace", ["text": "replace"], false),
            ("prepare_receiver", "replace", ["text": "replace\nsubmit"], false),
            ("key_sequence", "insert", ["text": "text"], true),
            ("clipboard_paste", "paste", ["text": "paste"], true),
            ("prepare_receiver", "insert", ["text": "text", "replace": true], false),
            ("native_action", "key", ["key": "Return"], false),
            ("key_sequence", "key", ["key": "Return", "skipValidation": true], false)
        ]
        for (unit, kind, parameters, foreground) in invalidKeys {
            do {
                _ = try NativeKeyboardIntent(unit: unit, kind: kind, parameters: parameters, foreground: foreground)
                preconditionFailure("invalid key intent admitted before preparation")
            } catch let error as DriverError { precondition(error.code == .invalidRequest) }
        }
        // Inspect real CGEvent encoding without posting input or requiring a UI.
        let source = CGEventSource(stateID: .privateState)!
        func text(_ event: CGEvent) -> String {
            var units = [UniChar](repeating: 0, count: 8), count = 0
            event.keyboardGetUnicodeString(maxStringLength: units.count, actualStringLength: &count, unicodeString: &units)
            return String(utf16CodeUnits: units, count: count)
        }
        for scalar in "A中e\u{301}😀".unicodeScalars {
            let (down, up) = try Input.unicodeEvents(scalar, source: source)
            precondition(down.type == .keyDown && up.type == .keyUp)
            precondition(text(down) == String(scalar) && text(up) == String(scalar))
        }
        let (shifted, released) = try Input.unicodeEvents("A", source: source, key: (0, .maskShift))
        precondition(shifted.flags == .maskShift && released.flags == .maskShift)
        let (_, globalRelease) = try Input.unicodeEvents("A", source: source, key: (0, .maskShift), foreground: true)
        precondition(globalRelease.flags.isEmpty)
        print("PASS native commands preserve intent, Unicode scalars and paste-only multiline semantics")
    }
}
