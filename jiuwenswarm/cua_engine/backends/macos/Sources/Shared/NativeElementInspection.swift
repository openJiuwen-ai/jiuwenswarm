import AppKit
import ApplicationServices

/// Native identity and provider checks; no editor discovery or input routing.
@MainActor
final class NativeElementInspection {
    typealias Target = ApplicationElements.Target
    private let registry: ApplicationElements
    init(registry: ApplicationElements) { self.registry = registry }

    func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        AXAccess.read(element, name).value
    }

    func opensMenu(_ element: AXUIElement, withinBudget: () -> Bool = { true }) -> Bool {
        func attribute(_ object: AXUIElement, _ key: String) -> CFTypeRef? {
            guard withinBudget() else { return nil }
            return self.attribute(object, key)
        }
        let role = attribute(element, kAXRoleAttribute) as? String
        if role == kAXMenuBarItemRole { return true }
        return role == kAXMenuItemRole && (attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? [])
            .contains { attribute($0, kAXRoleAttribute) as? String == kAXMenuRole }
    }

    func providerFacts(_ element: AXUIElement, app: NSRunningApplication) -> (identity: NativeProcessIdentity?, known: Bool) {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success else { return (nil, false) }
        if pid == app.processIdentifier { return (nil, true) }
        let identity = NativeProcessIdentity.read(pid)
        return (identity, identity != nil)
    }

    func validateTargetProvider(_ target: Target) throws {
        var pid: pid_t = 0
        let status = AXUIElementGetPid(target.element, &pid)
        guard status == .success else {
            throw DriverError("element provider identity is unavailable; no input was dispatched",
                              code: status == .invalidUIElement ? .staleTarget : .inputUnavailable)
        }
        if pid == target.application.processIdentifier, target.provider == nil { return }
        guard let provider = target.provider else {
            throw DriverError("element provider identity cannot be confirmed", code: target.providerKnown ? .staleTarget : .inputUnavailable)
        }
        guard provider.pid == pid else { throw DriverError("element provider changed", code: .staleTarget) }
        guard let current = NativeProcessIdentity.read(pid) else {
            throw DriverError("element provider lifetime cannot be read", code: NativeProcessIdentity.isGone(pid) ? .staleTarget : .inputUnavailable)
        }
        guard current == provider else { throw DriverError("element provider lifetime changed", code: .staleTarget) }
        guard let window = target.ancestors.reversed().first(where: { ancestor in
                  var owner: pid_t = 0
                  return [kAXWindowRole, kAXSheetRole].contains(attribute(ancestor, kAXRoleAttribute) as? String ?? "")
                      && AXUIElementGetPid(ancestor, &owner) == .success && owner == target.application.processIdentifier
              }),
              let current = WindowGeometry.owningWindow(target.element, pid: target.application.processIdentifier, in: window) else {
            throw DriverError("element provider attachment to its observed window is unavailable", code: .inputUnavailable)
        }
        guard CFEqual(current, window) else { throw DriverError("element provider moved to another window", code: .staleTarget) }
    }

    func validateTargetIdentity(_ target: Target) throws {
        let metric = RequestDiagnostics.metrics?.start(.targetValidation)
        defer { metric?.end() }
        try validateTargetProvider(target)
        func check(_ element: AXUIElement, _ identity: ElementIdentity) -> ElementIdentity.Check {
            let evidence = AXIdentity.read(element, keys: identity.attributes.keys.sorted())
            if case .invalid = evidence { return .changed }
            let result = identity.check(evidence.value ?? .init())
            if result != .matched {
                diagnose("identity evidence \(result): expected role=\(identity.attributes[kAXRoleAttribute] ?? "unknown")")
            }
            return result
        }
        let checks = [check(target.element, target.identity)] + target.context.map { context in
            context.native.element.map { check($0, context.identity) } ?? .unavailable
        }
        if checks.contains(.changed) {
            // Do not revive a handle if a recycled native object's old label
            // later reappears. Re-observation establishes a new lifetime.
            registry.invalidate { $0.scope == target.scope && CFEqual($0.element, target.element) }
            throw DriverError("element identity or document context changed; read its window again", code: .staleTarget)
        }
        if checks.contains(.unavailable) {
            throw DriverError("element identity or document context is temporarily unconfirmed; no input was dispatched",
                              code: .inputUnavailable, next: "read the target again; its retained reference has not been rebound or retired")
        }
    }

    func requireEnabled(_ element: AXUIElement, preparedMenu: Bool = false) throws {
        guard (attribute(element, kAXEnabledAttribute) as? NSNumber)?.boolValue != false else {
            if preparedMenu {
                throw DriverError("menu command remains disabled after target preparation; requested command was not dispatched",
                    code: .inputUnavailable, next: "change the required document state or choose another command; repeating this request alone will not enable it")
            }
            throw DriverError("element is disabled", code: .inputUnavailable)
        }
    }
}
