import Foundation
import CoreGraphics

/// Geometry disambiguates unnamed dialogs and stale windows retained by capture APIs.
enum WindowSelection {
    /// A sheet's key responder and its document's main responder are different
    /// windows in one native menu scope. Only declared sheet edges join them;
    /// ordinary child windows, focus, titles and geometry never establish this.
    static func sheetDocument<Node>(_ target: Node, roots: [Node], same: (Node, Node) -> Bool,
                                    sheets: (Node) -> [Node]?, withinBudget: () -> Bool) -> Node? {
        var nodes = [Node](), edges = [(Int, Int)](), cursor = 0
        func index(_ node: Node) -> Int {
            if let index = nodes.firstIndex(where: { same($0, node) }) { return index }
            nodes.append(node); return nodes.count - 1
        }
        for root in roots { _ = index(root) }
        while cursor < nodes.count {
            guard nodes.count <= 32, withinBudget(), let children = sheets(nodes[cursor]), children.count <= 32 else { return nil }
            for child in children { edges.append((cursor, index(child))) }
            cursor += 1
        }
        guard let selected = nodes.firstIndex(where: { same($0, target) }) else { return nil }
        var current = selected, seen = Set<Int>()
        while seen.insert(current).inserted {
            let parents = Set(edges.filter { $0.1 == current }.map { $0.0 })
            if parents.isEmpty { return roots.contains(where: { same($0, nodes[current]) }) ? nodes[current] : nil }
            guard parents.count == 1, let parent = parents.first else { return nil }
            current = parent
        }
        return nil // Cycles or conflicting ownership cannot authorize a menu.
    }

    struct Candidate {
        let id: UInt32
        let frame: CGRect
        let title: String
        let visible: Bool
    }
    struct Hint {
        let frame: CGRect?
        let title: String
        var id: UInt32? = nil
    }

    /// Visual discovery needs no title, AX role or normal-window layer. Hidden
    /// WindowServer buffers can outlive a closed NSWindow; a title does not prove
    /// that they are usable windows. AX-reported hidden windows are retained separately.
    static func discoverable(_ candidate: Candidate, alpha: Double?) -> Bool {
        let frame = candidate.frame
        return frame.origin.x.isFinite && frame.origin.y.isFinite
            && frame.width.isFinite && frame.height.isFinite && frame.width > 1 && frame.height > 1
            && alpha != 0 && candidate.visible
    }

    static func sameFrame(_ a: CGRect, _ b: CGRect) -> Bool {
        abs(a.minX - b.minX) < 0.5 && abs(a.minY - b.minY) < 0.5
            && abs(a.width - b.width) < 0.5 && abs(a.height - b.height) < 0.5
    }

    /// Catalogue identity is not a coordinate mapping. An exact native ID
    /// survives movement or temporarily missing AX geometry. Callers first
    /// constrain process identity and still require a mutual one-to-one match.
    static func identifies(_ candidate: Candidate, hint: Hint) -> Bool {
        if let id = hint.id { return id == candidate.id }
        guard let frame = hint.frame, sameFrame(frame, candidate.frame) else { return false }
        return hint.title.isEmpty || candidate.title.isEmpty || hint.title == candidate.title
    }

    /// Explain a rejected mapping without misreporting a known window as an
    /// ambiguous identity. This does not relax selection or input geometry.
    static func geometryConflict(_ candidates: [Candidate], hint: Hint?) -> Candidate? {
        guard let hint, let id = hint.id, let frame = hint.frame else { return nil }
        let matches = candidates.filter { $0.id == id }
        guard matches.count == 1, !sameFrame(matches[0].frame, frame) else { return nil }
        return matches[0]
    }

    /// Some accessibility bridges expose children correctly but omit their parent
    /// links. Find the window by native object identity, never by title or proximity.
    static func containing<Node>(_ ancestry: [Node], in windows: [Node],
                                 same: (Node, Node) -> Bool, isWindow: (Node) -> Bool,
                                 children: (Node) -> [Node],
                                 withinBudget: () -> Bool) -> Node? {
        var pending = windows.map { (window: $0, node: $0, depth: 0) }
        var visited = [Node](), index = 0
        while index < pending.count && visited.count < 500 && withinBudget() {
            let item = pending[index]; index += 1
            if visited.contains(where: { same($0, item.node) }) { continue }
            visited.append(item.node)
            // Sheets and nested native windows own their descendants even when
            // the search began at the containing document window.
            let owner = isWindow(item.node) ? item.node : item.window
            if ancestry.contains(where: { same($0, item.node) }) { return owner }
            if item.depth < 16 {
                pending.append(contentsOf: children(item.node).prefix(64).map {
                    (window: owner, node: $0, depth: item.depth + 1)
                })
            }
        }
        return nil
    }

    /// A hosted native view may expose both a service root and a presenter
    /// root. Only identical, complete, nonempty child identities prove that
    /// they present the same content; titles, geometry and partial overlap do not.
    static func contentHost<Node>(_ remote: Node, candidates: [Node],
                                  children: (Node) -> [Node]?, same: (Node, Node) -> Bool) -> Node? {
        guard let content = children(remote), !content.isEmpty else { return nil }
        let matches = candidates.filter { candidate in
            guard let hosted = children(candidate), hosted.count == content.count else { return false }
            return zip(content, hosted).allSatisfy { same($0.0, $0.1) }
        }
        return matches.count == 1 ? matches[0] : nil
    }

    static func select(_ candidates: [Candidate], hint: Hint?, requireUnique: Bool = false) -> UInt32? {
        var matches = candidates
        if let hint {
            if let id = hint.id { matches = matches.filter { $0.id == id } }
            if let frame = hint.frame {
                matches = matches.filter { sameFrame($0.frame, frame) }
            } else if hint.id == nil && !hint.title.isEmpty {
                matches = matches.filter { $0.title == hint.title }
            }
            if hint.id == nil && !hint.title.isEmpty {
                // A single stale document can have the new document's geometry.
                // Do not accept a conflicting known title just because it is unique.
                matches = matches.filter { $0.title.isEmpty || $0.title == hint.title }
            }
            // A known focused window that is not yet capturable must never fall
            // through to some other document window.
            if matches.isEmpty { return nil }
            if matches.count > 1 && hint.id == nil && !hint.title.isEmpty {
                let titled = matches.filter { $0.title == hint.title }
                if !titled.isEmpty { matches = titled }
            }
        }
        if matches.count > 1 && !requireUnique {
            let visible = matches.filter(\.visible)
            if visible.count == 1 { matches = visible }
        }
        return matches.count == 1 ? matches[0].id : nil
    }
}
