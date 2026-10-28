/// Native window-focus evidence for process-addressed keyboard events. A
/// window number in a posted event is not itself proof of the receiving window.
enum NativeReceiverEvidence {
    static func windowRoute(focusedWindowMatches: Bool?, focusPresent: Bool,
                            focusWindowMatches: Bool?, deliveryReady: Bool = true) -> String {
        if focusedWindowMatches == false || (focusPresent && focusWindowMatches == false) { return "no" }
        guard focusedWindowMatches == true else { return "unknown" }
        // Window-level input can use the declared current responder without a
        // complete AX tree. A visible conflicting/unknown owner is different.
        if focusPresent && focusWindowMatches == nil { return "unknown" }
        return deliveryReady ? "yes" : "no"
    }
}
