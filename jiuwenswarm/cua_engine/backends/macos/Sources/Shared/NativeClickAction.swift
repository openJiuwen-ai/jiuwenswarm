/// Choose one native action matching a normal click. AXShowMenu on an arbitrary
/// object can mean its context menu; only dropdown controls use it as a click.
enum NativeClickAction {
    static func resolve(role: String, actions: [String]) -> String? {
        if actions.contains("AXPress") { return "AXPress" }
        if ["AXPopUpButton", "AXMenuButton", "AXComboBox"].contains(role),
           actions.contains("AXShowMenu") { return "AXShowMenu" }
        return nil
    }
}

/// Opening a control's menu is distinct from activating its primary action.
/// The same declaration can also open an ordinary object's context menu.
enum NativeMenuAction {
    static func resolve(actions: [String]?, applicationMenu: Bool, allowsMenuPopup: Bool,
                        role: String? = nil, hasSubmenu: Bool = false) throws -> String {
        guard !applicationMenu || allowsMenuPopup else {
            throw DriverError("opening an application menu would cover the user's desktop", code: .modeUnsupported,
                next: "read({ menuPath: [observed menu titles] }) and click an observed leaf command")
        }
        guard let actions else {
            throw DriverError("native menu action unavailable: the target's actions could not be read; no menu action sent",
                code: .inputUnavailable)
        }
        if actions.contains("AXShowMenu") { return "AXShowMenu" }
        // A native menu branch activates its declared submenu. This does not
        // generalize to buttons (including split buttons) or leaf commands:
        // their Press may perform an unrelated primary action.
        if ["AXMenuBarItem", "AXMenuItem"].contains(role ?? ""), hasSubmenu,
           actions.contains("AXPress") { return "AXPress" }
        throw DriverError("opening this element's menu is unsupported: the target declares neither AXShowMenu nor a pressable submenu; no menu action sent",
            code: .inputUnavailable, next: "read the target and use an observed menu control or a supported visual input route")
    }
}
