import AppKit
import ApplicationServices
import Carbon

@MainActor
enum Input {
    private static let keys: [String: CGKeyCode] = [
        "a":0, "s":1, "d":2, "f":3, "h":4, "g":5, "z":6, "x":7, "c":8, "v":9,
        "b":11, "q":12, "w":13, "e":14, "r":15, "y":16, "t":17,
        "1":18, "2":19, "3":20, "4":21, "6":22, "5":23, "9":25, "7":26, "8":28, "0":29,
        "o":31, "u":32, "i":34, "p":35, "l":37, "j":38, "k":40, "n":45, "m":46,
        "Return":36, "Tab":48, "space":49, "BackSpace":51, "Escape":53, "Delete":117,
        "Home":115, "End":119, "Page_Up":116, "Page_Down":121,
        "Left":123, "Right":124, "Down":125, "Up":126,
        "F1": CGKeyCode(kVK_F1), "F2": CGKeyCode(kVK_F2), "F3": CGKeyCode(kVK_F3), "F4": CGKeyCode(kVK_F4),
        "F5": CGKeyCode(kVK_F5), "F6": CGKeyCode(kVK_F6), "F7": CGKeyCode(kVK_F7), "F8": CGKeyCode(kVK_F8),
        "F9": CGKeyCode(kVK_F9), "F10": CGKeyCode(kVK_F10), "F11": CGKeyCode(kVK_F11), "F12": CGKeyCode(kVK_F12)
    ]
    private static let modifiers: [String: (CGKeyCode, CGEventFlags)] = [
        "super":(55, .maskCommand), "ctrl":(59, .maskControl),
        "alt":(58, .maskAlternate), "shift":(56, .maskShift)
    ]

    private static func asciiKeys() -> [String: (CGKeyCode, CGEventFlags)] {
        // Some apps retranslate key codes and ignore CGEvent's Unicode field.
        // Match printable ASCII to the current layout so both paths receive the same text.
        let source = TISCopyCurrentKeyboardLayoutInputSource().takeRetainedValue()
        let fallback = TISCopyCurrentASCIICapableKeyboardLayoutInputSource().takeRetainedValue()
        guard let property = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData)
            ?? TISGetInputSourceProperty(fallback, kTISPropertyUnicodeKeyLayoutData) else { return [:] }
        let data = unsafeBitCast(property, to: CFData.self)
        guard let bytes = CFDataGetBytePtr(data) else { return [:] }
        let layout = UnsafeRawPointer(bytes).assumingMemoryBound(to: UCKeyboardLayout.self)
        var result = [String: (CGKeyCode, CGEventFlags)]()
        for shifted in [false, true] {
            for code in UInt16(0)..<128 {
                var dead: UInt32 = 0, length = 0
                var buffer = [UniChar](repeating: 0, count: 8)
                let status = UCKeyTranslate(layout, code, UInt16(kUCKeyActionDown), shifted ? UInt32(shiftKey >> 8) : 0,
                    UInt32(LMGetKbdType()), OptionBits(1 << kUCKeyTranslateNoDeadKeysBit), &dead, buffer.count, &length, &buffer)
                guard status == noErr, length == 1, (32...126).contains(buffer[0]) else { continue }
                let text = String(utf16CodeUnits: buffer, count: length)
                if result[text] == nil { result[text] = (code, shifted ? .maskShift : []) }
            }
        }
        return result
    }

    static func source() throws -> CGEventSource {
        guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier != "com.apple.loginwindow" else {
            throw DriverError("desktop is locked or unavailable; unlock the Mac before sending input", code: .desktopUnavailable)
        }
        guard CGPreflightPostEventAccess(), AXIsProcessTrusted() else {
            throw DriverError("accessibility permission denied; enable the driver or its launching terminal in System Settings > Privacy & Security", code: .desktopUnavailable)
        }
        guard let source = CGEventSource(stateID: .privateState) else { throw DriverError("cannot create input source") }
        return source
    }


    /// Global input deliberately has no AX window-preparation dependency.
    static func requireDesktopInput() throws {
        guard BackgroundTarget.isExclusive else { throw DriverError("desktop input requires exclusive mode; shared mode does not permit global input", code: .modeUnsupported, next: "use App/Element operations in shared mode; desktop mode is host configuration") }
        try RequestDiagnostics.checkCancellation()
        try BackgroundTarget.validateDesktop(input: true)
    }

    private static func chord(_ key: String) throws -> (code: CGKeyCode, modifiers: [String]) {
        let parts = key.split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard let last = parts.last, let code = keys[last] else { throw DriverError("unsupported key: \(key)", code: .invalidRequest) }
        let names = Array(parts.dropLast())
        guard Set(names).count == names.count, names.allSatisfy({ modifiers[$0] != nil }) else {
            throw DriverError("invalid modifiers: \(key)", code: .invalidRequest)
        }
        return (code, names)
    }

    /// Validation must happen before an element command performs focus preparation.
    static func validateKey(_ key: String) throws { _ = try chord(key) }

    private struct KeyEvents {
        let source: CGEventSource
        let downs: [CGEvent]
        let ups: [CGEvent]
    }

    /// Allocate the complete chord before any preparation or key-down. Both
    /// App/Element and desktop units share Return's ordinary text encoding.
    private static func keyEvents(_ key: String) throws -> KeyEvents {
        let (code, names) = try chord(key)
        let source = try source()
        let codes = names.map { modifiers[$0]!.0 } + [code]
        var downs = [CGEvent](), ups = [CGEvent]()
        for (index, item) in codes.enumerated() {
            guard let down = CGEvent(keyboardEventSource: source, virtualKey: item, keyDown: true),
                  let up = CGEvent(keyboardEventSource: source, virtualKey: item, keyDown: false) else {
                throw DriverError("cannot create keyboard events")
            }
            if item == CGKeyCode(kVK_Return) {
                var character = UniChar(kReturnCharCode)
                down.keyboardSetUnicodeString(stringLength: 1, unicodeString: &character)
                up.keyboardSetUnicodeString(stringLength: 1, unicodeString: &character)
            }
            down.flags = names.prefix(min(index + 1, names.count)).reduce(CGEventFlags()) { $0.union(modifiers[$1]!.1) }
            up.flags = names.prefix(min(index, names.count)).reduce(CGEventFlags()) { $0.union(modifiers[$1]!.1) }
            downs.append(down); ups.append(up)
        }
        return KeyEvents(source: source, downs: downs, ups: ups)
    }

    /// Selected primitive: no target search, focus preparation, activation,
    /// fallback, or post-input focus requirement. The caller checks its exact
    /// receiver before every down; releases always use the captured route.
    /// nil recipient is the explicit exclusive desktop route, never a fallback
    /// for a missing application recipient; withPreparedRoute enforces its mode.
    static func preparedKey(_ key: String, recipient: BackgroundEvents.KeyboardRecipient?,
                            validate: @escaping @MainActor () throws -> Void, onDispatch: @escaping @MainActor () throws -> Void,
                            onPosted: @escaping @MainActor () -> Void, onCleanupUnknown: @escaping () -> Void) async throws {
        let events = try keyEvents(key)
        try await withPreparedRoute(source: events.source, recipient: recipient, validate: validate,
            onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown) { press, release in
            for index in events.downs.indices { try press(events.downs[index], events.ups[index]) }
            try await Task.sleep(for: .milliseconds(15))
            release()
        }
    }

    /// Unicode insertion is a selected keyboard primitive, not replacement or
    /// implicit clipboard paste. The execution unit supplies the verified route.
    static func preparedText(_ text: String, recipient: BackgroundEvents.KeyboardRecipient?, applicationPID: pid_t?,
                             validate: @escaping @MainActor () throws -> Void, onDispatch: @escaping @MainActor () throws -> Void,
                             onPosted: @escaping @MainActor () -> Void, onCleanupUnknown: @escaping () -> Void) async throws {
        try validateText(text)
        let source = try source(), ascii = asciiKeys()
        try await withPreparedRoute(source: source, recipient: recipient, validate: validate,
            onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown) { press, release in
            for scalar in text.unicodeScalars {
                let (down, up) = try unicodeEvents(scalar, source: source, key: ascii[String(scalar)], foreground: recipient == nil)
                try press(down, up)
                // Keep release behind a bounded provider round trip. This is
                // delivery settling, not a post-input focus or value assertion.
                if let applicationPID {
                    let root = AXUIElementCreateApplication(applicationPID)
                    AXUIElementSetMessagingTimeout(root, 0.2)
                    var ignored: CFTypeRef?
                    _ = AXUIElementCopyAttributeValue(root, kAXFocusedUIElementAttribute as CFString, &ignored)
                } else {
                    try await Task.sleep(for: .milliseconds(10))
                }
                release()
                try await Task.sleep(for: .milliseconds(10))
            }
        }
    }

    static func unicodeEvents(_ scalar: Unicode.Scalar, source: CGEventSource,
                              key: (CGKeyCode, CGEventFlags)? = nil, foreground: Bool = false) throws -> (CGEvent, CGEvent) {
        let (code, flags) = key ?? (0, CGEventFlags())
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false) else {
            throw DriverError("cannot create Unicode input events")
        }
        down.flags = flags
        // Shift describes the character; global release must clear session
        // flags. Process-addressed events retain the character's full metadata.
        up.flags = foreground ? [] : flags
        let units = Array(String(scalar).utf16)
        units.withUnsafeBufferPointer { buffer in
            down.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
            up.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress!)
        }
        return (down, up)
    }

    private static func withPreparedRoute(source: CGEventSource, recipient: BackgroundEvents.KeyboardRecipient?,
                            validate: @escaping @MainActor () throws -> Void, onDispatch: @escaping @MainActor () throws -> Void,
                            onPosted: @escaping @MainActor () -> Void, onCleanupUnknown: @escaping () -> Void,
                            send: @MainActor (_ press: @MainActor (CGEvent, CGEvent) throws -> Void,
                                              _ release: @MainActor () -> Void) async throws -> Void) async throws {
        if recipient == nil { try requireDesktopInput() }
        let process = recipient.flatMap { NativeProcessIdentity.read($0.pid) }
        guard recipient == nil || process != nil else {
            throw DriverError("keyboard recipient is no longer identifiable", code: .staleTarget)
        }
        let pair = PairedInput<CGEvent>(source: source,
            post: { event in
                if let recipient { BackgroundEvents.postKey(event, to: recipient) }
                else { event.post(tap: .cghidEventTap) }
            },
            canRelease: { recipient.map { NativeProcessIdentity.read($0.pid) == process } ?? true }, skipped: onCleanupUnknown)
        try await pair.run {
            try await send({ down, up in
                try validate()
                guard recipient.map({ NativeProcessIdentity.read($0.pid) == process }) ?? true else {
                    throw DriverError("keyboard receiving process changed", code: .staleTarget)
                }
                try recipient?.validateFocus()
                try onDispatch()
                pair.press(down, release: up)
                onPosted()
            }, { pair.release() })
        }
    }
}
