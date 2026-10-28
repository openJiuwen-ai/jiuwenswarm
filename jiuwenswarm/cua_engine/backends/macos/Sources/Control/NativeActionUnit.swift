import AppKit
import ApplicationServices

/// A single addressed AX action, without focus/pointer/keyboard fallbacks.
/// Only application-menu-bar commands depend on the selected document receiver.
/// An observed local popup retains its own context and uses the addressed action.
extension NativeElementControl {
    struct ActionFacts {
        let role: String?
        let actions: [NativeAction.Item]?
        let enabled: Bool?
        let applicationMenuContext: Bool
        let foreground: Bool
    }

    func nativeActionFacts(_ ref: String) throws -> ActionFacts {
        guard let target = registry.target(ref) else { throw DriverError("element reference expired", code: .staleTarget) }
        try BackgroundTarget(target.application).validate()
        try inspection.validateTargetIdentity(target)
        let role = inspection.attribute(target.element, kAXRoleAttribute) as? String
        return ActionFacts(role: role, actions: try declaredActions(target.element),
            enabled: (inspection.attribute(target.element, kAXEnabledAttribute) as? NSNumber)?.boolValue,
            applicationMenuContext: [kAXMenuBarRole, kAXMenuBarItemRole].contains(role ?? "")
                || target.ancestors.contains { inspection.attribute($0, kAXRoleAttribute) as? String == kAXMenuBarRole },
            foreground: BackgroundTarget(target.application).isForeground)
    }

    func executeNativeAction(_ ref: String, action: String, description: String?, onDispatch: @MainActor () throws -> Void) throws {
        let target = try inputTarget(ref)
        let facts = try nativeActionFacts(ref)
        guard !facts.applicationMenuContext else {
            throw DriverError("application menu actions require the menu-context preparation unit", code: .inputUnavailable)
        }
        guard let actions = facts.actions,
              actions.filter({ $0.name == action && $0.description == description }).count == 1 else {
            throw DriverError("the selected native action is no longer declared", code: .inputUnavailable)
        }
        let background = BackgroundTarget(target.application)
        if action == kAXRaiseAction, !BackgroundTarget.isExclusive, !background.isForeground {
            throw DriverError("raising a background window requires exclusive mode", code: .modeUnsupported)
        }
        if !background.allowsMenuPopup, [kAXPressAction, kAXPickAction].contains(action), inspection.opensMenu(target.element) {
            throw DriverError("opening an application menu is unavailable in this desktop mode", code: .modeUnsupported)
        }
        try inspection.requireEnabled(target.element)
        try inspection.validateTargetIdentity(target)
        try background.validate(input: true)
        try RequestDiagnostics.checkCancellation()
        try onDispatch() // Final window guard and barrier BEFORE the native call.
        let status = AXMessaging.performAction(target.element, action as CFString)
        guard status == .success else {
            throw DriverError("native action returned AX error \(status.rawValue); effect unknown, not retried",
                              code: .executionUnknown, effect: .possible)
        }
    }
}
