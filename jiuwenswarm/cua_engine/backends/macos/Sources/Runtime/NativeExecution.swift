import AppKit
import ApplicationServices

/// Protocol v5 endpoint. Operation lifetime is independent of request lifetime;
/// native units never select alternative input routes on behalf of Python.
@MainActor final class NativeExecution {
    static let methods: Set<String> = ["begin", "inspect", "capture", "execute", "finish"]
    private let session: String
    private let state: NativeDesktopState
    private let cancelActive: () -> Void
    private var current: NativeOperation?
    private var pointerActivity: AgentPointer.Activity?
    private var retained = [String: NativeOperation]()
    private var order = [String]()
    private typealias Unit = NativeInputUnit
    private struct Contract {
        let id: String
        let node: String?
        let within: [String]
        let actuator: NativeInspectQuery.Actuator?
        let receiver: NativeInspectQuery.Receiver?
        let basis: [NativeInspectQuery.Dependency]
        let unit: Unit
    }
    private var contracts = [String: [Contract]]()
    private var selections = [String: NativeSelectionBasis]()
    private lazy var directory = NativeDirectory(state: state, session: session)
    private lazy var captures = NativeCapture(directory: directory, sharing: state.sharing)
    var hasUnfinishedOperation: Bool { current.map { $0.state != .closed || $0.cleanup != "complete" } ?? false }

    init(state: NativeDesktopState, session: String, cancelActive: @escaping () -> Void) {
        self.state = state; self.session = session; self.cancelActive = cancelActive
    }

    func cancel() { current?.stop("cancelled") }
    func shutdown() { current?.stop("connection_closed"); state.pointer.stop() }

    private func observePointer(_ binding: [String: Any]) {
        guard let scope = binding["surface"] as? String,
              let mapping = directory.surface(scope)?.native?.mapping else { return }
        state.pointer.observe(mapping)
    }

    private func aimPointer(_ binding: [String: Any], intent: NativeIntent, receiver: String?) {
        guard let ref = binding["node"] as? String ?? receiver else { return }
        let mapping = (binding["surface"] as? String).flatMap { directory.surface($0)?.native?.mapping }
        AgentPointer.current?.aim(element: state.applications.visualElement(ref), window: mapping,
            pulse: !["key", "scroll", "drag"].contains(intent.kind),
            count: intent.parameters["count"] as? Int ?? 1)
    }

    private func operation(_ value: Any?) throws -> NativeOperation {
        let token = try string(value, label: "operation")
        guard let operation = retained[token] else { throw DriverError("native operation expired", code: .staleTarget) }
        return operation
    }

    private struct InputScope {
        let wire: [String: Any]
        let application: NSRunningApplication
        let window: AXUIElement?
        let menuRoot: AXUIElement?
        let blocked: [String]

        func requireWindow() throws -> AXUIElement {
            guard menuRoot == nil, let window else {
                throw DriverError("this input unit requires a window surface, not an application menu", code: .inputUnavailable)
            }
            return window
        }
    }

    private func inputScope(_ raw: Any?, canonicalize: Bool = false) throws -> InputScope {
        var value = try NativeTarget(raw)
        try value.requireSession(session)
        let instance = try string(value.instance, label: "instance")
        let surface = try string(value.surface, label: "surface")
        guard value.capture == nil else { throw DriverError("native object actions do not consume a pixel capture", code: .invalidRequest) }
        let entry = try state.appSelections.entry(instance)
        if let menu = try directory.applicationRootScope(instance: instance,
                root: value.rootWindow, surface: surface) {
            if let root = entry.root {
                let windows = try state.applications.windows(entry.owner, nativeWindows: NativeWindows.list(entry.owner))
                guard let window = windows.first(where: { $0.ref == root })?.element else {
                    throw DriverError("the menu's original working window closed", code: .staleTarget)
                }
                let blocked = try NativeWindowBoundary.blocked(root: root, target: root,
                    windows: windows.map { .init(ref: $0.ref, parent: $0.parent, modal: $0.modal == true) })
                return InputScope(wire: value.wire, application: entry.owner, window: window, menuRoot: menu.element,
                                  blocked: blocked)
            }
            try BackgroundTarget(entry.owner).validateWindowlessMenu()
            return InputScope(wire: value.wire, application: entry.owner, window: nil, menuRoot: menu.element, blocked: [])
        }
        guard let root = entry.root, let discovered = directory.surface(surface),
              discovered.owner.isEqual(entry.owner) else {
            throw DriverError("target has no bound working-window observation", code: .staleTarget)
        }
        let window = discovered.window
        if !(canonicalize && value.rootWindow == nil) {
            guard try string(value.rootWindow, label: "rootWindow") == root else { throw DriverError("working root changed", code: .staleTarget) }
        }
        let native = NativeWindows.list(entry.owner)
        let windows = try state.applications.windows(entry.owner, nativeWindows: native)
        guard windows.contains(where: { $0.ref == root }), windows.contains(where: { $0.ref == window }) else {
            throw DriverError("the original working window or surface closed", code: .staleTarget)
        }
        let graph = windows.map { AppSelection.Window(ref: $0.ref, parent: $0.parent, modal: $0.modal == true) }
        var ancestor: String? = window, seen = Set<String>()
        while let value = ancestor, value != root, seen.insert(value).inserted {
            ancestor = windows.first { $0.ref == value }?.parent
        }
        let appModal = ancestor != root && windows.contains { $0.ref == window && $0.modal == true }
            && AppSelection.current(root: root, windows: graph, focused: nil) == window
        guard ancestor == root || appModal else { throw DriverError("surface is outside the selected working window", code: .staleTarget) }
        guard let nativeWindow = windows.first(where: { $0.ref == window })?.element else {
            throw DriverError("the original interaction surface closed", code: .staleTarget)
        }
        value.rootWindow = root
        // Observation may choose an app-modal surface with unknown ownership;
        // it does not decide whether a fixed input target is blocked. A modal
        // on the target's ancestor path contains that target, not blocks it.
        // Preserve the existing admission of a uniquely observed app-modal,
        // then validate its live boundary without selecting a different view.
        let blocked = try NativeWindowBoundary.blocked(root: appModal ? window : root, target: window, windows: graph)
        // A choice list can be published under the document's AXChildren while
        // AXWindow names a separate popup. Change only the physical destination,
        // with fresh exact child edges and a known non-modal popup in this app.
        if let node = value.node {
            let observed = try state.applications.readRoot(node)
            guard observed.scope == surface, observed.application.isEqual(entry.owner) else {
                throw DriverError("node belongs to a different surface", code: .staleTarget)
            }
            if let popup = WindowGeometry.owningWindow(observed.element, pid: entry.owner.processIdentifier, in: nativeWindow),
               !CFEqual(popup, nativeWindow), windows.contains(where: { CFEqual($0.element, popup) }),
               AXAccess.read(popup, kAXSubroleAttribute).value as? String != kAXStandardWindowSubrole,
               AXAccess.read(popup, kAXModalAttribute).value as? Bool != true,
               try LocalMenuAttachment.confirms(
                    leaf: observed.element, ancestors: observed.ancestors, window: nativeWindow,
                    same: { CFEqual($0, $1) }, role: { AXAccess.read($0, kAXRoleAttribute).value as? String },
                    children: { element in
                        try RequestDiagnostics.checkCancellation()
                        guard let children = AXAccess.elements(element, kAXChildrenAttribute, limit: 2048) else {
                            throw DriverError("popup attachment cannot be confirmed", code: .inputUnavailable)
                        }
                        return children
                    }) {
                return InputScope(wire: value.wire, application: entry.owner, window: popup, menuRoot: nil,
                                  blocked: blocked)
            }
        }
        return InputScope(wire: value.wire, application: entry.owner, window: nativeWindow, menuRoot: nil,
                          blocked: blocked)
    }

    private func checkContainment(_ binding: [String: Any], within: [String], op: NativeOperation) throws {
        guard !within.isEmpty else { return }
        let node = try string(binding["node"], label: "node")
        let leaf = try state.applications.readRoot(node).element
        let anchors = try within.map { ref in
            var anchored = binding; anchored["node"] = ref
            _ = try target(anchored, input: true)
            return try state.applications.readRoot(ref).element
        }
        try checkNativeContainment(leaf, anchors: anchors, op: op)
    }

    private func checkNativeContainment(_ leaf: AXUIElement, anchors: [AXUIElement], op: NativeOperation) throws {
        let evidence = try NativeContainment.check(leaf: leaf, within: anchors, same: { CFEqual($0, $1) }, parent: { element in
            let value = AXAccess.read(element, kAXParentAttribute)
            if case .invalid = value { throw DriverError("path ancestor expired", code: .staleTarget) }
            guard let raw = value.value, CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
            return unsafeDowncast(raw, to: AXUIElement.self)
        }, children: { element in
            var count: CFIndex = 0
            let status = AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &count)
            if status == .invalidUIElement { throw DriverError("path ancestor expired", code: .staleTarget) }
            if [.attributeUnsupported, .notImplemented, .noValue].contains(status) { return [] }
            guard status == .success, count >= 0, count <= 2048 else { return nil }
            if count == 0 { return [] }
            var values: CFArray?
            guard AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, 0, count, &values) == .success,
                  let children = values as? [AXUIElement], children.count == count else { return nil }
            return children
        }, check: { try op.check(); try RequestDiagnostics.checkCancellation() })
        switch evidence {
        case .confirmed: return
        case .changed: throw DriverError("selected element left its logical path scope", code: .staleTarget)
        case .unavailable: throw DriverError("selected path containment cannot be confirmed", code: .inputUnavailable)
        }
    }

    /// Check exactly the pair selected by Python, never find another editor.
    /// The native window is an outer anchor: a contradictory parent path must
    /// not be excused by a stale forward child list on the old owner.
    private func checkActuator(_ binding: [String: Any], actuator: NativeInspectQuery.Actuator?, op: NativeOperation) throws {
        guard let actuator else { return }
        if actuator.relation == "scrollPage" {
            try scrollUnit(binding, actuator: actuator).validate()
            return
        }
        if actuator.relation == "scrollPointer" {
            // Python selected the region and classified the exact hit path.
            // Check those fixed objects and containment, never choose another
            // ancestor or treat every descendant as a wheel receiver.
            guard let owner = binding["node"] as? String, let region = actuator.region else {
                throw DriverError("element scrolling requires an original target and selected region", code: .invalidRequest)
            }
            var objects = [String: AXUIElement]()
            for ref in Set([owner, region, actuator.node]) {
                var selected = binding; selected["node"] = ref
                let current = try target(selected, input: true)
                guard current.blocked.isEmpty else {
                    throw DriverError("selected scrolling object is blocked by a modal", code: .blockedByModal)
                }
                let element = try state.applications.readRoot(ref).element
                if AXAccess.read(element, kAXEnabledAttribute).value as? Bool == false {
                    throw DriverError("selected scrolling object is disabled", code: .inputUnavailable)
                }
                objects[ref] = element
            }
            guard AXAccess.read(objects[region]!, kAXRoleAttribute).value as? String == kAXScrollAreaRole else {
                throw DriverError("selected wheel region is no longer a scroll area", code: .inputUnavailable)
            }
            for ref in Set([owner, actuator.node]) where ref != region {
                try checkNativeContainment(objects[ref]!, anchors: [objects[region]!], op: op)
            }
            return
        }
        guard let owner = binding["node"] as? String, owner != actuator.node else {
            throw DriverError("a selected control member requires a distinct requested owner", code: .invalidRequest)
        }
        _ = try target(binding, input: true)
        let scope = try inputScope(binding)
        let parent = try state.applications.readRoot(owner).element
        if AXAccess.read(parent, kAXEnabledAttribute).value as? Bool == false {
            throw DriverError("the requested control owner is disabled", code: .inputUnavailable)
        }
        var selected = binding; selected["node"] = actuator.node
        let current = try target(selected, input: true)
        guard current.blocked.isEmpty else {
            throw DriverError("the selected control member is blocked by modal \(current.blocked.joined(separator: ", "))", code: .blockedByModal)
        }
        let editor = try state.applications.readRoot(actuator.node).element
        let window = try scope.requireWindow()
        let anchors = CFEqual(window, parent) ? [parent] : [window, parent]
        try checkNativeContainment(editor, anchors: anchors, op: op)
    }

    private func target(_ raw: Any?, canonicalize: Bool = false, input: Bool = false, attachedMenuAction: Bool = false) throws -> (wire: [String: Any], blocked: [String], node: String) {
        let scope = try inputScope(raw, canonicalize: canonicalize)
        let value = scope.wire
        let node = try string(value["node"], label: "node")
        let surface = try string(value["surface"], label: "surface")
        let read = try state.applications.readRoot(node)
        guard read.scope == surface else { throw DriverError("node belongs to a different surface", code: .staleTarget) }
        try read.validate()
        if let menu = scope.menuRoot {
            // An application menu has no AXWindow. Its membership is proved
            // against the retained, freshly re-read AXMenuBar instead.
            if !CFEqual(read.element, menu) {
                let evidence = try NativeContainment.check(leaf: read.element, within: [menu], same: { CFEqual($0, $1) }, parent: {
                    AXAccess.element($0, kAXParentAttribute)
                }, children: {
                    AXAccess.elements($0, kAXChildrenAttribute, limit: 2048)
                }, check: { try RequestDiagnostics.checkCancellation() })
                guard evidence == .confirmed else {
                    throw DriverError("the selected item is no longer confirmed within the observed application menu",
                                      code: evidence == .changed ? .staleTarget : .inputUnavailable)
                }
            }
        } else if input {
            // A retained object may move to another window without changing
            // its role, identifier or value. The old registry scope is not
            // evidence of its current input boundary.
            let expected = try scope.requireWindow()
            guard let actual = WindowGeometry.owningWindow(read.element, pid: scope.application.processIdentifier, in: expected) else {
                throw DriverError("native target's current window membership is unavailable", code: .inputUnavailable)
            }
            let localMenu = try attachedMenuAction && !CFEqual(actual, expected) && LocalMenuAttachment.confirms(
                leaf: read.element, ancestors: read.ancestors, window: expected,
                same: { CFEqual($0, $1) }, role: { AXAccess.read($0, kAXRoleAttribute).value as? String },
                children: { element in
                    try RequestDiagnostics.checkCancellation()
                    guard let children = AXAccess.elements(element, kAXChildrenAttribute, limit: 2048) else {
                        throw DriverError("local menu attachment cannot be confirmed", code: .inputUnavailable)
                    }
                    return children
                })
            guard CFEqual(actual, expected) || localMenu else {
                throw DriverError("native target moved outside its observed interaction surface", code: .staleTarget)
            }
        }
        return (value, scope.blocked, node)
    }

    private func inputBinding(_ raw: Any?, input: Bool = false, attachedMenuAction: Bool = false) throws -> (wire: [String: Any], blocked: [String], node: String?) {
        if let value = raw as? [String: Any], value["capture"] is String {
            let target = try NativeTarget(value)
            guard target.session == session, target.node == nil else {
                throw DriverError("visual input requires this session's exact window image binding", code: .invalidRequest)
            }
            if target.isDesktopImage {
                guard target.instance == nil, target.rootWindow == nil else {
                    throw DriverError("desktop input cannot contain application/window identities", code: .invalidRequest)
                }
                try desktopUnit(value).check()
                return (value, [], nil)
            }
            let visual = try captures.input(value)
            return (value, visual.blocked, nil)
        }
        if (raw as? [String: Any])?["node"] is NSNull {
            let scope = try inputScope(raw)
            return (scope.wire, scope.blocked, nil)
        }
        let value = try target(raw, input: input, attachedMenuAction: attachedMenuAction)
        return (value.wire, value.blocked, value.node)
    }

    private func isDesktop(_ binding: [String: Any]) -> Bool {
        binding["surface"] is NSNull && binding["capture"] is String
    }

    private func desktopUnit(_ binding: [String: Any]) throws -> NativeDesktopUnit {
        NativeDesktopUnit(desktop: state.desktop, ref: try string(binding["capture"], label: "capture"))
    }

    private func receiverRelation(_ binding: [String: Any], actuator: NativeInspectQuery.Actuator?,
                                 receiver: NativeInspectQuery.Receiver?) throws -> NativeReceiverRelation? {
        guard let receiver else { return nil }
        let editor = try actuator?.node ?? string(binding["node"], label: "node")
        guard receiver.reference != editor else { throw DriverError("related receiver must differ from its control", code: .invalidRequest) }
        var related = binding; related["node"] = receiver.reference
        _ = try inputBinding(related)
        return NativeReceiverRelation(editor: try state.applications.readRoot(editor).element,
            related: try state.applications.readRoot(receiver.reference).element, selection: receiver)
    }

    private func keyboardUnit(_ binding: [String: Any], actuator: NativeInspectQuery.Actuator? = nil, receiver: NativeInspectQuery.Receiver? = nil) throws -> NativeKeyboardUnit {
        let scope = try inputScope(binding)
        let node = actuator?.node ?? (binding["node"] as? String)
        return NativeKeyboardUnit(application: scope.application, window: try scope.requireWindow(),
            element: try node.map { try state.applications.readRoot($0).element },
            receiverRelation: try receiverRelation(binding, actuator: actuator, receiver: receiver))
    }

    private func selectedTextUnit(_ binding: [String: Any], actuator: NativeInspectQuery.Actuator?, receiver: NativeInspectQuery.Receiver? = nil) throws -> NativeSelectedTextUnit {
        let scope = try inputScope(binding)
        let node = try actuator?.node ?? string(binding["node"], label: "node")
        return NativeSelectedTextUnit(application: scope.application, window: try scope.requireWindow(),
            element: try state.applications.readRoot(node).element,
            receiverRelation: try receiverRelation(binding, actuator: actuator, receiver: receiver))
    }

    private func elementPointerUnit(_ binding: [String: Any], actuator: NativeInspectQuery.Actuator? = nil) throws -> NativeElementPointerUnit {
        let scope = try inputScope(binding)
        let node = try string(binding["node"], label: "node")
        let scrolling = actuator?.relation == "scrollPointer"
        let geometry = scrolling ? actuator!.region! : node
        return NativeElementPointerUnit(application: scope.application, window: try scope.requireWindow(),
            element: try state.applications.readRoot(geometry).element,
            member: try actuator.map { try state.applications.readRoot($0.node).element },
            kind: scrolling ? .scroll : .click,
            logicalWindow: (binding["surface"] as? String).flatMap { directory.surface($0)?.element })
    }

    private func menuUnit(_ binding: [String: Any], within: [String] = []) throws -> NativeMenuUnit {
        let scope = try inputScope(binding)
        guard scope.menuRoot != nil, let node = binding["node"] as? String else {
            throw DriverError("menu unit requires an application-menu element", code: .invalidRequest)
        }
        return NativeMenuUnit(application: scope.application, window: scope.window,
            element: try state.applications.readRoot(node).element, root: scope.menuRoot!,
            path: try (within + [node]).map { try state.applications.readRoot($0).element })
    }

    private func scrollUnit(_ binding: [String: Any], actuator: NativeInspectQuery.Actuator) throws -> NativeScrollUnit {
        guard actuator.relation == "scrollPage", let region = actuator.region, let bar = actuator.bar,
              let requested = binding["node"] as? String else {
            throw DriverError("scrolling requires a selected native page route", code: .invalidRequest)
        }
        func object(_ ref: String) throws -> AXUIElement {
            var selected = binding; selected["node"] = ref
            let current = try target(selected, input: true)
            guard current.blocked.isEmpty else { throw DriverError("scrolling target is blocked by a modal", code: .blockedByModal) }
            return try state.applications.readRoot(ref).element
        }
        return NativeScrollUnit(requested: try object(requested), region: try object(region),
                                bar: try object(bar), button: try object(actuator.node))
    }

    private func same(_ a: [String: Any], _ b: [String: Any]) -> Bool { NSDictionary(dictionary: a).isEqual(to: b) }

    private func directoryTarget(_ raw: Any?) throws -> [String: Any]? {
        let value = try NativeTarget(raw)
        try value.requireSession(session)
        return value.isDiscovery ? value.wire : nil
    }

    private func windowTarget(_ raw: Any?) throws -> [String: Any] {
        var value = try NativeTarget(raw)
        guard value.session == session, value.node == nil, value.capture == nil else {
            throw DriverError("window capture requires this session's window binding, without an element or old image", code: .invalidRequest)
        }
        let resolved = try directory.windowScope(instance: string(value.instance, label: "instance"),
            root: value.rootWindow, surface: string(value.surface, label: "surface"))
        value.rootWindow = resolved.root
        return value.wire
    }

    private func observationTarget(_ raw: Any?) throws -> (wire: [String: Any], node: String) {
        var value = try NativeTarget(raw)
        let node = try string(value.node, label: "node")
        var window = value
        window.node = nil
        let scope = try readScope(window.wire)
        let observed = try state.applications.nativeReadTarget(node)
        let surface = try string(scope.wire["surface"], label: "surface")
        guard observed.scope == surface, observed.application.isEqual(scope.owner) else {
            throw DriverError("observed node belongs to another UI scope", code: .staleTarget)
        }
        value.rootWindow = scope.wire["rootWindow"] as? String
        return (value.wire, node)
    }

    private func readScope(_ raw: Any?) throws -> (wire: [String: Any], owner: NSRunningApplication) {
        let value = try NativeTarget(raw)
        guard value.session == session, value.node == nil, value.capture == nil else {
            throw DriverError("UI-root observation requires this session's read binding", code: .invalidRequest)
        }
        if let appRoot = try directory.applicationRootScope(instance: string(value.instance, label: "instance"),
                root: value.rootWindow, surface: string(value.surface, label: "surface")) {
            return (value.wire, appRoot.owner)
        }
        let window = try windowTarget(value.wire)
        guard let surface = directory.surface(try string(window["surface"], label: "surface")) else {
            throw DriverError("observed window scope is unavailable", code: .staleTarget)
        }
        return (window, surface.owner)
    }

    /// Both collection routes resolve before reading and revalidate before
    /// publishing. A write operation also retains the facts used for selection.
    private func observeNodes(_ raw: Any?, op: NativeOperation,
                              collect: (String) throws -> [String: Any]) throws -> [String: Any] {
        let binding = try observationTarget(raw)
        let result = try collect(binding.node)
        _ = try observationTarget(raw)
        try op.check()
        if op.writable { try selections[op.token]!.retain(result) }
        else { observePointer(binding.wire) }
        return result
    }

    func perform(_ method: String, _ raw: Any?, timeoutMs: Int) async throws -> [String: Any] {
        let token = (raw as? [String: Any])?["operation"] as? String
        let activity = token == current?.token ? pointerActivity : nil
        return try await AgentPointer.$current.withValue(activity) {
            do { return try await self.performRequest(method, raw, timeoutMs: timeoutMs) }
            catch { activity?.recordOutcome(success: false); throw error }
        }
    }

    private func performRequest(_ method: String, _ raw: Any?, timeoutMs: Int) async throws -> [String: Any] {
        if ["inspect", "capture", "execute"].contains(method) {
            let op = try operation((raw as? [String: Any])?["operation"])
            let deadline = min(op.deadline, RequestDiagnostics.deadline ?? .infinity)
            return try await RequestDiagnostics.$deadline.withValue(deadline) {
                if method == "capture" {
                    let budget = ObservationBudget(deadline: deadline, startedAt: ProcessInfo.processInfo.systemUptime)
                    return try await RequestDiagnostics.$observationBudget.withValue(budget) {
                        try await self.performStep(method, raw, timeoutMs: timeoutMs)
                    }
                }
                return try await self.performStep(method, raw, timeoutMs: timeoutMs)
            }
        }
        return try await performStep(method, raw, timeoutMs: timeoutMs)
    }

    private func performStep(_ method: String, _ raw: Any?, timeoutMs: Int) async throws -> [String: Any] {
        switch method {
        case "begin":
            let begin = try NativeBeginRequest(raw), p = begin.value
            guard !hasUnfinishedOperation else { throw DriverError("another native operation owns the desktop", code: .invalidRequest) }
            let mode = begin.mode, request = begin.intent, intent = request.kind
            let budgetStart = ProcessInfo.processInfo.systemUptime
            let installation = intent == "launch" ? try directory.installation(p["target"]) : nil
            let discovery = installation == nil ? try directoryTarget(p["target"]) : nil
            let visual = (p["target"] as? [String: Any])?["capture"] is String
            let windowScope = installation == nil && discovery == nil && !visual && (p["target"] as? [String: Any])?["node"] is NSNull
            let initial: [String: Any]
            let releaseNode: () -> Void
            if let installation {
                try state.sharing.checkAvailable()
                initial = installation.wire; releaseNode = {}
            } else if let discovery {
                guard mode == "read" else { throw DriverError("unbound discovery cannot authorize input", code: .invalidRequest) }
                initial = discovery; releaseNode = {}
            } else if visual {
                let desktop = isDesktop(p["target"] as! [String: Any])
                guard mode == "write", NativePointerIntent.Kind(rawValue: intent) != nil || desktop && ["insert", "key"].contains(intent) else {
                    throw DriverError("image input supports pointer intents; desktop input also supports insertion and keys", code: .invalidRequest)
                }
                try state.sharing.checkAvailable()
                initial = try inputBinding(p["target"]).wire; releaseNode = {}
            } else if windowScope {
                try state.sharing.checkAvailable()
                initial = mode == "read" ? try readScope(p["target"]).wire : try inputScope(p["target"], canonicalize: true).wire
                releaseNode = {}
            } else {
                try state.sharing.checkAvailable()
                let resolved: (wire: [String: Any], node: String)
                if mode == "read" { resolved = try observationTarget(p["target"]) }
                else { let input = try target(p["target"], canonicalize: true); resolved = (input.wire, input.node) }
                guard let retained = state.applications.retainNode(resolved.node) else {
                    throw DriverError("operation target expired before retention", code: .staleTarget)
                }
                initial = resolved.wire; releaseNode = retained
            }
            var admitted = false
            defer { if !admitted { releaseNode() } }
            let lease = mode == "write" ? try await DesktopLease.acquire() : nil
            do {
                let binding = installation != nil ? try directory.installation(initial).wire
                    : discovery != nil ? initial : visual ? try inputBinding(initial).wire
                    : windowScope ? (mode == "read" ? try readScope(initial).wire : try inputScope(initial).wire)
                    : mode == "read" ? try observationTarget(initial).wire : try target(initial).wire
                let remaining = timeoutMs - Int((ProcessInfo.processInfo.systemUptime - budgetStart) * 1000)
                guard remaining > 0 else { throw RequestDeadlineExceeded() }
                let selection = NativeSelectionBasis(access: state.applications)
                let op = NativeOperation(timeoutMs: remaining, writable: mode == "write", intent: request,
                    target: binding, release: { selection.clear(); releaseNode(); lease?.release() }, cancelActive: cancelActive)
                admitted = true
                let activity = mode == "write" && intent != "launch" ? state.pointer.begin() : nil
                pointerActivity = activity
                op.onClose = { success in activity?.finish(success: success) }
                current = op; retained[op.token] = op; order.append(op.token); contracts[op.token] = []; selections[op.token] = selection
                while order.count > 8 {
                    let token = order.removeFirst()
                    retained.removeValue(forKey: token); contracts.removeValue(forKey: token); selections.removeValue(forKey: token)
                }
                return ["operation": op.token, "target": binding, "remainingMs": remaining]
            } catch { lease?.release(); throw error }
        case "capture":
            let request = try NativeCaptureRequest(raw)
            let op = try operation(request.operation)
            guard !op.writable, same(request.target, op.target) else {
                throw DriverError("capture requires the operation's original read scope", code: .invalidRequest)
            }
            return try await op.observe {
                try self.state.sharing.checkAvailable()
                switch request.kind {
                case .window:
                    let binding = try self.windowTarget(request.target)
                    let result = try await self.captures.capture(binding: binding, maxDimension: request.maxDimension)
                    self.observePointer(binding)
                    return result
                case .desktop:
                    guard try self.directoryTarget(request.target) != nil else {
                        throw DriverError("desktop capture needs this session's read scope", code: .invalidRequest)
                    }
                    let interval = CaptureInterval()
                    var result = try await self.state.desktop.read(maxDimension: request.maxDimension)
                    result["kind"] = "desktopCapture"
                    result["binding"] = request.target
                    result["observed"] = interval.observation()
                    return result
                }
            }
        case "inspect":
            let p = try object(raw, keys: ["operation", "target", "query"])
            let query = try NativeInspectQuery(p["query"])
            let op = try operation(p["operation"])
            try op.check()
            guard let requested = p["target"] as? [String: Any],
                  query.observesNodes ? op.permitsObservation(requested) : op.permits(requested) else {
                throw DriverError("operation target changed", code: .staleTarget)
            }
            switch query {
            case .launch:
                guard op.writable, op.intent == "launch" else {
                    throw DriverError("launch inspection requires an installation write operation", code: .invalidRequest)
                }
                let item = try directory.installation(p["target"])
                let unit = Unit.launch(item.identity)
                let contract = contracts[op.token]?.first(where: { $0.unit == unit })
                    ?? Contract(id: UUID().uuidString, node: nil, within: [], actuator: nil, receiver: nil, basis: [], unit: unit)
                contracts[op.token] = [contract]
                return ["kind": "launch", "binding": item.wire, "observed": UUID().uuidString, "targetLabel": item.name,
                    "primitives": [unit.declaration(id: contract.id, supported: true,
                        readiness: NativeReadiness(), exclusive: BackgroundTarget.isExclusive)]]
            case .applications:
                guard !op.writable, try directoryTarget(requested) != nil else {
                    throw DriverError("directory query requires a session read scope", code: .invalidRequest)
                }
                let result = try directory.applications()
                try op.check()
                return result
            case .capturedText(let ref, let offset, let length):
                guard !op.writable, try directoryTarget(requested) != nil else {
                    throw DriverError("captured text requires a session read scope", code: .invalidRequest)
                }
                // Read the retained string, never the current AX object. It
                // remains readable after its window/element has disappeared.
                let page = try state.applications.readText(ref, offset: offset, length: length)
                try op.check()
                return ["kind": "capturedText", "session": session, "textRef": ref, "page": page]
            case .surfaces(let application):
                guard !op.writable, try directoryTarget(requested) != nil else {
                    throw DriverError("directory query requires a session read scope", code: .invalidRequest)
                }
                try state.sharing.checkAvailable()
                let result = try directory.collectSurfaces(application)
                try op.check()
                return result
            case .roots(let application):
                guard !op.writable, try directoryTarget(requested) != nil else {
                    throw DriverError("application roots require a session read scope", code: .invalidRequest)
                }
                let result = try directory.collectRoots(application)
                try op.check()
                return result
            case .relation(let property, let cursor, let limit):
                try state.sharing.checkAvailable()
                guard NativeFactCollector.supportedRelations.contains(property) else {
                    throw DriverError("unsupported native relation", code: .invalidRequest)
                }
                return try observeNodes(p["target"], op: op) { node in
                    try state.applications.nativeRelation(node, session: session, property: property, cursor: cursor, limit: limit)
                }
            case .nodes(let fields, let nodes, let depth, let childrenPage, let relationPage, let advertisedFields, let followRelations):
                try state.sharing.checkAvailable()
                guard Set(fields).isSubset(of: Set(NativeFactCollector.fields)) else {
                    throw DriverError("unsupported native field profile", code: .invalidRequest)
                }
                guard Set(followRelations).isSubset(of: Set(NativeFactCollector.supportedRelations)) else {
                    throw DriverError("unsupported native relation profile", code: .invalidRequest)
                }
                if let relationPage, !NativeFactCollector.supportedRelations.contains(relationPage.property) {
                    throw DriverError("unsupported native page relation", code: .invalidRequest)
                }
                return try observeNodes(p["target"], op: op) { node in
                    try state.applications.nativeFacts(node, session: session, fields: fields,
                        maxNodes: nodes, maxDepth: depth, childrenPage: childrenPage, relationPage: relationPage,
                        advertisedFields: advertisedFields, followRelations: followRelations)
                }
            case .inputContext(let within, let actuator, let receiver, let basis):
                try state.sharing.checkAvailable()
                let binding = try inputBinding(p["target"])
                let desktop = isDesktop(binding.wire)
                guard !desktop || (within.isEmpty && actuator == nil && receiver == nil && basis.isEmpty) else {
                    throw DriverError("desktop input cannot carry element selection dependencies", code: .invalidRequest)
                }
                guard !within.contains(binding.node ?? "") else { throw DriverError("within must contain proper ancestors", code: .invalidRequest) }
                guard binding.node != nil || within.isEmpty else { throw DriverError("window input cannot carry element ancestors", code: .invalidRequest) }
                guard actuator == nil || (actuator?.relation == "containedEditor"
                    ? NativeKeyboardIntent.Kind(rawValue: op.intent) != nil
                    : actuator?.relation == "controlPart" ? op.intent == "click" : op.intent == "scroll") else {
                    throw DriverError("this input intent does not accept the selected actuator", code: .invalidRequest)
                }
                try checkContainment(binding.wire, within: within, op: op)
                try checkActuator(binding.wire, actuator: actuator, op: op)
                guard (actuator == nil && receiver == nil) || !basis.isEmpty else {
                    throw DriverError("a selected actuator requires its semantic selection dependencies", code: .invalidRequest)
                }
                guard receiver == nil || (binding.node != nil && !(binding.wire["capture"] is String)
                    && NativeKeyboardIntent.Kind(rawValue: op.intent) != nil
                    && (actuator == nil || actuator?.relation == "containedEditor")) else {
                    throw DriverError("receiver relation requires a keyboard or text receiver intent", code: .invalidRequest)
                }
                if let receiver {
                    _ = try receiverRelation(binding.wire, actuator: actuator, receiver: receiver)
                }
                let selection = selections[op.token]!
                let scope = desktop ? state.desktop.scope : try string(binding.wire["surface"], label: "surface")
                try selection.validate(basis, scope: scope)
                let previous = contracts[op.token] ?? []
                var currentContracts = [Contract](), primitives = [[String: Any]]()
                func declare(_ unit: Unit, supported: Bool, reason: String? = nil,
                             readiness: (Bool) throws -> NativeReadiness = { _ in NativeReadiness() }) rethrows {
                    let prior = previous.first { $0.node == binding.node && $0.within == within && $0.actuator == actuator && $0.receiver == receiver && $0.unit == unit
                        && selection.equivalent($0.basis, basis, scope: scope) }
                    let contract = Contract(id: prior?.id ?? UUID().uuidString, node: binding.node,
                        within: within, actuator: actuator, receiver: receiver, basis: basis, unit: unit)
                    currentContracts.append(contract)
                    let sampled = try readiness(op.preparationConfirmed(contract.id))
                    let conditions: NativeReadiness
                    if case .menuAction(let name, _) = unit {
                        if name == kAXCancelAction { conditions = NativeReadiness() }
                        else if binding.wire["rootWindow"] is String, !op.preparationConfirmed(contract.id) {
                            conditions = NativeReadiness([("receiver", "unknown")])
                        } else { conditions = sampled }
                    }
                    else { conditions = sampled.requiringEnabled(enabled) }
                    var declaration = unit.declaration(id: contract.id, supported: supported,
                        readiness: conditions, exclusive: BackgroundTarget.isExclusive)
                    if let reason { declaration["unavailableReason"] = reason }
                    primitives.append(declaration)
                }
                let enabled: Bool?, foreground: Bool
                var input: [String: Any] = ["keyboardSurface": NSNull(), "focusedNode": NSNull(), "focusAncestors": [],
                    "editor": NSNull(), "editorOwner": NSNull(), "selectionKnown": "unknown",
                    "geometryCurrent": "unknown", "windowRoute": "unknown", "pointerRoute": "unknown"]
                var pointerUnavailableReason: String?
                func recordPointer(_ unit: NativeElementPointerUnit, geometryNode: String? = nil) throws -> NativeReadiness {
                    let pointer = try unit.conditions()
                    pointerUnavailableReason = pointer.reason
                    input["geometryCurrent"] = pointer.geometry ? "yes" : "unknown"
                    input["pointerRoute"] = pointer.geometry ? (pointer.route ? "yes" : "no") : "unknown"
                    if let hit = pointer.hit {
                        if CFEqual(hit, unit.element) { input["pointerHit"] = geometryNode ?? binding.node! }
                        else if let member = unit.member, CFEqual(hit, member) { input["pointerHit"] = actuator!.node }
                        else {
                            do {
                                input["pointerHit"] = try state.applications.registerNativeRoot(hit, app: unit.application,
                                    scope: string(binding.wire["surface"], label: "surface"))
                            } catch let error as DriverError where error.code == .staleTarget {
                                // This optional hit disappeared while reading. It
                                // cannot invalidate a usable action on the owner.
                                input.removeValue(forKey: "pointerHit")
                            }
                        }
                    }
                    return pointer.readiness
                }
                var nativeRole: String?, actionsComplete = false, applicationMenu = false
                // An independent object edit does not need a keyboard receiver.
                // Do not sample or prepare unrelated focus to declare it.
                let textFacts = try op.intent == "replace" && actuator == nil && receiver == nil
                    ? binding.node.map { try state.applications.nativeTextValueFacts($0) } : nil
                if desktop {
                    let unit = try desktopUnit(binding.wire)
                    enabled = nil; foreground = true
                    if NativePointerIntent.Kind(rawValue: op.intent) != nil {
                        let pointer = try NativePointerIntent(unit: "pointer_sequence", kind: op.intent,
                            parameters: op.request.parameters, foreground: false)
                        _ = try unit.pointer(pointer)
                        input["geometryCurrent"] = "yes"; input["pointerRoute"] = "yes"
                    } else {
                        // Empty insertion is resolved by Python after this
                        // scope observation, without selecting an input unit.
                        if op.intent == "insert", op.request.parameters["text"] as? String == "" {
                            _ = try object(op.request.parameters, keys: ["text"])
                        } else {
                            _ = try NativeKeyboardIntent(unit: "key_sequence", kind: op.intent,
                                parameters: op.request.parameters, foreground: false)
                        }
                        _ = try state.desktop.keyboardDestination(unit.ref)
                    }
                    try declare(.desktop(op.intent), supported: true)
                } else if let actuator, actuator.relation == "scrollPage" {
                    let facts = try state.applications.nativeActionFacts(binding.node!)
                    enabled = facts.enabled; foreground = facts.foreground; nativeRole = facts.role
                    try declare(.scroll, supported: true)
                } else if let actuator, actuator.relation == "scrollPointer" {
                    _ = try NativePointerIntent(unit: "pointer_sequence", kind: op.intent,
                        parameters: op.request.parameters, foreground: false, element: true)
                    let facts = try state.applications.nativeActionFacts(binding.node!)
                    enabled = facts.enabled; foreground = facts.foreground; nativeRole = facts.role
                    let readiness = try recordPointer(elementPointerUnit(binding.wire, actuator: actuator), geometryNode: actuator.region)
                    declare(.pointer(.scroll), supported: true, reason: pointerUnavailableReason, readiness: { _ in readiness })
                } else if let actuator, actuator.relation == "controlPart" {
                    let facts = try state.applications.nativeActionFacts(binding.node!)
                    enabled = facts.enabled; foreground = facts.foreground; nativeRole = facts.role
                    let readiness = try recordPointer(elementPointerUnit(binding.wire, actuator: actuator))
                    declare(.pointer(.click), supported: true, reason: pointerUnavailableReason, readiness: { _ in readiness })
                } else if binding.wire["capture"] is String, let kind = NativePointerIntent.Kind(rawValue: op.intent) {
                    let unit = NativePointerUnit(capture: try captures.input(binding.wire).entry)
                    let request = try NativePointerIntent(unit: "pointer_sequence", kind: op.intent,
                        parameters: op.request.parameters, foreground: false)
                    let point = try unit.point(request.start!)
                    enabled = nil; foreground = unit.foregroundWindow(at: point)
                    input["geometryCurrent"] = "yes"
                    let ready = unit.ready(at: point, kind: request.kind)
                    input["pointerRoute"] = ready ? "yes" : "no"
                    var pending = [("receiver", ready ? "yes" : "no")]
                    if BackgroundTarget.isExclusive { pending.append(("foreground", foreground ? "yes" : "no")) }
                    declare(.pointer(kind), supported: true, readiness: { _ in NativeReadiness(pending) })
                } else if let facts = textFacts, let contract = facts.contract {
                    enabled = facts.enabled; foreground = facts.foreground
                    try declare(.textValue(contract), supported: true)
                } else if let kind = NativeKeyboardIntent.Kind(rawValue: op.intent) {
                    let unit = try keyboardUnit(binding.wire, actuator: actuator, receiver: receiver), facts = try unit.sample()
                    enabled = unit.element.flatMap { AXAccess.read($0, kAXEnabledAttribute).value as? Bool }
                    foreground = facts.foreground
                    nativeRole = unit.element.flatMap { AXAccess.read($0, kAXRoleAttribute).value as? String }
                    input["windowRoute"] = facts.windowRoute
                    if facts.windowRoute == "yes" { input["keyboardSurface"] = binding.wire["surface"] }
                    func register(_ element: AXUIElement) throws -> String {
                        try state.applications.registerNativeRoot(element, app: unit.application,
                            scope: string(binding.wire["surface"], label: "surface"))
                    }
                    if let focus = facts.focus { input["focusedNode"] = try register(focus) }
                    input["focusAncestors"] = try facts.ancestors.map(register)
                    if let actuator {
                        input["editor"] = actuator.node
                        input["editorOwner"] = binding.node!
                    }
                    if [.insert, .replace, .paste].contains(kind), let element = unit.element {
                        let edit = NativeSelectedTextUnit(application: unit.application, window: unit.window, element: element, receiverRelation: unit.receiverRelation)
                        if kind != .paste, let contract = edit.contract(replace: kind == .replace) {
                            try declare(.selectedText(contract), supported: true) { _ in try edit.readiness(contract) }
                        } else {
                            let receiver = NativeReceiverPreparation(application: unit.application, window: unit.window, element: element, receiverRelation: unit.receiverRelation)
                            if let contract = receiver.contract(), try !receiver.readiness().ready {
                                // This declares only AX preparation. The public
                                // resolver selected this exact candidate;
                                // reinspection must prove any editing capability.
                                try declare(.receiverContext(contract), supported: true) { _ in try receiver.readiness() }
                            }
                        }
                    }
                    if kind == .replace {
                        if let element = unit.element,
                           let selection = NativeReplacementUnit(element: element).contract() {
                            let readiness = try unit.readiness(kind: kind, replacement: selection, state: facts)
                            input["selectionKnown"] = readiness.pending.contains("selection") ? "unknown" : "yes"
                            try declare(.keyboard(kind, selection), supported: true) { prepared in
                                try unit.readiness(kind: kind, replacement: selection, prepared: prepared, state: facts)
                            }
                        }
                    } else {
                        try declare(.keyboard(kind), supported: true) { prepared in
                            try unit.readiness(kind: kind, key: op.request.parameters["key"] as? String,
                                               prepared: prepared, state: facts)
                        }
                    }
                } else if let node = binding.node {
                    let facts = try state.applications.nativeActionFacts(node)
                    enabled = facts.enabled; foreground = facts.foreground
                    applicationMenu = facts.applicationMenuContext
                    nativeRole = facts.role.flatMap { $0.isEmpty ? nil : $0 }; actionsComplete = facts.actions != nil
                    for action in facts.actions ?? [] {
                        if directory.applicationRoot(binding.wire["surface"] as? String ?? "") != nil {
                            let readiness = try menuUnit(binding.wire).readiness(enabled: facts.enabled)
                            declare(.menuAction(action.name, action.description), supported: true, readiness: { _ in readiness })
                        } else {
                            try declare(.action(action.name, action.description), supported: !facts.applicationMenuContext
                                && (action.name != kAXRaiseAction || BackgroundTarget.isExclusive || facts.foreground))
                        }
                    }
                    if op.intent == "click", directory.applicationRoot(binding.wire["surface"] as? String ?? "") == nil,
                       !facts.applicationMenuContext {
                        let readiness = try recordPointer(elementPointerUnit(binding.wire))
                        declare(.pointer(.click), supported: true, reason: pointerUnavailableReason, readiness: { _ in readiness })
                    }
                } else { throw DriverError("this intent requires an element target", code: .invalidRequest) }
                // A query refreshes only this selection's declarations. Reading
                // the requested owner must not erase a prepared editor/member
                // contract. Its identity and dependencies are still rechecked
                // by execute; changed declarations for the same selection retire
                // their old IDs. Never evict an unrelated live contract silently.
                if let pinned = try op.contentPreparationCandidate(in: currentContracts.map(\.id)) {
                    currentContracts = currentContracts.filter { $0.id == pinned }
                    primitives = primitives.filter { $0["id"] as? String == pinned }
                }
                let otherContracts = previous.filter {
                    $0.node != binding.node || $0.within != within || $0.actuator != actuator || $0.receiver != receiver
                }
                guard otherContracts.count + currentContracts.count <= 256 else {
                    throw DriverError("native input contract budget exhausted", code: .observationIncomplete)
                }
                contracts[op.token] = otherContracts + currentContracts
                try op.check()
                input["foregroundSurface"] = foreground ? binding.wire["surface"]! : NSNull()
                var response: [String: Any] = ["kind": "inputContext", "binding": binding.wire, "observed": UUID().uuidString,
                    "targetLabel": desktop ? "Desktop" : (binding.node == nil ? "working window" : "native element"),
                    "identity": "yes", "withinScope": "yes", "enabled": enabled.map { $0 ? "yes" : "no" } ?? "unknown",
                    "blockedBy": binding.blocked, "primitives": primitives,
                    "nativeRole": nativeRole as Any? ?? NSNull(), "actionsComplete": actionsComplete, "applicationMenu": applicationMenu, "within": within,
                    "actuator": actuator.map(\.wire) as Any? ?? NSNull(),
                    "input": input]
                if !basis.isEmpty { response["basis"] = basis.map(\.wire) }
                if let receiver { response["receiver"] = receiver.wire }
                return response
            }
        case "execute":
            let p = try object(raw, keys: ["operation", "step", "command"])
            let op = try operation(p["operation"])
            let n = try number(p["step"], label: "step")
            guard n > 0, n.rounded() == n, n <= 9_007_199_254_740_991 else { throw DriverError("invalid native step", code: .invalidRequest) }
            let decoded = try NativeUnitCommand(p["command"])
            let command = decoded.value
            let kind = decoded.kind, intentKind = op.request.kind, foreground = decoded.foreground
            let bound = decoded.target
            guard op.permits(bound), (kind == "launch_application") == (intentKind == "launch") else {
                throw DriverError("native object contract or target mismatch", code: .invalidRequest)
            }
            if kind == "launch_application" {
                return try await executeLaunch(op, step: Int64(n), command: decoded)
            }
            guard !foreground || kind == "prepare_receiver" else {
                throw DriverError("only receiver preparation may request the foreground", code: .invalidRequest)
            }
            let id = decoded.primitiveID
            let payload = try JSONSerialization.data(withJSONObject: command, options: [.sortedKeys, .withoutEscapingSlashes])
            return try await op.execute(step: Int64(n), payload: payload) {
                let receipt = NativeExecutionReceipt(target: bound, onEffect: self.state.recordEffect)
                var alreadySatisfied = false
                do {
                    try self.state.sharing.checkAvailable()
                    try op.check(write: true)
                    try op.requirePreparationCandidate(id)
                    // Resolve the live contract only after step deduplication.
                    // A completed step remains retrievable if a later inspect
                    // found different declarations or the action closed its UI.
                    guard let contract = self.contracts[op.token]?.first(where: { $0.id == id }) else {
                        throw DriverError("unknown native primitive", code: .invalidRequest)
                    }
                    let binding = try self.inputBinding(command["target"])
                    guard contract.node == binding.node else {
                        throw DriverError("native primitive belongs to a different target", code: .staleTarget)
                    }
                    guard binding.blocked.isEmpty else { throw DriverError("target blocked by modal \(binding.blocked.joined(separator: ", "))", code: .blockedByModal) }
                    try op.bindInputTarget(bound, within: contract.within) {
                        var releases = [() -> Void]()
                        for ref in contract.within + (binding.node.map { [$0] } ?? []) {
                            guard let release = self.state.applications.retainNode(ref) else {
                                releases.reversed().forEach { $0() }; return nil
                            }
                            releases.append(release)
                        }
                        return { releases.reversed().forEach { $0() } }
                    }
                    self.aimPointer(bound, intent: op.request, receiver: contract.receiver?.reference)
                    if let actuator = contract.actuator {
                        for ref in Set(actuator.references) {
                            try op.retainInputReference(ref) { self.state.applications.retainNode(ref) }
                        }
                    }
                    if let receiver = contract.receiver {
                        try op.retainInputReference(receiver.reference) { self.state.applications.retainNode(receiver.reference) }
                    }
                    @MainActor func checkBoundary() throws {
                        try op.check(write: true)
                        let attachedMenuAction: Bool
                        if case .action = contract.unit { attachedMenuAction = true } else { attachedMenuAction = false }
                        let final = try self.inputBinding(command["target"], input: true, attachedMenuAction: attachedMenuAction)
                        guard final.blocked.isEmpty else {
                            throw DriverError("target became blocked by modal \(final.blocked.joined(separator: ", "))", code: .blockedByModal)
                        }
                        try self.checkContainment(bound, within: contract.within, op: op)
                        try self.checkActuator(bound, actuator: contract.actuator, op: op)
                        if contract.actuator?.relation == "scrollPointer" {
                            // A preceding mouseMoved/entered event may change a
                            // passive hit into another scrolling/input region.
                            // Check the selected facts before EVERY wheel event,
                            // not merely at the sequence's first dispatch barrier.
                            try self.selections[op.token]!.check(contract.basis, scope: binding.wire["surface"] as! String)
                        }
                        try op.check(write: true)
                        try RequestDiagnostics.checkCancellation()
                    }
                    @MainActor func checkSelection() throws {
                        try self.selections[op.token]!.check(contract.basis,
                            scope: self.isDesktop(binding.wire) ? self.state.desktop.scope : binding.wire["surface"] as! String)
                    }
                    @MainActor func markDispatch() throws {
                        try op.check(write: true)
                        if receipt.dispatch == "notDispatched" { try checkSelection() }
                        try RequestDiagnostics.checkCancellation()
                        receipt.dispatch = "unknown"
                        op.recordInputDispatch()
                        self.state.recordEffect()
                    }
                    @MainActor func beforeDispatch() throws {
                        try checkBoundary()
                        try markDispatch()
                    }
                    try checkBoundary()
                    try checkSelection()
                    switch contract.unit {
                    case .desktop(let expected):
                        guard expected == intentKind, !foreground else {
                            throw DriverError("desktop contract intent changed", code: .invalidRequest)
                        }
                        let unit = try self.desktopUnit(bound)
                        if NativePointerIntent.Kind(rawValue: expected) != nil {
                            guard kind == "pointer_sequence" else { throw DriverError("desktop pointer contract mismatch", code: .invalidRequest) }
                            let request = try NativePointerIntent(unit: kind, kind: intentKind, parameters: op.request.parameters, foreground: false)
                            try await unit.deliver(request, check: checkBoundary, onDispatch: markDispatch,
                                onPosted: receipt.recordAccepted, onCleanupUnknown: receipt.recordCleanupUnknown)
                        } else {
                            guard kind == "key_sequence" else { throw DriverError("desktop keyboard contract mismatch", code: .invalidRequest) }
                            let request = try NativeKeyboardIntent(unit: kind, kind: intentKind, parameters: op.request.parameters, foreground: false)
                            try await unit.deliver(request, check: checkBoundary, onDispatch: markDispatch,
                                onPosted: receipt.recordAccepted, onCleanupUnknown: receipt.recordCleanupUnknown)
                        }
                        receipt.detail = intentKind == "scroll"
                            ? "Desktop wheel input dispatched; pages approximate the visible window length (display length if no window); content movement unconfirmed."
                            : "Desktop \(intentKind) dispatched; application effect unconfirmed."
                        receipt.context = ["after": unit.ref, "scope": self.state.desktop.scope, "window": NSNull()]
                    case .launch:
                        throw DriverError("installation contract cannot send control input", code: .invalidRequest)
                    case .action(let name, let description):
                        let request = try NativeObjectIntent(unit: kind, kind: intentKind, parameters: op.request.parameters)
                        guard kind == "native_action",
                              request.kind != "action" || request.value == name || request.value == description,
                              let node = binding.node else {
                            throw DriverError("native action contract mismatch", code: .invalidRequest)
                        }
                        try self.state.applications.executeNativeAction(node, action: name,
                            description: description, onDispatch: beforeDispatch)
                    case .menuAction(let name, let description):
                        let request = try NativeObjectIntent(unit: kind, kind: intentKind, parameters: op.request.parameters)
                        guard ["native_action", "prepare_receiver"].contains(kind),
                              request.kind != "action" || request.value == name || request.value == description,
                              let node = binding.node else {
                            throw DriverError("menu action contract mismatch", code: .invalidRequest)
                        }
                        let unit = try self.menuUnit(bound, within: contract.within)
                        if kind == "prepare_receiver" {
                            if foreground {
                                try op.requireForegroundPreparation(id, hostAllowed: ForegroundAssist.enabled || BackgroundTarget.isExclusive)
                            }
                            try await unit.prepare(foreground: foreground, check: checkBoundary,
                                onPreparation: receipt.recordPreparation, onCleanupUnknown: receipt.recordCleanupUnknown)
                            receipt.detail = "Selected document menu context prepared; no menu command was sent."
                            receipt.confirmation = "confirmed"
                        } else {
                            guard unit.window == nil || name == kAXCancelAction || op.preparationConfirmed(id) else {
                                throw DriverError("document menu validation has not been prepared in this operation", code: .inputUnavailable)
                            }
                            let facts = try self.state.applications.nativeActionFacts(node)
                            guard facts.actions?.filter({ $0.name == name && $0.description == description }).count == 1 else {
                                throw DriverError("the selected menu action declaration changed", code: .inputUnavailable)
                            }
                            try await unit.perform(action: name, opensMenu: intentKind == "openMenu", check: checkBoundary, onDispatch: beforeDispatch)
                            receipt.detail = "Native menu action dispatched in the selected application context; command effect unconfirmed."
                        }
                    case .textValue(let valueContract):
                        let request = try NativeObjectIntent(unit: kind, kind: intentKind, parameters: op.request.parameters)
                        guard kind == "native_text_edit", let text = request.value, let node = binding.node else { throw DriverError("native text contract mismatch", code: .invalidRequest) }
                        let edited = try self.state.applications.executeNativeTextValue(node,
                            contract: valueContract, text: text, onDispatch: beforeDispatch)
                        receipt.detail = edited.detail
                        receipt.confirmation = edited.verified ? "confirmed" : "unavailable"
                    case .receiverContext(let receiverContract):
                        guard kind == "prepare_receiver", ["insert", "replace", "paste"].contains(intentKind) else {
                            throw DriverError("native receiver preparation does not authorize input", code: .invalidRequest)
                        }
                        if intentKind == "paste" {
                            _ = try NativeKeyboardIntent(unit: "clipboard_paste", kind: intentKind,
                                parameters: op.request.parameters, foreground: false)
                        } else {
                            _ = try NativeObjectIntent(unit: "native_text_edit", kind: intentKind, parameters: op.request.parameters)
                        }
                        let selected = try self.selectedTextUnit(bound, actuator: contract.actuator, receiver: contract.receiver)
                        let unit = NativeReceiverPreparation(application: selected.application, window: selected.window,
                            element: selected.element, receiverRelation: selected.receiverRelation)
                        if foreground {
                            try op.requireForegroundPreparation(id, hostAllowed: ForegroundAssist.enabled || BackgroundTarget.isExclusive)
                        }
                        try await unit.prepare(foreground: foreground,
                            check: { try unit.validate(receiverContract, check: checkBoundary) },
                            ready: { try unit.readiness().ready },
                            onPreparation: receipt.recordPreparation)
                        receipt.detail = "Native receiver context confirmed; inspect editing capability before sending text."
                        receipt.confirmation = "confirmed"
                    case .selectedText(let editContract):
                        let request = try NativeObjectIntent(unit: "native_text_edit", kind: intentKind, parameters: op.request.parameters)
                        guard ["prepare_receiver", "native_text_edit"].contains(kind), let text = request.value,
                              editContract.replace == (intentKind == "replace") else {
                            throw DriverError("native selected-text contract mismatch", code: .invalidRequest)
                        }
                        let unit = try self.selectedTextUnit(bound, actuator: contract.actuator, receiver: contract.receiver)
                        if kind == "prepare_receiver" {
                            if foreground {
                                try op.requireForegroundPreparation(id, hostAllowed: ForegroundAssist.enabled || BackgroundTarget.isExclusive)
                            }
                            try await unit.prepare(editContract, foreground: foreground, check: checkBoundary,
                                onPreparation: receipt.recordPreparation)
                            receipt.detail = "Native editor context confirmed; no text was sent."
                            receipt.confirmation = "confirmed"
                        } else {
                            alreadySatisfied = try await unit.deliver(editContract, text: text, check: checkBoundary,
                                // A selection setter can change the composed
                                // control before the separate content setter.
                                // Recheck the chosen semantics at both writes,
                                // even after the operation has recorded effects.
                                onDispatch: {
                                    if receipt.dispatch != "notDispatched" { try checkSelection() }
                                    try markDispatch()
                                }, onPosted: receipt.recordAccepted)
                            receipt.detail = alreadySatisfied
                                ? (intentKind == "replace" ? "The complete target text is already empty; no replacement input was sent."
                                   : "Empty text; no input was sent.")
                                : "Native selected-text edit dispatched; application effect unconfirmed."
                            if alreadySatisfied { receipt.confirmation = "confirmed" }
                        }
                    case .scroll:
                        let scroll = try NativeScrollIntent(unit: kind, kind: intentKind,
                            parameters: op.request.parameters, foreground: foreground)
                        guard let actuator = contract.actuator else {
                            throw DriverError("native scroll contract mismatch", code: .invalidRequest)
                        }
                        let unit = try self.scrollUnit(bound, actuator: actuator)
                        try await unit.perform(scroll, check: checkBoundary, onDispatch: markDispatch,
                                               onAccepted: receipt.recordAccepted)
                        receipt.detail = "\(receipt.acceptedCount) native page actions dispatched; resulting content position unconfirmed."
                    case .keyboard(let expected, let replacement):
                        let keyboard = try NativeKeyboardIntent(unit: kind, kind: intentKind,
                            parameters: op.request.parameters, foreground: foreground)
                        guard keyboard.kind == expected else {
                            throw DriverError("keyboard primitive intent changed", code: .invalidRequest)
                        }
                        let unit = try self.keyboardUnit(bound, actuator: contract.actuator, receiver: contract.receiver)
                        if let element = unit.element, AXAccess.read(element, kAXEnabledAttribute).value as? Bool == false {
                            throw DriverError("the selected keyboard target is disabled", code: .inputUnavailable)
                        }
                        if expected == .replace {
                            guard let element = unit.element, let replacement,
                                  NativeReplacementUnit(element: element).contract() == replacement else {
                                throw DriverError("the selected replacement contract changed", code: .inputUnavailable)
                            }
                        }
                        if kind == "prepare_receiver" {
                            if foreground {
                                try op.requireForegroundPreparation(id, hostAllowed: ForegroundAssist.enabled || BackgroundTarget.isExclusive)
                            }
                            try await unit.prepare(keyboard, replacement: replacement,
                                foreground: foreground, check: checkBoundary,
                                onPreparation: receipt.recordPreparation, onCleanupUnknown: receipt.recordCleanupUnknown)
                            receipt.detail = "Keyboard receiver confirmed; no requested input was sent."
                            receipt.confirmation = "confirmed"
                        } else {
                            alreadySatisfied = try await unit.deliver(keyboard, replacement: replacement, prepared: op.preparationConfirmed(id),
                                check: checkBoundary, onDispatch: markDispatch,
                                onPosted: receipt.recordAccepted, onPreparation: receipt.recordPreparation,
                                onClipboardRestore: receipt.recordClipboardRestore, onCleanupUnknown: receipt.recordCleanupUnknown)
                            receipt.detail = alreadySatisfied
                                ? "The complete target text is already empty; no replacement input was sent."
                                : "\(intentKind) dispatched to the selected receiver; application effect unconfirmed."
                            if alreadySatisfied { receipt.confirmation = "confirmed" }
                        }
                    case .pointer(let expected):
                        let pointer = try NativePointerIntent(unit: kind, kind: intentKind, parameters: op.request.parameters,
                            foreground: foreground, element: !(bound["capture"] is String))
                        guard pointer.kind == expected else {
                            throw DriverError("pointer primitive intent changed", code: .invalidRequest)
                        }
                        let captured = try (bound["capture"] is String)
                            ? NativePointerUnit(capture: self.captures.input(bound).entry) : nil
                        let element = try captured == nil ? self.elementPointerUnit(bound, actuator: contract.actuator) : nil
                        try captured?.validateParameters(pointer)
                        if kind == "prepare_receiver" {
                            if foreground, !BackgroundTarget.isExclusive {
                                try op.requireForegroundPreparation(id, hostAllowed: ForegroundAssist.enabled)
                            }
                            if let captured {
                                try await captured.prepare(foreground: foreground, atNative: captured.point(pointer.start!), kind: pointer.kind,
                                    check: checkBoundary, onPreparation: receipt.recordPreparation,
                                    onCleanupUnknown: receipt.recordCleanupUnknown)
                            } else if let element {
                                try await element.prepare(foreground: foreground, check: checkBoundary,
                                    onPreparation: receipt.recordPreparation,
                                    onContentPreparation: { try op.recordContentPreparation(id) },
                                    onCleanupUnknown: receipt.recordCleanupUnknown)
                            }
                            receipt.detail = "Pointer window route prepared; no requested pointer event sent."
                            receipt.confirmation = "confirmed"
                        } else {
                            if let captured {
                                try await captured.deliver(pointer, check: checkBoundary, onDispatch: markDispatch,
                                    onPosted: receipt.recordAccepted, onCleanupUnknown: receipt.recordCleanupUnknown)
                            } else if let element {
                                try await element.deliver(pointer, check: checkBoundary, onDispatch: markDispatch,
                                    onPosted: receipt.recordAccepted, onCleanupUnknown: receipt.recordCleanupUnknown)
                            }
                            receipt.detail = "\(intentKind) pointer sequence dispatched; application effect unconfirmed."
                        }
                    }
                    if kind == "prepare_receiver" {
                        try checkSelection()
                        try op.confirmPreparation(id)
                    } else if !alreadySatisfied { receipt.dispatch = "dispatched" }
                } catch {
                    receipt.recordFailure(error)
                    if op.contentPreparationStarted {
                        AgentPointer.current?.recordOutcome(success: false)
                        return receipt.wire
                    }
                    if receipt.dispatch == "notDispatched", error is NativeSelectionBasis.Changed {
                        receipt.retryReason = "preconditionChanged"
                    } else if receipt.dispatch == "notDispatched", (error as? ClassifiedFailure)?.code == .inputUnavailable,
                              !(error is NativeReceiverContinuity.Lost) {
                        // A lost bound App receiver cannot be repaired by
                        // re-inspecting and selecting another current control.
                        if kind == "prepare_receiver", !foreground,
                           error is NativePreparationUnavailable {
                            do {
                                try op.backgroundPreparationUnavailable(id)
                                receipt.retryReason = "preparationUnavailable"
                            } catch { /* Expired/cancelled operations cannot authorize activation. */ }
                        } else if kind != "prepare_receiver" {
                            receipt.retryReason = ["key_sequence", "clipboard_paste", "pointer_sequence"].contains(kind) ? "preconditionChanged" : "routeUnavailable"
                        }
                    }
                }
                AgentPointer.current?.recordOutcome(success: receipt.succeeded)
                return receipt.wire
            }
        case "finish":
            let p = try object(raw, keys: ["operation"])
            return ["cleanup": try operation(p["operation"]).finish()]
        default: throw DriverError("unknown native operation command", code: .invalidRequest)
        }
    }

    private func executeLaunch(_ op: NativeOperation, step: Int64, command: NativeUnitCommand) async throws -> [String: Any] {
        let payload = try JSONSerialization.data(withJSONObject: command.value, options: [.sortedKeys, .withoutEscapingSlashes])
        return try await op.execute(step: step, payload: payload) {
            let receipt = NativeExecutionReceipt(target: command.target, detail: "Application launch pending.",
                confirmation: "notRequested", onEffect: self.state.recordEffect)
            do {
                try self.state.sharing.checkAvailable()
                try op.check(write: true)
                guard let contract = self.contracts[op.token]?.first(where: { $0.id == command.primitiveID }),
                      case .launch(let installation) = contract.unit,
                      try self.directory.installation(command.target).identity == installation else {
                    throw DriverError("unknown or changed installation launch contract", code: .staleTarget)
                }
                let launch = try await NativeLaunchUnit(installation: installation, discovery: self.state.discovery).perform(check: {
                    try self.state.sharing.checkAvailable()
                    try op.check(write: true)
                    try RequestDiagnostics.checkCancellation()
                }, onDispatch: {
                    try op.check(write: true)
                    try RequestDiagnostics.checkCancellation()
                    receipt.dispatch = "unknown"; op.recordInputDispatch(); self.state.recordEffect()
                })
                receipt.dispatch = launch.launched ? "dispatched" : "notDispatched"
                receipt.application = launch.application
                receipt.confirmation = "confirmed"
                receipt.detail = launch.launched
                    ? "The selected application started; window and content readiness require an explicit read."
                    : "The selected installation is already running; no launch or activation was sent."
            } catch {
                receipt.recordFailure(error)
            }
            return receipt.wire
        }
    }
}
