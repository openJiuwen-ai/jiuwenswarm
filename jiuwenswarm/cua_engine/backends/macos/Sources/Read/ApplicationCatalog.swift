import Foundation

/// Reads bundle metadata only. No launch, accessibility inspection, or persistent cache.
enum ApplicationCatalog {
    struct Running {
        let id: String
        let url: URL?
        let identifier: String?
        let name: String?
        let regularUI: Bool?

        init(id: String, url: URL?, identifier: String?, name: String?, regularUI: Bool? = nil) {
            self.id = id; self.url = url; self.identifier = identifier; self.name = name; self.regularUI = regularUI
        }
    }

    struct Entry {
        let id: String
        let displayName: String
        let isRunning: Bool
        let path: String?
        let identifier: String?

        init(id: String, displayName: String, isRunning: Bool, path: String? = nil, identifier: String? = nil) {
            self.id = id; self.displayName = displayName; self.isRunning = isRunning
            self.path = path; self.identifier = identifier
        }

        var json: [String: Any] { ["id": id, "displayName": displayName, "isRunning": isRunning] }
        var nativeJSON: [String: Any] {
            json.merging(["path": path as Any? ?? NSNull(), "identifier": identifier as Any? ?? NSNull()]) { _, new in new }
        }
    }

    private struct Candidate {
        let url: URL
        let identifier: String?
        let name: String
        let regularUI: Bool?
    }

    /// A bundle's declared activation policy is useful before launch. Malformed
    /// or incomplete declarations remain unknown instead of excluding the app.
    private static func declaredRegularUI(_ bundle: Bundle?) -> Bool? {
        guard let bundle else { return nil }
        func flag(_ name: String) -> Bool? {
            guard let value = bundle.object(forInfoDictionaryKey: name) else { return false }
            if let number = value as? NSNumber {
                if number.doubleValue == 0 { return false }
                if number.doubleValue == 1 { return true }
            }
            if let text = value as? String {
                switch text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
                case "true", "yes", "1": return true
                case "false", "no", "0": return false
                default: break
                }
            }
            return nil
        }
        let agent = flag("LSUIElement"), background = flag("LSBackgroundOnly")
        if agent == true || background == true { return false }
        guard agent == false, background == false,
              bundle.object(forInfoDictionaryKey: "CFBundlePackageType") as? String == "APPL" else { return nil }
        return true
    }

    /// Finder-hidden application entry points (including symlinks) remain
    /// launchable. Skip private dot paths and hidden non-application containers,
    /// rather than FileManager's blanket skipsHiddenFiles filter.
    static func installedURLs(roots: [URL], continuing: () -> Bool = { true }) -> [URL] {
        var result = Set<URL>()
        for root in roots {
            guard continuing() else { break }
            guard let iterator = FileManager.default.enumerator(at: root,
                includingPropertiesForKeys: [.isHiddenKey, .isDirectoryKey, .isSymbolicLinkKey, .isPackageKey], options: [.skipsPackageDescendants]) else { continue }
            for case let url as URL in iterator {
                guard continuing() else { break }
                let attributes = try? url.resourceValues(forKeys: [.isHiddenKey, .isDirectoryKey, .isSymbolicLinkKey, .isPackageKey])
                if url.lastPathComponent.hasPrefix(".") {
                    if attributes?.isDirectory == true, attributes?.isSymbolicLink != true, attributes?.isPackage != true { iterator.skipDescendants() }
                    continue
                }
                if url.pathExtension.lowercased() == "app" {
                    let canonical = url.standardizedFileURL.resolvingSymlinksInPath()
                    if canonical.pathExtension.lowercased() == "app", Bundle(url: canonical)?.executableURL != nil {
                        result.insert(url.standardizedFileURL)
                    }
                } else if attributes?.isHidden == true, attributes?.isDirectory == true, attributes?.isSymbolicLink != true, attributes?.isPackage != true {
                    iterator.skipDescendants()
                }
            }
        }
        return result.sorted { $0.path < $1.path }
    }

    static func list(roots: [URL], running: [Running], continuing: () -> Bool = { true }) -> [Entry] {
        var candidates = [String: Candidate]()
        func nonempty(_ values: String?...) -> String? {
            values.compactMap { $0 }.first { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
        func add(_ url: URL) {
            let canonical = url.standardizedFileURL.resolvingSymlinksInPath()
            guard canonical.pathExtension.lowercased() == "app" else { return }
            let bundle = Bundle(url: canonical)
            guard bundle?.executableURL != nil, candidates[canonical.path] == nil else { return }
            let identifier = nonempty(bundle?.bundleIdentifier)
            let name = nonempty(bundle?.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String,
                bundle?.object(forInfoDictionaryKey: "CFBundleName") as? String, canonical.deletingPathExtension().lastPathComponent)!
            candidates[canonical.path] = Candidate(url: canonical, identifier: identifier, name: name,
                regularUI: declaredRegularUI(bundle))
        }
        for url in installedURLs(roots: roots, continuing: continuing) {
            guard continuing() else { break }
            add(url)
        }
        // Deduplicate observations of one native lifetime, not installations.
        // NSWorkspace supplies application instances, not arbitrary helper PIDs.
        var activeByID = [String: Running]()
        for app in running {
            if activeByID[app.id] == nil { activeByID[app.id] = app }
        }
        let activePaths = Set(running.compactMap { $0.url?.standardizedFileURL.resolvingSymlinksInPath().path })
        // Include active bundles outside search roots when assessing duplicate IDs.
        var pathsByIdentifier = [String: Set<String>]()
        for app in candidates.values { if let id = app.identifier { pathsByIdentifier[id, default: []].insert(app.url.path) } }
        for app in running { if let id = app.identifier, let path = app.url?.standardizedFileURL.resolvingSymlinksInPath().path { pathsByIdentifier[id, default: []].insert(path) } }
        let installed = candidates.values.filter { !activePaths.contains($0.url.path) }.map { app in
            // Duplicate bundle IDs must remain selectable without ambiguous resolution.
            let id = app.identifier.flatMap { pathsByIdentifier[$0]?.count == 1 ? $0 : nil } ?? app.url.path
            return (entry: Entry(id: id, displayName: app.name, isRunning: false, path: app.url.path, identifier: app.identifier), regularUI: app.regularUI == true)
        }
        let active = activeByID.values.map { app in
            let bundle = app.url.flatMap { Bundle(url: $0) }
            let name = nonempty(app.name, bundle?.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String,
                bundle?.object(forInfoDictionaryKey: "CFBundleName") as? String, app.url?.deletingPathExtension().lastPathComponent, app.identifier, "Unnamed application")!
            return (entry: Entry(id: app.id, displayName: name, isRunning: true,
                path: app.url?.standardizedFileURL.resolvingSymlinksInPath().path, identifier: app.identifier), regularUI: app.regularUI == true)
        }
        return (installed + active).sorted {
            if $0.entry.isRunning != $1.entry.isRunning { return $0.entry.isRunning }
            if $0.regularUI != $1.regularUI { return $0.regularUI }
            let order = $0.entry.displayName.localizedStandardCompare($1.entry.displayName)
            return order == .orderedSame ? $0.entry.id < $1.entry.id : order == .orderedAscending
        }.map { $0.entry }
    }
}
