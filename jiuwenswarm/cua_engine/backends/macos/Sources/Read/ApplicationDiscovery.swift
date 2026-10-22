import AppKit

/// Installation discovery and running-instance selection share one registry.
/// AX trees, window capture and foreground selection play no part in identity.
@MainActor final class ApplicationDiscovery {
    private let instances = ApplicationInstances<NSRunningApplication>()
    private var applicationRoots: [URL] {
        ["/Applications", "/System/Applications", "/System/Library/CoreServices", NSHomeDirectory() + "/Applications"].map { URL(fileURLWithPath: $0) }
    }

    private var running: [NSRunningApplication] {
        instances.retire { $0.isTerminated || NativeProcessIdentity.isGone($0.processIdentifier) }
        return Array(Set(NSWorkspace.shared.runningApplications.filter {
            !$0.isTerminated && $0.processIdentifier > 0 && !NativeProcessIdentity.isGone($0.processIdentifier)
                && $0.processIdentifier != ProcessInfo.processInfo.processIdentifier
        }))
    }

    func selector(for app: NSRunningApplication) -> String? {
        guard !app.isTerminated, app.processIdentifier > 0, !NativeProcessIdentity.isGone(app.processIdentifier) else { return nil }
        return instances.id(for: app)
    }

    private func catalogue(continuing: () -> Bool = { true }) -> [ApplicationCatalog.Entry] {
        let active = running.compactMap { app -> ApplicationCatalog.Running? in
            guard let id = selector(for: app) else { return nil }
            return ApplicationCatalog.Running(id: id, url: app.bundleURL, identifier: app.bundleIdentifier,
                name: app.localizedName, regularUI: app.activationPolicy == .regular)
        }
        return ApplicationCatalog.list(roots: applicationRoots, running: active, continuing: continuing)
    }

    func nativeApplications(continuing: () -> Bool) -> [[String: Any]] { catalogue(continuing: continuing).map(\.nativeJSON) }

    func nativeEntry(for app: NSRunningApplication) -> [String: Any]? {
        guard let id = selector(for: app) else { return nil }
        let name = [app.localizedName, app.bundleIdentifier, app.bundleURL?.deletingPathExtension().lastPathComponent]
            .compactMap { $0 }.first { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? "Unnamed application"
        return ApplicationCatalog.Entry(id: id,
            displayName: name, isRunning: true,
            path: app.bundleURL?.standardizedFileURL.resolvingSymlinksInPath().path,
            identifier: app.bundleIdentifier).nativeJSON
    }

    private func sameName(_ a: String?, _ b: String) -> Bool {
        a?.localizedCaseInsensitiveCompare(b) == .orderedSame
    }

    private func matches(_ app: NSRunningApplication, _ selector: String) -> Bool {
        if selector.hasPrefix("/") {
            return app.bundleURL?.standardizedFileURL.resolvingSymlinksInPath() == URL(fileURLWithPath: selector).standardizedFileURL.resolvingSymlinksInPath()
        }
        return app.bundleIdentifier == selector || sameName(app.localizedName, selector) || sameName(app.bundleURL?.deletingPathExtension().lastPathComponent, selector)
    }

    private func installed(_ selector: String) throws -> URL {
        if selector.hasPrefix("/") {
            let url = URL(fileURLWithPath: selector).standardizedFileURL.resolvingSymlinksInPath()
            guard url.pathExtension.lowercased() == "app", Bundle(url: url)?.executableURL != nil else {
                throw DriverError("application path does not identify an installed .app: \(selector)", code: .invalidRequest)
            }
            return url
        }
        var candidates = Set<URL>()
        for app in NSWorkspace.shared.runningApplications where matches(app, selector) {
            if let url = app.bundleURL { candidates.insert(url.standardizedFileURL.resolvingSymlinksInPath()) }
        }
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: selector) {
            candidates.insert(url.standardizedFileURL.resolvingSymlinksInPath())
        }
        for url in ApplicationCatalog.installedURLs(roots: applicationRoots) {
            guard let bundle = Bundle(url: url) else { continue }
            if bundle.bundleIdentifier == selector || sameName(url.deletingPathExtension().lastPathComponent, selector)
                || sameName(bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String, selector)
                || sameName(bundle.object(forInfoDictionaryKey: "CFBundleName") as? String, selector) {
                candidates.insert(url.standardizedFileURL.resolvingSymlinksInPath())
            }
        }
        guard candidates.count == 1, let url = candidates.first else {
            throw DriverError(candidates.isEmpty ? "application not found: \(selector)" : "application selector is ambiguous: \(selector); candidates: \(candidates.map(\.path).sorted().joined(separator: "; ")); use an absolute .app path", code: candidates.isEmpty ? .invalidRequest : .ambiguousTarget)
        }
        return url
    }

    private func choose(_ candidates: [NSRunningApplication], selector: String) throws -> NSRunningApplication? {
        guard candidates.count <= 1 else {
            let entries = candidates.compactMap { app -> [String: String]? in
                guard let id = self.selector(for: app) else { return nil }
                return ["id": id, "displayName": app.localizedName ?? app.bundleIdentifier ?? "Unnamed application"]
            }.sorted { $0["id"]! < $1["id"]! }
            let json = String(data: try JSONSerialization.data(withJSONObject: entries, options: [.sortedKeys]), encoding: .utf8)!
            throw DriverError("multiple running instances match \(selector); candidates: \(json)", code: .ambiguousTarget,
                next: "use computer.app(candidate.id).read() to inspect the intended instance")
        }
        return candidates.first
    }

    enum Selection {
        case running(NSRunningApplication)
        case installed(URL)
    }

    /// Resolve identity without launching, focusing or reading any UI.
    func locate(_ selector: String) throws -> Selection {
        if ApplicationInstances<NSRunningApplication>.isInstanceSelector(selector) {
            guard let instance = instances.instance(selector) else {
                throw DriverError("application instance selector expired or belongs to another session", code: .staleTarget,
                    next: "use computer.listApps() to choose a current instance")
            }
            // Never fall through to installed lookup or launch for an instance ID.
            try BackgroundTarget(instance).validate()
            return .running(instance)
        }
        let active = running
        if let existing = try choose(active.filter { matches($0, selector) }, selector: selector) { return .running(existing) }
        let url = try installed(selector)
        if let existing = try choose(active.filter { $0.bundleURL?.standardizedFileURL.resolvingSymlinksInPath() == url }, selector: selector) {
            return .running(existing)
        }
        return .installed(url)
    }

    /// LaunchServices completion, AppKit registration and application startup
    /// finish independently. Wait on readiness without relaunching or adopting
    /// a different process. Pending registration never authorizes AX or input.
    static func waitUntilReady(_ target: BackgroundTarget, isFinishedLaunching: () -> Bool,
                               applicationForPID: (pid_t) -> NSRunningApplication? = { NSRunningApplication(processIdentifier: $0) },
                               attempts: Int = 50,
                               pause: () async throws -> Void = { try await Task.sleep(for: .milliseconds(100)) }) async throws {
        for attempt in 0..<attempts {
            try RequestDiagnostics.checkCancellation()
            if try target.launchRegistrationReady(applicationForPID: applicationForPID), isFinishedLaunching() { return }
            if attempt + 1 < attempts { try await pause() }
        }
        throw DriverError("application startup is not ready; no UI input sent", code: .executionFailed,
            next: "allow the application to finish starting, then read the same application again")
    }
}
