import AppKit

/// One explicit installation launch. This never chooses a different bundle,
/// activates an existing window, or retries a potentially completed launch.
@MainActor struct NativeLaunchUnit {
    let installation: NativeInstallation
    let discovery: ApplicationDiscovery

    func perform(check: () throws -> Void, onDispatch: () throws -> Void) async throws
        -> (application: [String: Any], launched: Bool) {
        try check()
        try installation.validate()
        let matches = NSWorkspace.shared.runningApplications.filter {
            !$0.isTerminated && $0.bundleURL?.standardizedFileURL.resolvingSymlinksInPath().path == installation.url.path
        }
        guard matches.count <= 1 else {
            throw DriverError("several running instances now match the selected installation", code: .ambiguousTarget,
                              next: "read the application directory and choose a running instance")
        }
        let app: NSRunningApplication
        let launched: Bool
        if let running = matches.first {
            app = running; launched = false
        } else {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = false
            configuration.createsNewApplicationInstance = false
            configuration.allowsRunningApplicationSubstitution = false
            try check()
            try installation.validate()
            try onDispatch()
            app = try await NSWorkspace.shared.openApplication(at: installation.url, configuration: configuration)
            launched = true
        }
        guard app.bundleURL?.standardizedFileURL.resolvingSymlinksInPath().path == installation.url.path else {
            throw DriverError("LaunchServices returned a different application installation", code: .staleTarget)
        }
        try await ApplicationDiscovery.waitUntilReady(BackgroundTarget(app), isFinishedLaunching: { app.isFinishedLaunching })
        try check()
        guard let entry = discovery.nativeEntry(for: app) else {
            throw DriverError("the selected running application ended before its receipt was recorded", code: .staleTarget)
        }
        return (entry, launched)
    }
}
