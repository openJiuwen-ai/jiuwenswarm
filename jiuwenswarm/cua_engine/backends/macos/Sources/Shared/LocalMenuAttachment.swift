/// A local popup may be a child of a document AX tree while AXWindow names
/// its separate popup surface. Only a live, exact forward path authorizes an
/// action. A choice list published by a menu/combobox has the same logical
/// attachment even when its pixels live in a separate non-modal popup.
enum LocalMenuAttachment {
    static func confirms<Node>(
        leaf: Node, ancestors: [Node], window: Node, same: (Node, Node) -> Bool, role: (Node) -> String?,
        children: (Node) throws -> [Node]
    ) rethrows -> Bool {
        guard let start = ancestors.lastIndex(where: { same($0, window) }) else { return false }
        let path = Array(ancestors[start...]) + [leaf]
        let roles = path.map { role($0) ?? "" }
        let menu = ["AXMenuItem", "AXMenu"].contains(roles.last ?? "") && roles.contains("AXMenu")
        let choices =
            roles.firstIndex(where: { ["AXMenuButton", "AXPopUpButton", "AXComboBox"].contains($0) }).map {
                roles.dropFirst($0 + 1).contains("AXList")
            } ?? false
        guard path.count <= 41, menu || choices,
            !path.dropFirst().contains(where: {
                ["AXMenuBar", "AXMenuBarItem", "AXWindow", "AXSheet"].contains(role($0) ?? "")
            })
        else { return false }
        return try ObservedPath.matches(path, root: window, same: same, children: children)
    }
}
