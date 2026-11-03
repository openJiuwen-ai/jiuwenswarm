import ApplicationServices

/// A window-scoped primitive keeps the usable AX receiver observed after
/// preparation. This is a local lifetime guard, not editor discovery or policy.
/// Explicit Element requests keep their separately selected receiver relation.
@MainActor struct NativeReceiverContinuity {
    struct Lost: ClassifiedFailure, CustomStringConvertible {
        let description: String
        let code: FailureCode
    }

    private let focus: AXUIElement?

    init?(element: AXUIElement?, window: AXUIElement, state: NativeReceiverSnapshot) {
        guard element == nil || CFEqual(element!, window) else { return nil }
        // Some providers expose only the window as AXFocusedUIElement. That
        // proves no control identity beyond the unit's existing window guard;
        // the first character may legitimately instantiate an editor.
        focus = state.focus.flatMap { CFEqual($0, window) ? nil : $0 }
    }

    func validate(_ state: NativeReceiverSnapshot) throws {
        // An initially unavailable AX receiver preserves the existing visual
        // window route. Never adopt a new object midway through this primitive.
        guard let focus else { return }
        guard let current = state.focus else {
            throw Lost(description: "the previously observed keyboard receiver is unavailable; remaining input was not sent",
                       code: .inputUnavailable)
        }
        guard CFEqual(current, focus) else {
            // AXFocused on the old object may be stale. A new suggestion or
            // field editor needs an explicit semantic relation chosen in Python;
            // sharing a window or ancestor alone cannot authorize a handoff.
            throw Lost(description: "the keyboard receiver changed during this input; remaining input was not sent",
                       code: .staleTarget)
        }
    }
}
