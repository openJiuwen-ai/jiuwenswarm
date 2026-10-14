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
        let mapping: ScreenCapture.WindowMapping
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
    static func listIfAvailable(_ app: NSRunningApplication) -> [Window]? {
        ownership.prune()
        guard let catalog = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
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
                mapping: ScreenCapture.WindowMapping(id: id, pid: pid, frame: frame, title: title,
                    width: Int(frame.width.rounded(.up)), height: Int(frame.height.rounded(.up))),
                onScreen: onScreen, level: (value[kCGWindowLayer as String] as? NSNumber)?.intValue)
        }
        return NativeMenuSurfaces.classify(windows)
    }

    /// Resolve same-title choices using each window's own AXDocument. This is
    /// optional discovery metadata, never an input binding or a reason to omit
    /// a native-only window. Bound the work across the entire listApps request.
    static func documentHints(_ windows: [Window], until deadline: TimeInterval) throws -> [CGWindowID: String] {
        guard AXIsProcessTrusted(), ProcessInfo.processInfo.systemUptime < deadline else { return [:] }
        let duplicates = Dictionary(grouping: windows, by: { $0.mapping.title ?? "" })
            .values.filter { $0.count > 1 }.flatMap { $0 }
        var result = [CGWindowID: String]()
        for group in Dictionary(grouping: duplicates, by: { $0.process.pid }).values {
            try RequestDiagnostics.checkCancellation()
            guard ProcessInfo.processInfo.systemUptime < deadline else { break }
            guard let process = group.first?.process, NativeProcessIdentity.read(process.pid) == process else { continue }
            let wanted = Set(group.map { $0.mapping.id })
            let root = AXUIElementCreateApplication(process.pid)
            AXUIElementSetMessagingTimeout(root, 0.05)
            guard let native = AXAccess.read(root, kAXWindowsAttribute).value as? [AXUIElement] else { continue }
            for window in native {
                try RequestDiagnostics.checkCancellation()
                guard ProcessInfo.processInfo.systemUptime < deadline else { break }
                AXUIElementSetMessagingTimeout(window, 0.05)
                var pid: pid_t = 0
                guard AXUIElementGetPid(window, &pid) == .success, pid == process.pid,
                      let id = BackgroundEvents.nativeWindowID(window), wanted.contains(id),
                      let document = ScreenCapture.documentIdentity(window),
                      BackgroundEvents.nativeWindowID(window) == id,
                      NativeProcessIdentity.read(pid) == process,
                      let info = WindowServerInfo.window(id),
                      (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid else { continue }
                result[id] = document
            }
        }
        return result
    }

    /// AX callbacks, launch and screenshot capture can reveal/create windows
    /// after the initial enumeration. Do not publish a stale or empty catalog.
    /// Enumeration order/titles and moving sibling frames do not invalidate the
    /// selected window. Individual captures validate their own geometry.
    static func validateCatalog(_ expected: [Window], app: NSRunningApplication) throws {
        var current = [CGWindowID: Window]()
        for window in list(app) {
            guard current.updateValue(window, forKey: window.mapping.id) == nil else {
                throw ObservationChanged("application window catalog is inconsistent; read the app again")
            }
        }
        guard current.count == expected.count, expected.allSatisfy({ old in
            guard let window = current[old.mapping.id] else { return false }
            return window.process == old.process
        }) else { throw ObservationChanged("application windows changed while reading; read the app again") }
    }
}
