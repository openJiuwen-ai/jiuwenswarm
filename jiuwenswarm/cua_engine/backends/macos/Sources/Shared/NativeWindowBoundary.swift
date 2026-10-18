import Foundation

/// Final input boundary for an already selected surface. Unlike a read-view
/// selector, this neither picks a modal nor treats the root as the only input
/// surface. Incomplete AX data cannot establish a child relationship.
enum NativeWindowBoundary {
    static func blocked(root: String, target: String, windows: [AppSelection.Window]) throws -> [String] {
        var graph = [String: AppSelection.Window]()
        for window in windows {
            guard graph.updateValue(window, forKey: window.ref) == nil else {
                throw DriverError("native window relationship is ambiguous", code: .inputUnavailable)
            }
        }
        func ancestry(_ ref: String) -> Set<String> {
            var current: String? = ref, seen = Set<String>()
            while let ref = current, seen.insert(ref).inserted { current = graph[ref]?.parent }
            return seen
        }
        let targetPath = ancestry(target)
        guard targetPath.contains(root) else {
            throw DriverError("input surface left its selected working-window range", code: .staleTarget)
        }
        return windows.filter { window in
            guard window.modal, !targetPath.contains(window.ref) else { return false }
            // A known sheet in this document, or a reported app-modal window
            // without an owner, blocks input. Never use a default-view tie
            // breaker to turn several blocking modals into 'none'.
            return ancestry(window.ref).contains(root) || (window.parent.map { graph[$0] == nil } ?? true)
        }.map(\.ref).sorted()
    }
}
