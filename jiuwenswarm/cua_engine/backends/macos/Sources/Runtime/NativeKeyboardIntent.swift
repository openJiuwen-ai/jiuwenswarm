import Foundation

/// Validate the original intent before any receiver preparation. Preparation
/// carries the same intent as delivery; insert, paste and replace never alias.
@MainActor struct NativeKeyboardIntent {
    enum Kind: String {
        case focus, key, insert, paste, replace
        var unit: String {
            switch self {
            case .focus: return "prepare_receiver"
            case .key, .insert, .replace: return "key_sequence"
            case .paste: return "clipboard_paste"
            }
        }
    }
    let kind: Kind
    let value: String?
    var key: String? { kind == .key ? value : nil }

    /// AppKit handles Command-key equivalents at the application/window
    /// level, before ordinary first-responder text delivery. A valid element
    /// receiver alone does not prepare that route in a cold document.
    static func usesWindowCommand(_ kind: Kind, key: String?, replacementShortcut: Bool = false) -> Bool {
        kind == .paste || (kind == .replace && replacementShortcut)
            || (kind == .key && key?.split(separator: "+").dropLast().contains("super") == true)
    }

    func usesWindowCommand(replacementShortcut: Bool = false) -> Bool {
        Self.usesWindowCommand(kind, key: key, replacementShortcut: replacementShortcut)
    }

    init(unit: String, kind: String, parameters: Any?, foreground: Bool) throws {
        guard let kind = Kind(rawValue: kind),
              unit == "prepare_receiver" || unit == kind.unit,
              !foreground || unit == "prepare_receiver" else {
            throw DriverError("invalid keyboard unit", code: .invalidRequest)
        }
        self.kind = kind
        let field = kind == .key ? "key" : "text"
        let p = try object(parameters, keys: kind == .focus ? [] : [field])
        value = kind == .focus ? nil : try string(p[field], label: field, nonempty: kind == .key)
        // The public engine completes empty insert/paste as a no-op before
        // choosing any primitive. A raw unit must not focus or claim dispatch.
        if (kind == .insert || kind == .paste) && value!.isEmpty {
            throw DriverError("empty text requires no input primitive", code: .invalidRequest)
        }
        switch kind {
        case .focus: break
        case .key: try Input.validateKey(value!)
        case .insert, .replace: try validateText(value!)
        case .paste: try validatePasteText(value!)
        }
    }
}
