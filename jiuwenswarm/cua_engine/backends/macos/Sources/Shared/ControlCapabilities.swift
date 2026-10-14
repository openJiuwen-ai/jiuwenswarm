import Foundation

/// Public intentions, not native actions. Unknown never means permission to
/// redirect input; execution resolves the current recipient inside its scope.
struct ControlCapabilities: Equatable {
    enum Availability: String { case available, unavailable, unknown }
    var click: Availability
    var typeText: Availability
    var replaceText: Availability
    var focus: Availability

    var protocolValue: [String: String] {
        ["click": click.rawValue, "typeText": typeText.rawValue,
         "replaceText": replaceText.rawValue, "focus": focus.rawValue]
    }
    static let unavailable = Self(click: .unavailable, typeText: .unavailable, replaceText: .unavailable, focus: .unavailable)
}

/// Bounded, read-only evidence used by observation and execution. A failed
/// query retains its AX error; it must not become a false capability.
struct ControlFacts {
    var role: String
    var enabled: Bool?
    var actions: [String]?
    var selectable = false
    var focusWritable: Bool? = nil
    var editable: Bool? = nil
    var valueWritable: Bool? = nil
    var textWritable: Bool? = nil
    var selectionWritable: Bool? = nil
    var hasTextValue = false
    var editorKnown = false
    var checkboxValue: Int? = nil
    var pointer = false
    var menuBlocked = false
    // A recorded application-menu command has an authorized preparation and
    // validation route. Its pre-preparation AXEnabled is not the final answer.
    var menuValidationAvailable = false
    var inputBlocked = false
    var failures: [String: Int32] = [:]
    var collectionIncomplete = false

    enum ClickRoute: Equatable { case native, checkbox, input, pointer, unresolved }
    var isTextRole: Bool { ["AXTextField", "AXTextArea", "AXComboBox", "AXWebArea"].contains(role) }
    var textForbidden: Bool { role == "AXStaticText" || editable == false }

    /// Keep the priority identical to the execution dispatcher. Role-only
    /// candidates can still be resolved at action time without being advertised.
    var clickRoute: ClickRoute {
        // Clicking a text entry positions input. An incidental AXPress on a
        // text field need not do that (PDFKit exposes such an action). Choose
        // its input route before dispatch, rather than replaying a native Press.
        // Selectable cells and combo-box dropdowns keep their own click meaning.
        if !selectable, ["AXTextField", "AXTextArea"].contains(role) { return .input }
        if selectable || NativeClickAction.resolve(role: role, actions: actions ?? []) != nil { return .native }
        if role == "AXCheckBox", checkboxValue != nil { return .checkbox }
        if isTextRole && (role != "AXWebArea" || editorKnown) { return .input }
        if pointer { return .pointer }
        return .unresolved
    }

    var capabilities: ControlCapabilities {
        if inputBlocked { return .unavailable }
        if enabled == false {
            if menuValidationAvailable, role == "AXMenuItem", !menuBlocked, !collectionIncomplete,
               NativeClickAction.resolve(role: role, actions: actions ?? []) != nil {
                return ControlCapabilities(click: .unknown, typeText: .unavailable, replaceText: .unavailable, focus: .unavailable)
            }
            return .unavailable
        }
        if collectionIncomplete {
            return ControlCapabilities(click: menuBlocked ? .unavailable : .unknown,
                typeText: .unknown, replaceText: .unknown, focus: .unknown)
        }
        let focus: ControlCapabilities.Availability = focusWritable == true ? .available : .unknown
        let click: ControlCapabilities.Availability
        if menuBlocked { click = .unavailable }
        else {
            switch clickRoute {
            case .native, .pointer: click = .available
            case .checkbox, .input: click = focus
            case .unresolved: click = .unknown
            }
        }
        // Some readonly providers advertise writable selected text but reject
        // value editing. Conflicting declarations are not proof of an input
        // route. Keep unknown (and action-time preparation), not a blanket ban:
        // a real keyboard editor need not implement either native text setter.
        let input: ControlCapabilities.Availability
        if textForbidden || (selectable && !editorKnown) || (role == "AXWebArea" && !editorKnown) { input = .unavailable }
        else if editorKnown && focus == .available
            && (editable == true || valueWritable == true
                || (textWritable == true && valueWritable != false)) { input = .available }
        else if !isTextRole && !editorKnown && !["AXGroup", "AXUnknown", "AXCell", "AXRow", "AXIncrementor"].contains(role) {
            input = .unavailable
        } else { input = .unknown }
        // Replacement needs the complete value. A native-value fallback may
        // resolve unknown focus at action time; it does not promise focus here.
        let replace: ControlCapabilities.Availability = input == .available
            && (!hasTextValue || (role == "AXWebArea" && selectionWritable != true)) ? .unknown : input
        return ControlCapabilities(click: click, typeText: input, replaceText: replace, focus: focus)
    }
}
