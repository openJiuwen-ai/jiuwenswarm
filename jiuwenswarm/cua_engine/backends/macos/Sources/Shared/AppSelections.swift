import AppKit

/// Opaque selections survive focus/title changes, but never a closed root or process restart.
@MainActor final class AppSelections {
    struct Entry { let id: String; let owner: NSRunningApplication; let root: String? }
    private var entries = [String: Entry]()
    private let namespace = UUID().uuidString
    static func isSelector(_ value: String) -> Bool { value.hasPrefix("app:") }

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

    func candidate(owner: NSRunningApplication, window: AccessibilityWindowInfo, document: String? = nil) -> [String: Any] {
        let entry = get(owner: owner, root: window.ref)
        return ["id": entry.id, "displayName": AppSelection.candidateLabel(application: owner.localizedName ?? "Application",
            title: window.title, document: document), "isRunning": true]
    }

    func select(owner: NSRunningApplication, windows: [AccessibilityWindowInfo], selected: Entry?, explicit: String?,
                documentForWindow: ((String) -> String?)? = nil) throws -> Entry {
        if let explicit {
            guard windows.contains(where: { $0.ref == explicit }) else { throw closed() }
            return get(owner: owner, root: explicit)
        }
        if let selected, let root = selected.root {
            guard windows.contains(where: { $0.ref == root }) else { throw closed() }
            return selected
        }
        let roots = Set(AppSelection.roots(windows.map { .init(ref: $0.ref, parent: $0.parent, modal: $0.modal == true) }))
        let candidates = windows.filter { roots.contains($0.ref) && $0.kind != "menu" }
        if candidates.count > 1 {
            // This ambiguous selection includes each candidate's document.
            // Explicit selections and single-window reads need no extra AX work.
            let choices = candidates.map { candidate(owner: owner, window: $0, document: documentForWindow?($0.ref)) }
            let json = String(data: try JSONSerialization.data(withJSONObject: choices, options: [.sortedKeys]), encoding: .utf8)!
            throw DriverError("multiple working windows; choose one App: \(json)", code: .ambiguousTarget,
                next: "use computer.app(candidate.id).read(); no window was selected or activated")
        }
        return get(owner: owner, root: candidates.first?.ref)
    }

    private func closed() -> DriverError {
        DriverError("this App's selected window has closed; it will not switch to another document", code: .staleTarget,
            next: "use computer.listApps() and computer.app(id) to select another working window")
    }
}
