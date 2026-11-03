import AppKit
import ApplicationServices

/// One preselected full-text replacement recipe. The original Element and
/// receiver are owned by the surrounding operation; no editor lookup occurs.
@MainActor struct NativeReplacementUnit {
    enum Selection: Equatable { case range, shortcut }
    let element: AXUIElement

    func contract() -> Selection? {
        guard AXAccess.read(element, "AXEditable").value as? Bool != false else { return nil }
        let editable = AXAccess.read(element, "AXEditable").value as? Bool == true
            || AXAccess.writable(element, kAXValueAttribute).value == true
            || AXAccess.writable(element, kAXSelectedTextAttribute).value == true
        guard editable else { return nil }
        if AXAccess.writable(element, kAXSelectedTextRangeAttribute).value == true { return .range }
        // Declared readable selection is needed to verify Select All. A role,
        // focusable flag or key-post return code cannot stand in for this fact.
        var attributes: CFArray?
        guard AXUIElementCopyAttributeNames(element, &attributes) == .success,
              (attributes as? [String])?.contains(kAXSelectedTextRangeAttribute) == true else { return nil }
        return .shortcut
    }

    func snapshot() -> NativeTextSelection? {
        NativeTextSelection.read { AXAccess.read(element, $0) }
    }

    func selectRange(count: Int, check: @MainActor () throws -> Void,
                     onDispatch: @MainActor () throws -> Void, onPosted: @MainActor () -> Void) throws {
        var all = CFRange(location: 0, length: count)
        guard count >= 0, let value = AXValueCreate(.cfRange, &all) else {
            throw DriverError("cannot allocate replacement selection", code: .inputUnavailable)
        }
        try check(); try onDispatch()
        let status = AXMessaging.setAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, value)
        guard status == .success else {
            throw DriverError("replacement selection returned AX error \(status.rawValue); effect unknown, not retried",
                              code: .executionUnknown, effect: .possible)
        }
        onPosted()
    }

    /// Returns true only for an already-empty target replaced with empty text.
    /// Selection mutation and content delivery share one dispatch barrier. Any
    /// uncertain selection result stops here; there is no second recipe.
    func deliver(text: String, selection: Selection, recipient: BackgroundEvents.KeyboardRecipient,
                 applicationPID: pid_t, check: @escaping @MainActor () throws -> Void,
                 onDispatch: @escaping @MainActor () throws -> Void, onPosted: @escaping @MainActor () -> Void,
                 onCleanupUnknown: @escaping () -> Void) async throws -> Bool {
        func validate() throws {
            try check()
            guard contract() == selection else {
                throw DriverError("replacement editing/selection contract changed", code: .inputUnavailable)
            }
        }
        try validate()
        guard let before = snapshot() else {
            throw DriverError("target text extent is unavailable; no replacement sent", code: .inputUnavailable)
        }
        if before.count == 0 && text.isEmpty { return true }
        if !before.full {
            switch selection {
            case .range:
                try selectRange(count: before.count, check: validate, onDispatch: onDispatch, onPosted: onPosted)
            case .shortcut:
                try await Input.preparedKey("super+a", recipient: recipient, validate: validate,
                    onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
            }
        }
        // Selection changes may be asynchronous. Observe only, without
        // repeating Select All or comparing normal formatted text values.
        for attempt in 0..<12 {
            try validate()
            if snapshot()?.full == true { break }
            if attempt < 11 { try await Task.sleep(for: .milliseconds(25)) }
        }
        var contentStarted = false
        func beforeContent() throws {
            try validate()
            if !contentStarted, snapshot()?.full != true {
                throw DriverError("the target does not have a confirmed full-text selection; no replacement text sent",
                                  code: .inputUnavailable)
            }
        }
        func posted() { contentStarted = true; onPosted() }
        if text.isEmpty {
            try await Input.preparedKey("BackSpace", recipient: recipient, validate: beforeContent,
                onDispatch: onDispatch, onPosted: posted, onCleanupUnknown: onCleanupUnknown)
        } else {
            try await Input.preparedText(text, recipient: recipient, applicationPID: applicationPID, validate: beforeContent,
                onDispatch: onDispatch, onPosted: posted, onCleanupUnknown: onCleanupUnknown)
        }
        return false
    }
}
