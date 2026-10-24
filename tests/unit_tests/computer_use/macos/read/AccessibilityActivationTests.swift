import Foundation
import ApplicationServices

@main struct AccessibilityActivationTests {
    @MainActor static func main() {
        typealias Attribute = AccessibilityActivation.Attribute
        let first = NativeProcessIdentity(pid: 123, parent: 1, seconds: 100, microseconds: 4, executable: "/fixture/editor")
        func run(_ activation: AccessibilityActivation, process: NativeProcessIdentity? = nil,
                 values: [Attribute: Bool] = [:], errors: [Attribute: AXError] = [:]) -> (AccessibilityActivation.Outcome, [String]) {
            var calls = [String]()
            let outcome = activation.request(process ?? first, isCurrent: { true }, readEnabled: {
                calls.append("read:\($0.rawValue)"); return values[$0]
            }, writeEnabled: {
                calls.append("write:\($0.rawValue)"); return errors[$0] ?? .success
            })
            return (outcome, calls)
        }

        let existing = AccessibilityActivation()
        let (already, existingCalls) = run(existing, values: [.manual: true])
        precondition(already.kind == .alreadyEnabled && already.attribute == .manual)
        precondition(existingCalls == ["read:AXManualAccessibility"])
        precondition(run(existing).1.isEmpty)

        let modern = AccessibilityActivation()
        let (requested, modernCalls) = run(modern, values: [.manual: false])
        precondition(requested.kind == .requestReturned && requested.writeStatus == .success && requested.attribute == .manual)
        precondition(modernCalls == ["read:AXManualAccessibility", "write:AXManualAccessibility"])
        precondition(run(modern).0.kind == .alreadyAttempted)

        let legacy = AccessibilityActivation()
        let (legacyOutcome, legacyCalls) = run(legacy, values: [.enhanced: false],
            errors: [.manual: .attributeUnsupported, .enhanced: .cannotComplete])
        precondition(legacyOutcome.kind == .requestReturned && legacyOutcome.attribute == .enhanced && legacyOutcome.writeStatus == .cannotComplete)
        precondition(legacyCalls == ["read:AXManualAccessibility", "write:AXManualAccessibility", "read:AXEnhancedUserInterface", "write:AXEnhancedUserInterface"])
        precondition(run(legacy).1.isEmpty, "An ambiguous but possibly effective write is never replayed")

        let (enabledLegacy, enabledCalls) = run(AccessibilityActivation(), values: [.enhanced: true], errors: [.manual: .attributeUnsupported])
        precondition(enabledLegacy.kind == .alreadyEnabled && enabledLegacy.attribute == .enhanced)
        precondition(enabledCalls.last == "read:AXEnhancedUserInterface" && enabledCalls.count == 3)

        for error in [AXError.cannotComplete, .apiDisabled, .noValue, .failure, .notImplemented, .invalidUIElement] {
            let activation = AccessibilityActivation()
            let (outcome, calls) = run(activation, errors: [.manual: error])
            precondition(outcome.writeStatus == error && outcome.attribute == .manual)
            precondition(calls.count == 2, "An uncertain failure must not select the legacy route")
            precondition(run(activation).1.isEmpty)
        }

        let unsupported = AccessibilityActivation()
        precondition(run(unsupported, errors: [.manual: .attributeUnsupported, .enhanced: .attributeUnsupported]).0.kind == .unsupported)
        precondition(run(unsupported).1.isEmpty, "Unsupported providers do not pay the same two requests on every read")

        let reparented = NativeProcessIdentity(pid: 123, parent: 999, seconds: 100, microseconds: 4, executable: "/fixture/editor")
        precondition(run(modern, process: reparented).1.isEmpty)
        let restarted = NativeProcessIdentity(pid: 123, parent: 1, seconds: 101, microseconds: 4, executable: "/fixture/editor")
        precondition(run(modern, process: restarted).1.count == 2, "A reused PID with a new lifetime must opt in again")

        var live = true, writes = 0
        let changed = AccessibilityActivation().request(first, isCurrent: { live }, readEnabled: { _ in live = false; return false },
            writeEnabled: { _ in writes += 1; return .success })
        precondition(changed.kind == .processUnavailable && writes == 0, "Do not send an opt-in write after process identity changes")
        print("PASS: provider activation preserves existing state, prefers manual, falls back only on unsupported, and requests once per process lifetime")
    }
}
