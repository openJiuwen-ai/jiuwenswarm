@main struct NativeReceiverEvidenceTests {
    static func main() {
        // Window-scoped keys do not require a full tree or named input field.
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: true, focusPresent: false, focusWindowMatches: nil) == "yes")
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: true, focusPresent: true, focusWindowMatches: true) == "yes")
        // Cached AX focus alone does not establish background event delivery.
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: true, focusPresent: true, focusWindowMatches: true, deliveryReady: false) == "no")
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: true, focusPresent: false, focusWindowMatches: nil, deliveryReady: false) == "no")
        // A window ID on an event cannot override contradictory AX ownership.
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: true, focusPresent: true, focusWindowMatches: false) == "no")
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: false, focusPresent: true, focusWindowMatches: true) == "no")
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: false, focusPresent: false, focusWindowMatches: nil) == "no")
        // Missing a parent/window relation is not an affirmative receiver.
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: true, focusPresent: true, focusWindowMatches: nil) == "unknown")
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: nil, focusPresent: true, focusWindowMatches: true) == "unknown")
        precondition(NativeReceiverEvidence.windowRoute(focusedWindowMatches: nil, focusPresent: false, focusWindowMatches: nil) == "unknown")
        print("PASS native receiver: exact window, missing tree, conflicting ownership and unknown focus")
    }
}
