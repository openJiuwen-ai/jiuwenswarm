import Foundation

/// An App is a persistent working-window selection, not a process-wide cursor.
/// Unknown ownership remains a separate choice; only native parent edges group windows.
enum AppSelection {
    struct Window { let ref: String; let parent: String?; let modal: Bool }

    static func current(root: String, windows: [Window], focused: String?) -> String {
        let known = Set(windows.map(\.ref))
        var current = root, visited = Set<String>()
        while visited.insert(current).inserted {
            let children = windows.filter { $0.modal && $0.parent == current && !visited.contains($0.ref) }
            if let focused, children.contains(where: { $0.ref == focused }) { current = focused }
            else if children.count == 1 { current = children[0].ref }
            else { break }
        }
        if current != root { return current }
        // A reported app-modal dialog with no owner blocks all documents. Its
        // presence is not evidence that it belongs to a particular document.
        let global = windows.filter { $0.ref != root && $0.modal && ($0.parent == nil || !known.contains($0.parent!)) }
        if global.count == 1 { return self.current(root: global[0].ref, windows: windows.filter { $0.ref != root }, focused: focused) }
        return root
    }
}
