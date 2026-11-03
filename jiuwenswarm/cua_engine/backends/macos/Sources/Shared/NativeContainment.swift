/// Recheck selected containment, without finding a new semantic target.
/// Parent evidence is cheap; incomplete parent providers can be checked using
/// their forward children. A complete conflicting parent path is never hidden.
enum NativeContainment {
    enum Evidence { case confirmed, changed, unavailable }

    static func check<Node>(leaf: Node, within: [Node], same: (Node, Node) -> Bool,
                            parent: (Node) throws -> Node?, children: (Node) throws -> [Node]?,
                            check: () throws -> Void, maxNodes: Int = 2048, maxDepth: Int = 40) throws -> Evidence {
        guard !within.isEmpty else { return .confirmed }
        guard maxNodes > 0, maxDepth > 0,
              !within.contains(where: { same($0, leaf) }) else { return .unavailable }
        for index in within.indices where within[..<index].contains(where: { same($0, within[index]) }) {
            return .unavailable
        }
        var node = leaf, seen = [Node](), expected = within.count - 1
        for _ in 0..<maxDepth {
            try check()
            guard !seen.contains(where: { same($0, node) }) else { break }
            seen.append(node)
            guard let next = try parent(node) else { break }
            if same(next, within[expected]) {
                if expected == 0 { return .confirmed }
                expected -= 1
            } else if within[..<expected].contains(where: { same($0, next) }) {
                // Reached an outer anchor without the required inner anchor.
                return .changed
            }
            node = next
        }
        // Only exact object membership is tested here. No labels, control
        // classification, candidate ranking or alternative receiver selection.
        var remaining = maxNodes
        let chain = within + [leaf]
        for index in 1..<chain.count {
            let target = chain[index]
            var pending = [(chain[index - 1], 0)], visited = [Node](), unknown = false, found = false
            while let (current, depth) = pending.popLast() {
                try check()
                if same(current, target) { found = true; break }
                if visited.contains(where: { same($0, current) }) { continue }
                guard remaining > 0, depth < maxDepth else { unknown = true; continue }
                remaining -= 1; visited.append(current)
                guard let items = try children(current) else { unknown = true; continue }
                if items.contains(where: { same($0, target) }) { found = true; break }
                guard items.count <= remaining + 1 else { unknown = true; continue }
                pending.append(contentsOf: items.map { ($0, depth + 1) })
            }
            if !found { return unknown ? .unavailable : .changed }
        }
        return .confirmed
    }
}
