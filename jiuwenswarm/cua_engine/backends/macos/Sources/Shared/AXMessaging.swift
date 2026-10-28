import ApplicationServices

/// A native request limit, separate from the whole observation/operation budget.
/// AX timeouts belong to the exact proxy object (not its provider or equal
/// proxies). Discovery may use shorter probes, so every mutation reconfigures
/// its recipient instead of inheriting the last observer's timeout.
enum AXMessaging {
    static let timeoutSeconds: Float = 2

    @discardableResult
    static func configure(_ element: AXUIElement) -> AXError {
        AXUIElementSetMessagingTimeout(element, timeoutSeconds)
    }

    static func performAction(_ element: AXUIElement, _ action: CFString) -> AXError {
        let status = configure(element)
        guard status == .success else { return status }
        return AXUIElementPerformAction(element, action)
    }

    static func setAttributeValue(_ element: AXUIElement, _ attribute: CFString, _ value: CFTypeRef) -> AXError {
        let status = configure(element)
        guard status == .success else { return status }
        return AXUIElementSetAttributeValue(element, attribute, value)
    }
}
