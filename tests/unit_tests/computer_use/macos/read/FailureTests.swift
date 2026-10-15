import Foundation

@main struct FailureTests {
    static func main() {
        let blocked = DriverError("control is behind a dialog", code: .blockedByModal, next: "read window 2")
        let bridged: Error = blocked
        precondition(bridged.localizedDescription == blocked.description)
        precondition((blocked as NSError).localizedDescription == blocked.description)
        precondition(FailureEffect.none.including(.possible) == .possible)
        precondition(FailureEffect.partial.including(.possible) == .partial)
        // Target prose can contain arbitrary application text; it cannot classify errors.
        precondition(DriverError("snapshot_expired; blocked_by_modal; no effect").code == .executionFailed)
        print("PASS structured errors survive Foundation bridging and effect evidence never decreases")
    }
}
