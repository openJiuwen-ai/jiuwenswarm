import AppKit
import ApplicationServices
import Darwin

/// WindowServer discovery is independent of an application's AX implementation.
/// Include the main process and confirmed in-bundle descendants. A title or
/// process name alone never establishes ownership of another process's window.
@MainActor enum NativeWindows {
    private static let ownership = NativeProcessOwnership()
    struct Window {
        let application: NSRunningApplication
        let process: NativeProcessIdentity
        let mapping: WindowGeometry.WindowMapping
        let onScreen: Bool
        var level: Int? = nil
        var menu: AXUIElement? = nil
        var menuParent: CGWindowID? = nil
    }

    static func belongs(_ process: NativeProcessIdentity, to app: NSRunningApplication) -> Bool {
        guard !app.isTerminated, let bundle = app.bundleURL,
              let owner = NativeProcessIdentity.read(app.processIdentifier) else { return false }
        return ownership.belongs(process, to: owner, bundle: bundle)
    }

    static func list(_ app: NSRunningApplication) -> [Window] {
        listIfAvailable(app) ?? []
    }

    /// Absence proofs must not turn a WindowServer query failure into an empty desktop.
    static func listIfAvailable(_ app: NSRunningApplication, catalog supplied: [[String: Any]]? = nil) -> [Window]? {
        ownership.prune()
        guard let catalog = supplied ?? (CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]) else { return nil }
        var owners = [pid_t: NativeProcessIdentity](), rejected = Set<pid_t>()
        let windows: [Window] = catalog.compactMap { value in
            guard let pid = (value[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
                  !rejected.contains(pid),
                  let id = (value[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let bounds = value[kCGWindowBounds as String] as? [String: Any],
                  let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary) else { return nil }
            let title = value[kCGWindowName as String] as? String ?? ""
            let onScreen = (value[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue == true
            guard WindowSelection.discoverable(.init(id: id, frame: frame, title: title, visible: onScreen),
                alpha: (value[kCGWindowAlpha as String] as? NSNumber)?.doubleValue) else { return nil }
            let process: NativeProcessIdentity
            if let known = owners[pid] { process = known }
            else {
                guard let candidate = NativeProcessIdentity.read(pid),
                      pid == app.processIdentifier || belongs(candidate, to: app) else {
                    rejected.insert(pid); return nil
                }
                process = candidate; owners[pid] = candidate
            }
            guard let application = NSRunningApplication(processIdentifier: pid), !application.isTerminated else { return nil }
            return Window(application: application, process: process,
                mapping: WindowGeometry.WindowMapping(id: id, pid: pid, frame: frame, title: title,
                    width: Int(frame.width.rounded(.up)), height: Int(frame.height.rounded(.up))),
                onScreen: onScreen, level: (value[kCGWindowLayer as String] as? NSNumber)?.intValue)
        }
        return NativeMenuSurfaces.classify(windows)
    }

}
