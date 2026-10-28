import AppKit

/// Opaque selections survive focus/title changes, but never a closed root or process restart.
@MainActor final class AppSelections {
    struct Entry { let id: String; let owner: NSRunningApplication; let root: String? }
    private var entries = [String: Entry]()
    private let namespace = UUID().uuidString

    func entry(_ id: String) throws -> Entry {
        guard let entry = entries[id] else {
            throw DriverError("App id is unknown or expired; window references are not App ids", code: .staleTarget,
                next: "pass an exact id from computer.listApps() to computer.app(id); for a ref shown in Windows, use the observed app.window(ref)")
        }
        try BackgroundTarget(entry.owner).validate()
        return entry
    }

    func get(owner: NSRunningApplication, root: String?) -> Entry {
        if let existing = entries.values.first(where: { $0.owner.isEqual(owner) && $0.root == root }) { return existing }
        let id = "app:\(namespace):\(entries.count)"
        let entry = Entry(id: id, owner: owner, root: root)
        entries[id] = entry
        return entry
    }
}
