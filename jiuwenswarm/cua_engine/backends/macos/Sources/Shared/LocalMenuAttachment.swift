/// A local popup may be a child of a document AX tree while AXWindow names
/// its separate popup surface. Only a live, exact forward path authorizes an
/// object action; this never changes a pointer/keyboard destination.
enum LocalMenuAttachment {
    static func confirms<Node>(leaf: Node, ancestors: [Node], window: Node,
                               same: (Node, Node) -> Bool, role: (Node) -> String?,
                               children: (Node) throws -> [Node]) rethrows -> Bool {
        guard ["AXMenuItem", "AXMenu"].contains(role(leaf) ?? ""),
              let start = ancestors.lastIndex(where: { same($0, window) }) else { return false }
        let path = Array(ancestors[start...]) + [leaf]
        guard path.count <= 41,
              path.dropFirst().contains(where: { role($0) == "AXMenu" }),
              !path.dropFirst().contains(where: { ["AXMenuBar", "AXMenuBarItem", "AXWindow", "AXSheet"].contains(role($0) ?? "") }) else { return false }
        return try ObservedPath.matches(path, root: window, same: same, children: children)
    }
}
