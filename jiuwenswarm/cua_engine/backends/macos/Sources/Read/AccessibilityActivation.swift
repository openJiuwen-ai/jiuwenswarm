import AppKit
import ApplicationServices

/// Ask a lazy accessibility provider to publish its UI. This changes only the
/// provider's opt-in attribute: it does not activate an app, move focus, start
/// VoiceOver, or promise that the subsequent tree is complete.
@MainActor final class AccessibilityActivation {
    enum Attribute: String {
        case manual = "AXManualAccessibility"
        case enhanced = "AXEnhancedUserInterface"
    }

    struct Outcome: Equatable {
        enum Kind: String {
            case alreadyEnabled
            case requestReturned
            case unsupported
            case alreadyAttempted
            case processUnavailable
        }
        let kind: Kind
        var attribute: Attribute? = nil
        /// A setter returning success is still only provider acknowledgement.
        /// cannotComplete may also follow an effective write; never replay it.
        var writeStatus: AXError? = nil
    }

    // Keep only the latest observed lifetime per PID. Restarted/reused PIDs must
    // not inherit an attempt, while reparenting the same process changes none.
    private var attempted: [pid_t: NativeProcessIdentity] = [:]

    func ensure(_ app: NSRunningApplication) -> Outcome {
        guard !app.isTerminated,
              let process = NativeProcessIdentity.read(app.processIdentifier) else {
            return Outcome(kind: .processUnavailable)
        }
        let element = AXUIElementCreateApplication(process.pid)
        AXUIElementSetMessagingTimeout(element, 0.3)
        return request(process, isCurrent: {
            !app.isTerminated && NativeProcessIdentity.read(process.pid) == process
        }, readEnabled: { attribute in
            var value: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, attribute.rawValue as CFString, &value) == .success,
                  let value, CFGetTypeID(value) == CFBooleanGetTypeID() else { return nil }
            return CFBooleanGetValue((value as! CFBoolean))
        }, writeEnabled: { attribute in
            AXUIElementSetAttributeValue(element, attribute.rawValue as CFString, kCFBooleanTrue)
        })
    }

    /// Injectable native operations keep request ordering, ambiguous errors and
    /// process-lifetime suppression testable without operating anyone's apps.
    func request(_ process: NativeProcessIdentity,
                 isCurrent: () -> Bool,
                 readEnabled: (Attribute) -> Bool?,
                 writeEnabled: (Attribute) -> AXError) -> Outcome {
        guard isCurrent() else { return Outcome(kind: .processUnavailable) }
        guard attempted[process.pid] != process else { return Outcome(kind: .alreadyAttempted) }
        // Remember the attempt, not just success. In particular cannotComplete
        // is not proof of no effect and must not trigger repeated opt-in writes.
        attempted[process.pid] = process
        for attribute in [Attribute.manual, .enhanced] {
            guard isCurrent() else { return Outcome(kind: .processUnavailable) }
            let enabled = readEnabled(attribute)
            guard isCurrent() else { return Outcome(kind: .processUnavailable) }
            if enabled == true { return Outcome(kind: .alreadyEnabled, attribute: attribute) }
            let status = writeEnabled(attribute)
            // Get/set support can differ. A failed read is not permission to
            // select the legacy route; only the setter's explicit rejection is.
            if status != .attributeUnsupported {
                return Outcome(kind: .requestReturned, attribute: attribute, writeStatus: status)
            }
        }
        return Outcome(kind: .unsupported, attribute: .enhanced, writeStatus: .attributeUnsupported)
    }
}
