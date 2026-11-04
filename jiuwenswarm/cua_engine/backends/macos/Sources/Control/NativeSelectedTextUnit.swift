import AppKit
import ApplicationServices

/// One native selection edit on the exact selected object. The caller owns
/// semantic editor selection, scope, dependency checks and fallback policy.
@MainActor struct NativeSelectedTextUnit {
    struct Contract: Equatable {
        let role: String
        let replace: Bool
    }
    let application: NSRunningApplication
    let window: AXUIElement
    let element: AXUIElement
    var receiverRelation: NativeReceiverRelation? = nil

    func contract(replace: Bool) -> Contract? {
        guard let role = AXAccess.read(element, kAXRoleAttribute).value as? String, !role.isEmpty,
              AXAccess.read(element, "AXEditable").value as? Bool != false,
              AXAccess.writable(element, kAXSelectedTextAttribute).value == true,
              !replace || AXAccess.writable(element, kAXSelectedTextRangeAttribute).value == true else { return nil }
        return Contract(role: role, replace: replace)
    }

    func readiness(_ expected: Contract) throws -> NativeReadiness {
        let receiver = try NativeReceiverSnapshot.read(application: application, window: window, receiverRelation: receiverRelation).receiver(element)
        // Insertion uses the editor's current selection; it does not need the
        // whole value or a readable extent. Full replacement does.
        if !expected.replace { return NativeReadiness([("receiver", receiver)]) }
        let selection = NativeTextSelection.read { AXAccess.read(element, $0) }
        let known = selection.map { $0.contiguous && $0.location != nil && $0.length != nil } ?? false
        return NativeReadiness([("receiver", receiver), ("selection", known ? "yes" : "unknown")])
    }

    private func validate(_ expected: Contract, check: @MainActor () throws -> Void) throws {
        try check()
        guard contract(replace: expected.replace) == expected,
              AXAccess.read(element, kAXEnabledAttribute).value as? Bool != false else {
            throw DriverError("the selected native text-edit contract changed", code: .inputUnavailable)
        }
        try BackgroundTarget(application).validate(input: true)
        try RequestDiagnostics.checkCancellation()
    }

    func prepare(_ expected: Contract, foreground: Bool, check: @escaping @MainActor () throws -> Void,
                 onPreparation: @escaping @MainActor (String) -> Void) async throws {
        let receiver = NativeReceiverPreparation(application: application, window: window, element: element, receiverRelation: receiverRelation)
        try await receiver.prepare(foreground: foreground,
            check: { try validate(expected, check: check) }, ready: { try readiness(expected).ready },
            onPreparation: onPreparation)
    }

    /// Selection mutation and content mutation share the dispatch barrier. An
    /// uncertain setter result is not permission to send the text via keys.
    func deliver(_ expected: Contract, text: String, check: @escaping @MainActor () throws -> Void,
                 onDispatch: @escaping @MainActor () throws -> Void,
                 onPosted: @escaping @MainActor () -> Void) async throws -> Bool {
        try validateText(text)
        func ready() throws {
            try validate(expected, check: check)
            guard try readiness(expected).ready else {
                throw DriverError("native editor receiver or selection changed; no further text sent", code: .inputUnavailable)
            }
        }
        try ready()
        if !expected.replace && text.isEmpty { return true }
        if expected.replace {
            let selection = NativeReplacementUnit(element: element)
            guard let before = selection.snapshot() else {
                throw DriverError("target text extent is unavailable", code: .inputUnavailable)
            }
            if before.count == 0 && text.isEmpty { return true }
            if !before.full {
                try selection.selectRange(count: before.count, check: ready, onDispatch: onDispatch, onPosted: onPosted)
            }
            for attempt in 0..<12 {
                try ready()
                if selection.snapshot()?.full == true { break }
                if attempt < 11 { try await Task.sleep(for: .milliseconds(25)) }
            }
        }
        try ready()
        if expected.replace {
            guard NativeReplacementUnit(element: element).snapshot()?.full == true else {
                throw DriverError("the editor does not have a confirmed full-text selection; no replacement text sent",
                                  code: .inputUnavailable)
            }
        }
        try onDispatch()
        let status = AXMessaging.setAttributeValue(element, kAXSelectedTextAttribute as CFString, text as CFString)
        guard status == .success else {
            throw DriverError("native selected-text edit returned AX error \(status.rawValue); effect unknown, not retried",
                              code: .executionUnknown, effect: .possible)
        }
        onPosted()
        return false
    }
}
