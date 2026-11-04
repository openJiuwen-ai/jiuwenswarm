import ApplicationServices

/// Keep provider failures distinct from an absent capability. Apple documents
/// cannotComplete as a messaging failure, not an unsupported attribute.
enum AXRead<Value> {
    case value(Value)
    case unsupported
    case unavailable(AXError)
    case invalid

    var value: Value? {
        if case .value(let value) = self { return value }
        return nil
    }

    static func result(_ error: AXError, _ value: Value?) -> Self {
        switch error {
        case .success: return value.map(Self.value) ?? .unavailable(.noValue)
        case .attributeUnsupported, .notImplemented: return .unsupported
        case .invalidUIElement: return .invalid
        default: return .unavailable(error)
        }
    }
}

@MainActor enum AXAccess {
    static func element(_ element: AXUIElement, _ key: String) -> AXUIElement? {
        guard let value = read(element, key).value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return unsafeDowncast(value, to: AXUIElement.self)
    }
    static func read(_ element: AXUIElement, _ key: String) -> AXRead<CFTypeRef> {
        var value: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, key as CFString, &value)
        return .result(status, value)
    }

    static func writable(_ element: AXUIElement, _ key: String) -> AXRead<Bool> {
        var writable = DarwinBoolean(false)
        let status = AXUIElementIsAttributeSettable(element, key as CFString, &writable)
        return .result(status, writable.boolValue)
    }

    /// Complete, bounded relationship lists. Never load a virtual collection's
    /// unbounded Children merely to establish a small control relationship.
    static func elements(_ element: AXUIElement, _ key: String, limit: Int) -> [AXUIElement]? {
        var count: CFIndex = 0
        guard AXUIElementGetAttributeValueCount(element, key as CFString, &count) == .success,
              count >= 0, count <= limit else { return nil }
        if count == 0 { return [] }
        var value: CFArray?
        guard AXUIElementCopyAttributeValues(element, key as CFString, 0, count, &value) == .success,
              let elements = value as? [AXUIElement], elements.count == count else { return nil }
        return elements
    }
}
