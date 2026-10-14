import Foundation

struct ElementMatch: Equatable {
    let role: String?
    let name: String?
    let identifier: String?

    init(_ raw: Any) throws {
        guard let fields = raw as? [String: Any], !fields.isEmpty,
              Set(fields.keys).isSubset(of: ["role", "name", "identifier"]) else {
            throw DriverError("path segments require role, name and/or identifier", code: .invalidRequest)
        }
        for value in fields.values {
            guard let text = value as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  text.utf16.count <= 256 else { throw DriverError("invalid path segment string", code: .invalidRequest) }
        }
        role = fields["role"] as? String; name = fields["name"] as? String; identifier = fields["identifier"] as? String
        if let role, role.range(of: "^[a-z][a-zA-Z0-9]*$", options: .regularExpression) == nil {
            throw DriverError("use canonical roles such as button or textField", code: .invalidRequest)
        }
    }

    func matches(role: String, name: String, identifier: String) -> Bool {
        (self.role == nil || self.role == role) && (self.name == nil || self.name == name)
            && (self.identifier == nil || self.identifier == identifier)
    }
    var description: String {
        [role.map { "role=\(String(reflecting: $0))" }, name.map { "name=\(String(reflecting: $0))" },
         identifier.map { "identifier=\(String(reflecting: $0))" }].compactMap { $0 }.joined(separator: ", ")
    }
}

/// The first anchor may be anywhere in the selected root. Later segments only
/// cross anonymous layout wrappers, never a named region, record or submenu item.
/// Collection failures propagate: one visible match does not prove uniqueness.
enum ElementPath {
    struct Match<Node> { let path: [Node]; let anchors: [(Node, ElementMatch)] }
    static func roots<Node>(firstRole: String?, window: Node, menuBar: Node?) -> [Node] {
        let bar = menuBar.map { [$0] } ?? []
        switch firstRole {
        case "menuBar", "menuBarItem": return bar
        case "menuItem": return [window] + bar
        default: return [window]
        }
    }
    static func resolve<Node>(roots: [Node], path: [ElementMatch], firstRootOnly: Bool = false,
                              same: (Node, Node) -> Bool,
                              matches: (Node, ElementMatch) throws -> Bool,
                              transparent: (Node) throws -> Bool,
                              children: (Node) throws -> [Node],
                              check: () throws -> Void) throws -> Match<Node> {
        var selected = [Node](), anchors = [(Node, ElementMatch)]()
        for (step, condition) in path.enumerated() {
            var found = [[Node]](), seen = [Node]()
            func visit(_ node: Node, parents: [Node], depth: Int) throws {
                try check()
                guard depth <= 40, seen.count < 2048 else {
                    throw DriverError("path search exceeded its node/depth limit; narrow the first anchor", code: .observationIncomplete)
                }
                if parents.contains(where: { same($0, node) }) {
                    throw DriverError("cyclic native containment while finding a path", code: .observationIncomplete)
                }
                if seen.contains(where: { same($0, node) }) { return }
                seen.append(node)
                let matched = try matches(node, condition)
                if matched {
                    found.append(parents + [node])
                    if found.count > 1 {
                        throw DriverError("ambiguous path at step \(step + 1): \(condition.description); at least two matches; no first-match fallback", code: .ambiguousTarget)
                    }
                }
                let descend = step == 0 ? !firstRootOnly : (try !matched && transparent(node))
                if descend {
                    for child in try children(node) { try visit(child, parents: parents + [node], depth: depth + 1) }
                }
            }
            let candidates = try selected.last.map(children) ?? roots
            for node in candidates { try visit(node, parents: selected, depth: 0) }
            guard let result = found.first, let last = result.last else {
                throw DriverError("no element at path step \(step + 1): \(condition.description)", code: .elementNotFound)
            }
            selected = result; anchors.append((last, condition))
        }
        return Match(path: selected, anchors: anchors)
    }
}
