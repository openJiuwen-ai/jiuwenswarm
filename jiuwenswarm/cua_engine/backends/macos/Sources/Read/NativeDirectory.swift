import AppKit
import ApplicationServices

/// Discovery and retained scope identities. Selection belongs to Python; this
/// collector never picks a document, launches, activates, or reads its contents.
@MainActor final class NativeDirectory {
    struct Surface {
        let owner: NSRunningApplication
        let window: String
        let scope: String
        let parent: String?
        let element: AXUIElement?
        let native: NativeWindows.Window?
    }
    private let state: NativeDesktopState
    private let session: String
    private var surfaces = [String: Surface]()
    private var order = [String]()
    struct ApplicationRoot {
        let owner: NSRunningApplication
        let element: AXUIElement
        let scope: String
    }
    private var applicationRoots = [String: ApplicationRoot]()
    private var rootOrder = [String]()
    private struct InstallationEntry {
        let name: String
        let identity: NativeInstallation?
        let failure: String?
    }
    private var installations = [String: InstallationEntry]()
    private var installationIDs = [NativeInstallation: String]()
    private var installationOrder = [String]()

    init(state: NativeDesktopState, session: String) { self.state = state; self.session = session }
    func surface(_ id: String) -> Surface? { surfaces[id] }
    func applicationRoot(_ id: String) -> ApplicationRoot? { applicationRoots[id] }

    /// Application UI such as the menu bar is not a document window. When a
    /// caller has selected a document, keep that binding and its lifetime; a
    /// read of a global root cannot revive a closed working window.
    func applicationRootScope(instance: String, root: String?, surface: String) throws -> ApplicationRoot? {
        guard let selected = applicationRoots[surface] else { return nil }
        let entry = try state.appSelections.entry(instance)
        guard entry.owner.isEqual(selected.owner), entry.root == root else {
            throw DriverError("application root belongs to another application or working window", code: .staleTarget)
        }
        try BackgroundTarget(selected.owner).validate()
        if let root {
            guard let window = surfaces.values.first(where: { $0.owner.isEqual(selected.owner) && $0.window == root }) else {
                throw DriverError("the original working window is no longer retained", code: .staleTarget)
            }
            try validateWindow(window)
        }
        let app = AXUIElementCreateApplication(selected.owner.processIdentifier)
        AXMessaging.configure(app)
        guard let current = AXAccess.element(app, kAXMenuBarAttribute), CFEqual(current, selected.element) else {
            throw DriverError("the observed application menu root changed or is unavailable", code: .staleTarget)
        }
        return selected
    }

    func collectRoots(_ application: String) throws -> [String: Any] {
        guard ApplicationInstances<NSRunningApplication>.isInstanceSelector(application),
              case .running(let app) = try state.discovery.locate(application) else {
            throw DriverError("root discovery requires an observed running-application id", code: .invalidRequest)
        }
        let began = Int64(Date().timeIntervalSince1970 * 1000)
        var roots = [[String: Any]](), reasons = [String]()
        if AXIsProcessTrusted() {
            _ = state.accessibilityActivation.ensure(app)
            let element = AXUIElementCreateApplication(app.processIdentifier)
            AXMessaging.configure(element)
            let read = AXAccess.read(element, kAXMenuBarAttribute)
            switch read {
            case .value(let raw) where CFGetTypeID(raw) == AXUIElementGetTypeID():
                let menu = unsafeDowncast(raw, to: AXUIElement.self)
                let scope = state.applications.observationScope(app, window: nil)
                let node = try state.applications.registerNativeRoot(menu, app: app, scope: scope)
                let instance = state.appSelections.get(owner: app, root: nil)
                applicationRoots[scope] = ApplicationRoot(owner: app, element: menu, scope: scope)
                rootOrder.removeAll { $0 == scope }; rootOrder.append(scope)
                while rootOrder.count > 1024 { applicationRoots.removeValue(forKey: rootOrder.removeFirst()) }
                roots.append(["kind": "menuBar", "instance": instance.id, "surface": scope, "node": node])
            case .unsupported, .unavailable(.noValue): break
            default: reasons.append("application menu root could not be read")
            }
        } else { reasons.append("accessibility permission unavailable") }
        try BackgroundTarget(app).validate()
        try RequestDiagnostics.checkCancellation()
        var value = result("roots", began: began, reasons: reasons)
        value["application"] = application; value["roots"] = roots
        return value
    }

    func installation(_ raw: Any?) throws -> (wire: [String: Any], identity: NativeInstallation, name: String) {
        let p = try object(raw, keys: ["session", "application"])
        guard try string(p["session"], label: "session") == session else {
            throw DriverError("installation belongs to another session", code: .staleTarget)
        }
        let id = try string(p["application"], label: "application")
        guard let entry = installations[id] else {
            throw DriverError("installation was not observed in this driver session or expired", code: .staleTarget)
        }
        guard let identity = entry.identity else {
            throw DriverError(entry.failure ?? "installation identity is unavailable", code: .observationIncomplete)
        }
        try identity.validate()
        return (p, identity, entry.name)
    }

    private func retainInstallation(_ item: [String: Any]) -> [String: Any] {
        var value = item
        let identity: NativeInstallation?, issue: String?
        do {
            guard let path = item["path"] as? String else { throw DriverError("installation path is unavailable", code: .observationIncomplete) }
            identity = try NativeInstallation(path: path); issue = nil
        } catch { identity = nil; issue = String(describing: error) }
        let id = identity.flatMap { installationIDs[$0] } ?? "installation-\(UUID().uuidString)"
        installations[id] = InstallationEntry(name: item["displayName"] as? String ?? "application", identity: identity, failure: issue)
        if let identity { installationIDs[identity] = id }
        installationOrder.removeAll { $0 == id }; installationOrder.append(id)
        while installationOrder.count > 4096 {
            if let expired = installations.removeValue(forKey: installationOrder.removeFirst()), let identity = expired.identity {
                installationIDs.removeValue(forKey: identity)
            }
        }
        value["id"] = id
        return value
    }

    /// Read-only range validation uses the directory's native window identity,
    /// not a tree node or input recipient. It does not choose a different view.
    func windowScope(instance: String, root: String?, surface id: String) throws -> (root: String, surface: Surface) {
        let entry = try state.appSelections.entry(instance)
        guard let expected = entry.root, root == nil || root == expected,
              let selected = surfaces[id], selected.owner.isEqual(entry.owner),
              let selectedRoot = surfaces.values.first(where: { $0.owner.isEqual(entry.owner) && $0.window == expected }) else {
            throw DriverError("window capture has no retained working-window binding", code: .staleTarget)
        }
        var current = selected, visited = Set<String>()
        while current.window != expected {
            guard visited.insert(current.window).inserted, let parent = current.parent,
                  let ancestor = surfaces.values.first(where: { $0.owner.isEqual(entry.owner) && $0.window == parent }) else {
                throw DriverError("capture surface is outside its observed working-window range", code: .staleTarget)
            }
            current = ancestor
        }
        // A closed root never turns an existing App into another document.
        try validateWindow(selectedRoot)
        if selected.window != expected { try validateWindow(selected) }
        return (expected, selected)
    }

    func validateWindow(_ surface: Surface) throws {
        try BackgroundTarget(surface.owner).validate()
        if let native = surface.native {
            guard NativeProcessIdentity.read(native.process.pid) == native.process,
                  let window = WindowServerInfo.window(native.mapping.id),
                  (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == native.process.pid else {
                throw DriverError("observed native window closed or its process changed", code: .staleTarget)
            }
            return
        }
        // AX-only surfaces have no independent WindowServer identity. Query the
        // exact retained object, without requiring its label, children or focus.
        guard let element = surface.element,
              AXAccess.read(element, kAXRoleAttribute).value as? String != nil else {
            throw DriverError("observed window is no longer available", code: .staleTarget)
        }
    }

    /// Visual input keeps native window identity independent of AX content.
    /// Re-read known modal/containment relations; an old capture's parent edge
    /// cannot by itself authorize input into a formerly attached child.
    func inputWindowScope(instance: String, root: String, surface id: String) throws -> (surface: Surface, blocked: [String]) {
        let selected = try windowScope(instance: instance, root: root, surface: id).surface
        let native = NativeWindows.list(selected.owner)
        let windows: [NativeApplicationAccess.Window]
        do { windows = try state.applications.windows(selected.owner, nativeWindows: native) }
        catch {
            try RequestDiagnostics.checkCancellation()
            // A directly selected native window is still an addressable
            // visual scope. Unknown AX data is not proof that it is closed.
            guard selected.window == root, selected.native != nil else { throw error }
            return (selected, [])
        }
        let blocked = try NativeWindowBoundary.blocked(root: root, target: selected.window,
            windows: windows.map { .init(ref: $0.ref, parent: $0.parent, modal: $0.modal == true) })
        return (selected, blocked)
    }
    private func remember(_ surface: Surface) {
        surfaces[surface.scope] = surface
        order.removeAll { $0 == surface.scope }; order.append(surface.scope)
        while order.count > 1024 { surfaces.removeValue(forKey: order.removeFirst()) }
    }
    private func result(_ kind: String, began: Int64, reasons: [String]) -> [String: Any] {
        ["kind": kind, "session": session, "platform": "macos", "stopReasons": reasons,
         "observed": ["id": UUID().uuidString, "startedAt": began, "completedAt": Int64(Date().timeIntervalSince1970 * 1000)]]
    }

    func applications() throws -> [String: Any] {
        let began = Int64(Date().timeIntervalSince1970 * 1000)
        let deadline = min((RequestDiagnostics.deadline ?? .infinity) - 0.2, ProcessInfo.processInfo.systemUptime + 3)
        var limited = false
        let entries = state.discovery.nativeApplications {
            let allowed = !Task.isCancelled && ProcessInfo.processInfo.systemUptime < deadline
            if !allowed { limited = true }
            return allowed
        }
        try RequestDiagnostics.checkCancellation()
        var reasons = [String]()
        if limited { reasons.append("application catalogue time budget; installed entries may be missing") }
        if entries.count > 4096 { reasons.append("application catalogue entry limit") }
        var value = result("applications", began: began, reasons: reasons)
        value["applications"] = try entries.prefix(4096).map { item in
            try RequestDiagnostics.checkCancellation()
            return item["isRunning"] as? Bool == false ? retainInstallation(item) : item
        }
        return value
    }

    func collectSurfaces(_ application: String) throws -> [String: Any] {
        guard ApplicationInstances<NSRunningApplication>.isInstanceSelector(application),
              case .running(let app) = try state.discovery.locate(application) else {
            throw DriverError("surface discovery requires an observed running-application id; it never launches an application", code: .invalidRequest)
        }
        let began = Int64(Date().timeIntervalSince1970 * 1000)
        var reasons = [String]()
        var provider: [String: Any] = ["status": "permissionUnavailable", "attribute": NSNull(), "writeStatus": NSNull()]
        if AXIsProcessTrusted() {
            // Provider initialization is a platform operation, not window
            // activation. Reuse the lifetime-bounded opt-in used by normal
            // reads, and expose acknowledgement without claiming readiness.
            let activation = state.accessibilityActivation.ensure(app)
            provider = ["status": activation.kind.rawValue, "attribute": activation.attribute?.rawValue as Any? ?? NSNull(),
                "writeStatus": activation.writeStatus?.rawValue as Any? ?? NSNull()]
        }
        let native = NativeWindows.listIfAvailable(app)
        if native == nil { reasons.append("WindowServer catalogue unavailable") }
        if !AXIsProcessTrusted() { reasons.append("accessibility permission unavailable; native windows are retained") }
        // Reuse the platform's explicit AX/WindowServer correspondence. This
        // describes native ownership, not AppSelection's default-view policy.
        let ax: [NativeApplicationAccess.Window]
        do {
            ax = try state.applications.windows(app, nativeWindows: native ?? [], reportIssue: { reasons.append($0) })
        } catch {
            try RequestDiagnostics.checkCancellation()
            reasons.append("AX surface catalogue failed; native windows remain available: \(error.localizedDescription)")
            ax = []
        }
        let visual = Dictionary(grouping: native ?? [], by: { state.applications.windowReference($0) })
        var entries = [[String: Any]](), known = Set<String>()
        func append(window: String, title: String, kind: String, parent: String?, modal: Bool?, element: AXUIElement?) throws {
            guard known.insert(window).inserted else { return }
            guard entries.count < 512 else { return }
            try RequestDiagnostics.checkCancellation()
            let scope = state.applications.observationScope(app, window: element, reference: window)
            let instance = state.appSelections.get(owner: app, root: window)
            let node = try element.map { try state.applications.registerNativeRoot($0, app: app, scope: scope) }
            // Missing AXParent does not prove a top-level window. Known native
            // parent edges are supplied by the platform collector; an explicit
            // AXApplication parent proves there is no containing window.
            var parentState = parent == nil ? "unknown" : "known"
            if parent == nil, let element,
               let owner = AXAccess.element(element, kAXParentAttribute),
               AXAccess.read(owner, kAXRoleAttribute).value as? String == kAXApplicationRole {
                parentState = "none"
            }
            let matching = visual[window] ?? []
            remember(Surface(owner: app, window: window, scope: scope, parent: parent,
                             element: element, native: matching.count == 1 ? matching[0] : nil))
            entries.append(["window": window, "instance": instance.id, "surface": scope, "node": node as Any? ?? NSNull(),
                "title": title, "kind": kind, "parent": parent as Any? ?? NSNull(), "parentState": parentState,
                "modal": modal as Any? ?? NSNull(),
                "nativeWindow": visual[window] != nil, "accessibility": node != nil])
        }
        for item in ax {
            try append(window: item.ref, title: item.title, kind: item.kind, parent: item.parent, modal: item.modal, element: item.element)
        }
        for item in native ?? [] {
            let ref = state.applications.windowReference(item)
            try append(window: ref, title: item.mapping.title ?? "", kind: "window", parent: nil, modal: nil, element: nil)
        }
        if known.count > entries.count { reasons.append("surface catalogue entry limit") }
        try BackgroundTarget(app).validate()
        var value = result("surfaces", began: began, reasons: reasons)
        value["application"] = application; value["surfaces"] = entries; value["provider"] = provider
        return value
    }
}
