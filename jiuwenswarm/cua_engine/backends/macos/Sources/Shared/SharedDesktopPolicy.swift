/// Native system launcher items act on the shared desktop itself: pressing an
/// app/document/window item can launch or activate a different application.
/// These are AX semantics, independent of process names, labels and URLs.
enum SharedDesktopPolicy {
    private static let dockSubroles: Set<String> = [
        "AXApplicationDockItem", "AXDocumentDockItem", "AXFolderDockItem",
        "AXMinimizedWindowDockItem", "AXURLDockItem", "AXDockExtraDockItem",
        "AXTrashDockItem", "AXSeparatorDockItem"
    ]

    static func allowsElementInput(role: String, subrole: String?, exclusive: Bool) -> Bool {
        exclusive || (role != "AXDockItem" && !dockSubroles.contains(subrole ?? ""))
    }

    static func validateElementInput(role: String, subrole: String?, currentRole: String? = nil,
                                     currentSubrole: String? = nil, exclusive: Bool) throws {
        // Identity has already been validated by the caller. A supplemental
        // attribute query may add evidence, but its absence adds no new refusal.
        // Conversely, missing current attributes cannot erase a known launcher.
        guard allowsElementInput(role: role, subrole: subrole, exclusive: exclusive),
              allowsElementInput(role: currentRole ?? "", subrole: currentSubrole, exclusive: exclusive) else {
            throw DriverError("system Dock item input requires exclusive mode because its action can change the user's foreground or shared desktop; no input sent",
                code: .modeUnsupported, effect: FailureEffect.none,
                next: "use computer.listApps() and read the intended App directly; do not use system launcher items to activate it in shared mode")
        }
    }
}
