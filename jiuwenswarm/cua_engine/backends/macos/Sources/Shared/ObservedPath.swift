/// Validate the currently reported forward child edges, not a title or reverse
/// parent/window attributes. Observation membership does not authorize input.
/// Providers can recycle AX references by position: this does not establish an
/// unobservable underlying target identity. Observable replacements fail.
enum ObservedPath {
    static func matches<Node>(_ path: [Node], root: Node,
                              same: (Node, Node) -> Bool,
                              children: (Node) throws -> [Node]) rethrows -> Bool {
        guard path.count >= 2, let first = path.first, same(first, root) else { return false }
        for index in 1..<path.count {
            let child = path[index]
            guard !path[..<index].contains(where: { same($0, child) }),
                  try children(path[index - 1]).contains(where: { same($0, child) }) else { return false }
        }
        return true
    }
}
