@main struct SharedDesktopPolicyTests {
    static func main() throws {
        let subroles: [String?] = [nil, "AXApplicationDockItem", "AXDocumentDockItem", "AXFolderDockItem",
            "AXMinimizedWindowDockItem", "AXURLDockItem", "AXDockExtraDockItem", "AXTrashDockItem", "AXSeparatorDockItem", "futureDockSubrole"]
        for subrole in subroles {
            precondition(!SharedDesktopPolicy.allowsElementInput(role: "AXDockItem", subrole: subrole, exclusive: false))
            try SharedDesktopPolicy.validateElementInput(role: "AXDockItem", subrole: subrole, exclusive: true)
            var dispatched = false
            do {
                try SharedDesktopPolicy.validateElementInput(role: "AXDockItem", subrole: subrole, exclusive: false)
                dispatched = true
                preconditionFailure("shared system launcher input was accepted")
            } catch let error as DriverError {
                precondition(error.code == .modeUnsupported && error.effect == FailureEffect.none)
                precondition(error.next.contains("listApps"))
            }
            precondition(!dispatched)
        }
        // Recognize known launcher subroles even if a provider uses another
        // generic role. Ordinary application controls remain unaffected.
        precondition(!SharedDesktopPolicy.allowsElementInput(role: "AXButton", subrole: "AXApplicationDockItem", exclusive: false))
        for role in ["AXButton", "AXCheckBox", "AXRow", "AXMenuItem", "AXTextField", "AXWindow"] {
            try SharedDesktopPolicy.validateElementInput(role: role, subrole: nil, exclusive: false)
            // Successful mandatory identity validation is enough. Missing
            // supplemental single-attribute queries add no new precondition.
            try SharedDesktopPolicy.validateElementInput(role: role, subrole: nil,
                currentRole: nil, currentSubrole: nil, exclusive: false)
        }
        for (role, subrole, currentRole, currentSubrole) in [
            ("AXDockItem", "AXApplicationDockItem", nil, nil),
            ("AXDockItem", "AXApplicationDockItem", "AXButton", nil),
            ("AXButton", nil, "AXDockItem", nil),
            ("AXButton", nil, nil, "AXApplicationDockItem")
        ] as [(String, String?, String?, String?)] {
            do {
                try SharedDesktopPolicy.validateElementInput(role: role, subrole: subrole,
                    currentRole: currentRole, currentSubrole: currentSubrole, exclusive: false)
                preconditionFailure("known or newly observed launcher was accepted")
            } catch let error as DriverError {
                precondition(error.code == .modeUnsupported && error.effect == FailureEffect.none)
            }
            try SharedDesktopPolicy.validateElementInput(role: role, subrole: subrole,
                currentRole: currentRole, currentSubrole: currentSubrole, exclusive: true)
        }
        print("PASS shared system-launcher input refused before dispatch; ordinary controls and exclusive preserved")
    }
}
