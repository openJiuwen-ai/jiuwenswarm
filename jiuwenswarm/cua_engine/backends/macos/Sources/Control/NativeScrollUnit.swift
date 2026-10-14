import AppKit
import ApplicationServices

/// Execute Python's exact region -> bar -> page-button selection. This unit
/// never finds a different button, walks into another scrolling region, focuses
/// the app, or falls back to wheel input after an uncertain page action.
@MainActor struct NativeScrollUnit {
    let requested: AXUIElement
    let region: AXUIElement
    let bar: AXUIElement
    let button: AXUIElement

    private func read(_ element: AXUIElement, _ key: String) throws -> CFTypeRef? {
        try RequestDiagnostics.checkCancellation()
        let result = AXAccess.read(element, key)
        switch result {
        case .value(let value): return value
        case .unsupported: return nil
        case .invalid: throw DriverError("native scrolling object expired", code: .staleTarget)
        case .unavailable(let error):
            if error == .noValue { return nil }
            throw DriverError("native scrolling relationship is unavailable", code: .inputUnavailable)
        }
    }

    private func element(_ value: AXUIElement, _ key: String) throws -> AXUIElement? {
        guard let raw = try read(value, key) else { return nil }
        guard CFGetTypeID(raw) == AXUIElementGetTypeID() else {
            throw DriverError("native scroll relationship has an invalid type", code: .staleTarget)
        }
        return unsafeDowncast(raw, to: AXUIElement.self)
    }

    private func validateParent(_ child: AXUIElement, expected: AXUIElement) throws {
        var current = child, seen = [child]
        for _ in 0..<40 {
            guard let parent = try element(current, kAXParentAttribute) else { return }
            if CFEqual(parent, expected) { return }
            let role = try read(parent, kAXRoleAttribute) as? String
            guard !seen.contains(where: { CFEqual($0, parent) }),
                  [kAXGroupRole, kAXUnknownRole].contains(role ?? "") else {
                throw DriverError("the selected scrolling relationship has conflicting ownership", code: .staleTarget)
            }
            seen.append(parent); current = parent
        }
        throw DriverError("scrolling parent validation exceeded its depth budget", code: .inputUnavailable)
    }

    func validate(direction: String? = nil) throws {
        // The nearest scrolling ancestor is part of the selected route. A
        // later nested scroller cannot silently redirect this request outward.
        var current = requested, seen = [AXUIElement](), found = false
        for _ in 0..<40 {
            guard !seen.contains(where: { CFEqual($0, current) }) else { break }
            seen.append(current)
            let role = try read(current, kAXRoleAttribute) as? String
            guard role != nil else { throw DriverError("scrolling ancestor role is unavailable", code: .inputUnavailable) }
            if [kAXScrollAreaRole, kAXScrollBarRole].contains(role ?? "") {
                found = CFEqual(current, region); break
            }
            if [kAXWindowRole, kAXSheetRole, kAXApplicationRole].contains(role ?? "") { break }
            guard let parent = try element(current, kAXParentAttribute) else { break }
            current = parent
        }
        guard found else { throw DriverError("the nearest scrolling region changed or cannot be confirmed", code: .inputUnavailable) }
        guard try read(bar, kAXRoleAttribute) as? String == kAXScrollBarRole,
              try read(button, kAXRoleAttribute) as? String == kAXButtonRole else {
            throw DriverError("the selected native scroll control changed", code: .staleTarget)
        }
        let orientation = try read(bar, kAXOrientationAttribute) as? String
        let subrole = try read(button, kAXSubroleAttribute) as? String
        guard ["AXIncrementPage", "AXDecrementPage"].contains(subrole ?? "") else {
            throw DriverError("the selected button is no longer a page control", code: .staleTarget)
        }
        let relationships: [String]
        if let direction {
            let spec = NativeScrollAction.pageButtonSpec(direction: direction, pages: 1)!
            if CFEqual(region, bar), orientation != spec.orientation {
                throw DriverError("the addressed scroll bar does not confirm the requested axis", code: .inputUnavailable)
            }
            try NativeScrollAction.validateScrollBar(role: kAXScrollBarRole, orientation: orientation, enabled: nil, spec: spec)
            guard subrole == spec.subrole else { throw DriverError("page action does not match the requested direction", code: .invalidRequest) }
            relationships = [spec.scrollBar]
        } else { relationships = ["AXVerticalScrollBar", "AXHorizontalScrollBar"] }
        if !CFEqual(region, bar) {
            var linked = false
            for key in relationships {
                if try element(region, key).map({ CFEqual($0, bar) }) == true { linked = true; break }
            }
            guard linked else { throw DriverError("the selected scroll bar left its region", code: .inputUnavailable) }
            try validateParent(bar, expected: region)
        }
        try validateParent(button, expected: bar)
        var count: CFIndex = 0
        guard AXUIElementGetAttributeValueCount(bar, kAXChildrenAttribute as CFString, &count) == .success,
              (1...256).contains(count) else {
            throw DriverError("scroll-bar children cannot be checked within the native budget", code: .inputUnavailable)
        }
        var raw: CFArray?
        guard AXUIElementCopyAttributeValues(bar, kAXChildrenAttribute as CFString, 0, count, &raw) == .success,
              let children = raw as? [AXUIElement], children.count == count,
              children.contains(where: { CFEqual($0, button) }) else {
            throw DriverError("the selected page button is no longer in the selected scroll bar", code: .inputUnavailable)
        }
        for control in [requested, region, bar, button] {
            if try read(control, kAXEnabledAttribute) as? Bool == false {
                throw DriverError("the selected scrolling control is disabled", code: .inputUnavailable)
            }
        }
        var actions: CFArray?
        guard AXUIElementCopyActionNames(button, &actions) == .success,
              (actions as? [String])?.filter({ $0 == kAXPressAction }).count == 1 else {
            throw DriverError("the page button no longer declares its press action", code: .inputUnavailable)
        }
    }

    func perform(_ request: NativeScrollIntent, check: () throws -> Void,
                 onDispatch: () throws -> Void, onAccepted: () -> Void) async throws {
        for _ in 0..<request.pages {
            await Task.yield()
            try check()
            try validate(direction: request.direction)
            try onDispatch()
            let status = AXMessaging.performAction(button, kAXPressAction as CFString)
            guard status == .success else {
                throw DriverError("native page action returned AX error \(status.rawValue); effect unknown, not retried",
                                  code: .executionUnknown, effect: .possible)
            }
            onAccepted()
        }
    }
}
