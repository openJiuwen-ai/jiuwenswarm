import AppKit
import ApplicationServices

/// Bounded native acquisition only. Semantic composition and display numbering
/// belong to Python; native relations retain their real, separately readable IDs.
@MainActor final class NativeFactCollector {
    typealias Target = ApplicationElements.Target
    typealias Identity = ApplicationElements.Identity
    static let fields = ["AXRole", "AXSubrole", "AXRoleDescription", "AXTitle", "AXDescription", "AXValue",
        "AXIdentifier", "AXHelp", "AXURL", "AXDocument", "AXEnabled", "AXSelected", "AXExpanded", "AXFocused",
        "AXModal", "AXElementBusy", "AXHasPopup", "AXSelectedText", "AXSelectedTextRange", "AXPlaceholderValue", "AXDisclosing", "AXDisclosureLevel", "AXEditable", "AXOrientation",
        "AXRowIndexRange", "AXColumnIndexRange", "AXRowCount", "AXColumnCount", "AXIndex"]
        + writableFields.keys.sorted()
    static let writableFields = ["settable.AXFocused": "AXFocused", "settable.AXValue": "AXValue",
                                 "settable.AXSelectedText": "AXSelectedText"]
    static let relations = ["AXChildren", "AXParent", "AXTitleUIElement", "AXServesAsTitleForUIElements", "AXWindow"]
    // Optional relationships are selected by the caller. Advertised-property
    // checks avoid invoking undeclared getters on every unrelated node.
    static let supportedRelations = relations + ["AXVerticalScrollBar", "AXHorizontalScrollBar", "AXHeader", "AXDisclosedByRow"]
        + AXRelationReader.tableArrays + ["AXOwns", "AXLinkedUIElements", "AXSelectedChildren"]
    private let registry: ApplicationElements
    private let inspection: NativeElementInspection
    private let relationReader = AXRelationReader()
    init(registry: ApplicationElements, inspection: NativeElementInspection) { self.registry = registry; self.inspection = inspection }

    /// Reading an exact retained object does not require its label, enabled
    /// state, input context or a complete provider containment path. The input
    /// endpoint keeps its separate identity/owning-window checks.
    func readTarget(_ ref: String) throws -> Target {
        guard let target = registry.target(ref) else { throw DriverError("native node expired", code: .staleTarget) }
        try Self.validateRetainedTarget(target)
        return target
    }

    static func validateRetainedTarget(_ target: Target) throws {
        guard target.application.launchDate == target.launched else {
            throw DriverError("native node expired", code: .staleTarget)
        }
        try BackgroundTarget(target.application).validate()
        var pid: pid_t = 0
        let status = AXUIElementGetPid(target.element, &pid)
        guard status == .success else {
            throw DriverError("native node provider identity is unavailable",
                              code: status == .invalidUIElement ? .staleTarget : .observationIncomplete)
        }
        if let provider = target.provider {
            guard pid == provider.pid else { throw DriverError("native node provider changed", code: .staleTarget) }
            guard let current = NativeProcessIdentity.read(pid) else {
                throw DriverError("native node provider lifetime is unavailable",
                                  code: NativeProcessIdentity.isGone(pid) ? .staleTarget : .observationIncomplete)
            }
            guard current == provider else { throw DriverError("native node provider lifetime changed", code: .staleTarget) }
        } else if pid != target.application.processIdentifier {
            throw DriverError("native node provider was not confirmed for this scope",
                              code: target.providerKnown ? .staleTarget : .observationIncomplete)
        }
    }

    private func register(_ element: AXUIElement, app: NSRunningApplication, scope: String,
                          parent: Target? = nil) throws -> (String, Target) {
        try RequestDiagnostics.checkCancellation()
        AXMessaging.configure(element)
        let key = Identity(pid: app.processIdentifier, launched: app.launchDate, element: element, scope: scope)
        let previous = registry.observed(key)?.value
        let read = AXIdentity.read(element)
        if case .invalid = read {
            registry.invalidate { $0.scope == scope && CFEqual($0.element, element) }
            throw DriverError("native node no longer exists", code: .staleTarget)
        }
        let identity = ElementIdentity(facts: read.value ?? .init(), roleHint: previous?.role)
        let role = identity.attributes["AXRole"] ?? ""
        let provider = inspection.providerFacts(element, app: app)
        var target = Target(application: app, launched: app.launchDate, element: element, scope: scope,
            role: role, identity: identity, provider: provider.identity, providerKnown: provider.known,
            ancestors: parent.map { $0.ancestors + [$0.element] } ?? [],
            context: parent.map(ApplicationElements.childContext) ?? [])
        if parent == nil, let previous, previous.identity.refining(with: identity) != nil,
           !provider.known || !(previous.providerKnown || previous.provider != nil) || previous.provider == provider.identity {
            // An independent relationship does not provide a new containment
            // path and cannot erase the one already observed.
            target.ancestors = previous.ancestors; target.context = previous.context
        }
        let ref = registry.observe(key, evidence: .init(identity: identity, provider: target.provider,
            context: target.context, providerKnown: target.providerKnown),
            value: target)
        return (ref, registry.target(ref) ?? target)
    }

    func registerRoot(_ element: AXUIElement, app: NSRunningApplication, scope: String) throws -> String {
        try register(element, app: app, scope: scope).0
    }

    private func related(_ element: AXUIElement, app: NSRunningApplication, scope: String,
                         parent: Target? = nil) throws -> (String, Target)? {
        do { return try register(element, app: app, scope: scope, parent: parent) }
        catch let error as DriverError where error.code == .staleTarget { return nil }
    }

    private func stamp(_ began: Int64) -> [String: Any] {
        ["id": UUID().uuidString, "startedAt": began, "completedAt": Int64(Date().timeIntervalSince1970 * 1000)]
    }

    func collect(_ ref: String, session: String, fields requested: [String], maxNodes: Int, maxDepth: Int,
                 childrenPage: NativeInspectQuery.ChildrenPage? = nil, relationPage: NativeInspectQuery.RelationPage? = nil, advertisedFields: [String] = [],
                 followRelations: [String] = []) throws -> [String: Any] {
        let root = try readTarget(ref)
        let began = Int64(Date().timeIntervalSince1970 * 1000)
        let deadline = min((RequestDiagnostics.deadline ?? .infinity) - 0.2, ProcessInfo.processInfo.systemUptime + 5)
        // Settable is a distinct native query, not an AX attribute name. Keep
        // its false/unsupported/error results separate from value reads.
        var queue: [(Target, String, Int)] = [(root, ref, 0)]
        var known = [Identity(pid: root.application.processIdentifier, launched: root.launched, element: root.element, scope: root.scope): ref]
        var queued = Set([ref])
        var nodes = [[String: Any]](), links = [[String: Any]](), reasons = Set<String>()
        var pendingLinks = [AXRelationReader.Page]()
        var pendingValues = [(index: Int, property: String, value: CFTypeRef)]()
        var index = 0
        var previewBytes = 0, capturedBytes = 0, edgeCount = 0
        let followed = Set(["AXChildren"] + followRelations)
        var relations = Self.relations + followRelations.filter { !Self.relations.contains($0) }
        let rootPage = relationPage?.page ?? childrenPage
        let pageProperty = relationPage?.property ?? "AXChildren"
        if rootPage != nil { relations = [pageProperty] + relations.filter { $0 != pageProperty } }
        func reading() throws -> Bool {
            try Task.checkCancellation()
            return ProcessInfo.processInfo.systemUptime < deadline
        }
        func key(_ element: AXUIElement) -> Identity {
            Identity(pid: root.application.processIdentifier, launched: root.launched, element: element, scope: root.scope)
        }
        func encodeField(_ value: CFTypeRef) throws -> (value: Any?, failure: [String: Any]?) {
            guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return AXFactEncoding.scalar(value) }
            let element = unsafeDowncast(value, to: AXUIElement.self), identity = key(element)
            if let ref = known[identity] { return AXFactEncoding.scalar(value, elementReference: ref) }
            guard known.count < 8192 else {
                return (nil, ["state": "error", "detail": "native value reference count limit"])
            }
            guard try reading() else {
                return (nil, ["state": "timeout", "detail": "native value reference collection deadline"])
            }
            guard let (ref, _) = try related(element, app: root.application, scope: root.scope) else {
                return (nil, ["state": "error", "detail": "native value object expired before registration"])
            }
            // This is a value relation, not a child or input-authority edge.
            // Values resolve after traversal so they cannot change which
            // children are expanded or the containment paths they retain.
            known[identity] = ref
            return AXFactEncoding.scalar(value, elementReference: ref)
        }
        while index < queue.count, try reading() {
            let (target, nodeID, depth) = queue[index]; index += 1
            AXMessaging.configure(target.element)
            var values = [String: Any](), missing = [String: Any]()
            var relationFailures = [String: [String: Any]]()
            if !advertisedFields.isEmpty || !followRelations.isEmpty {
                var attributes: CFArray?
                let status = AXUIElementCopyAttributeNames(target.element, &attributes)
                // Writable probes use a different API, but their underlying
                // attribute declaration is still the provider's native name.
                let names = advertisedFields.map { Self.writableFields[$0] ?? $0 }
                let unavailable = AXFactEncoding.advertisedFieldFailures(Array(Set(names)), status: status, names: attributes)
                for name in advertisedFields {
                    if let failure = unavailable[Self.writableFields[name] ?? name] { missing[name] = failure }
                }
                relationFailures = AXFactEncoding.advertisedFieldFailures(followRelations, status: status, names: attributes)
            }
            let keys = requested.filter { Self.writableFields[$0] == nil && missing[$0] == nil }
            var raw: CFArray?
            let canReadFields = try reading()
            let status = keys.isEmpty ? AXError.success : !canReadFields ? .cannotComplete
                : AXUIElementCopyMultipleAttributeValues(target.element, keys as CFArray, [], &raw)
            if !canReadFields { reasons.insert("collection time budget") }
            let attributes = raw as? [Any]
            for (position, name) in keys.enumerated() where requested.contains(name) {
                if status == .success, attributes?.count == keys.count,
                   CFGetTypeID(attributes![position] as CFTypeRef) == AXUIElementGetTypeID() {
                    pendingValues.append((nodes.count, name, attributes![position] as CFTypeRef))
                    continue
                }
                let decoded = status == .success && attributes?.count == keys.count
                    ? AXFactEncoding.scalar(attributes![position] as CFTypeRef) : (value: nil, failure: AXFactEncoding.status(status == .success ? .failure : status))
                if let value = decoded.value { values[name] = value }
                if let failure = decoded.failure { missing[name] = failure }
            }
            for name in requested {
                guard missing[name] == nil else { continue }
                guard let attribute = Self.writableFields[name] else { continue }
                guard try reading() else {
                    missing[name] = ["state": "timeout", "detail": "collection time budget"]
                    reasons.insert("collection time budget")
                    continue
                }
                var writable = DarwinBoolean(false)
                let status = AXUIElementIsAttributeSettable(target.element, attribute as CFString, &writable)
                if status == .success { values[name] = writable.boolValue }
                else { missing[name] = AXFactEncoding.status(status) }
            }
            for (name, value) in values {
                guard let text = value as? String else { continue }
                let available = max(0, 8 * 1024 * 1024 - previewBytes)
                if text.utf16.count > 16_384 || text.utf8.count > available {
                    // This immutable text token is separate from the live node.
                    let preview = String(text.unicodeScalars.prefix(min(4096, available / 4)))
                    values[name] = preview; previewBytes += preview.utf8.count
                    var limited: [String: Any] = ["state": "truncated", "totalUnits": text.utf16.count]
                    if text.utf8.count <= 8 * 1024 * 1024 - capturedBytes {
                        let identity = key(target.element)
                        let life = registry.observe(identity, evidence: .init(identity: target.identity, provider: target.provider,
                            context: target.context, providerKnown: target.providerKnown),
                            value: target)
                        if let textRef = registry.bind(life, text: text), registry.text(textRef) != nil {
                            limited["textRef"] = textRef; capturedBytes += text.utf8.count
                        }
                    }
                    if limited["textRef"] == nil { limited["detail"] = "immutable text capture budget exhausted; preview only" }
                    missing[name] = limited
                } else { previewBytes += text.utf8.count }
            }
            var actionNames: CFArray?
            let canReadActions = try reading()
            let actionStatus = canReadActions ? AXUIElementCopyActionNames(target.element, &actionNames) : .cannotComplete
            var actionFacts = AXFactEncoding.actions(actionStatus, actionNames)
            if !canReadActions { actionFacts["detail"] = "collection time budget" }
            var actions = actionFacts["items"] as! [[String: Any]]
            for index in actions.indices {
                let name = actions[index]["name"] as! String
                if try reading() {
                    var description: CFString?
                    let status = AXUIElementCopyActionDescription(target.element, name as CFString, &description)
                    actions[index] = ["name": name].merging(AXFactEncoding.description(status, description)) { _, new in new }
                }
            }
            actionFacts["items"] = actions
            nodes.append(["node": nodeID, "requestedFields": requested, "values": values, "fieldStatus": missing, "actions": actionFacts])

            // The caller chooses which raw edges to follow. Collecting an edge
            // does not reinterpret it as parentage or input ownership.
            for property in relations {
                if let failure = relationFailures[property] {
                    var link: [String: Any] = ["source": nodeID, "property": property,
                        "targets": [String](), "certainty": "unknown"]
                    link.merge(failure) { _, value in value }
                    links.append(link)
                    continue
                }
                guard try reading() else {
                    links.append(AXFactEncoding.relationFailure(source: nodeID, property: property))
                    reasons.insert("collection time budget")
                    continue
                }
                let page = nodeID == ref && property == pageProperty ? rootPage : nil
                let shouldFollow = followed.contains(property) || page != nil
                var limit = shouldFollow
                    ? (depth < maxDepth ? min(maxNodes - queue.count, 8192 - known.count, 32768 - edgeCount) : 0)
                    : min(128, 32768 - edgeCount)
                if let page { limit = min(limit, page.limit) }
                let relation: AXRelationReader.Result
                do {
                    relation = try relationReader.read(target.element, source: nodeID, property: property, limit: limit, cursor: page?.cursor,
                                                       withinBudget: reading)
                } catch let error as DriverError where error.code == .snapshotExpired {
                    // A changing branch is a local observation gap, not loss of
                    // the fields and unrelated branches already collected.
                    var failure = AXFactEncoding.relationFailure(source: nodeID, property: property, error: .failure)
                    failure["detail"] = error.localizedDescription
                    links.append(failure); reasons.insert("native relation changed during collection")
                    continue
                }
                switch relation {
                case .failure(let value): links.append(value)
                case .page(let page):
                    if !shouldFollow { pendingLinks.append(page); continue }
                    var targets = [String](), expired = false
                    for element in page.targets {
                        guard try reading() else { break }
                        let nativeKey = key(element)
                        if let previous = known[nativeKey] { targets.append(previous); continue }
                        guard let (life, child) = try related(element, app: root.application, scope: root.scope,
                            parent: property == "AXChildren" ? target : nil) else {
                            expired = true; break
                        }
                        known[nativeKey] = life; targets.append(life)
                        if queued.insert(life).inserted { queue.append((child, life, depth + 1)) }
                    }
                    edgeCount += targets.count
                    var link = relationReader.encode(page, targets: targets)
                    if expired { link["detail"] = "a related native object expired before registration; read the relation source again" }
                    if link["state"] as? String != "complete" { reasons.insert("partial native relation: " + property) }
                    links.append(link)
                }
            }
        }
        if index < queue.count { reasons.insert("collection time budget") }
        let collected = Set(nodes.compactMap { $0["node"] as? String })
        for page in pendingLinks {
            var targets = [String](), expired = false
            for element in page.targets {
                guard edgeCount + targets.count < 32768 else { break }
                if let previous = known[key(element)] { targets.append(previous); continue }
                guard known.count < 8192, try reading() else { break }
                // A native related object remains inspectable even when it is
                // not a child. This does not authorize it as an input recipient.
                guard let (life, _) = try related(element, app: root.application, scope: root.scope) else { expired = true; break }
                known[key(element)] = life; targets.append(life)
            }
            edgeCount += targets.count
            var link = relationReader.encode(page, targets: targets)
            if expired { link["detail"] = "a related native object expired before registration; read the relation source again" }
            links.append(link)
        }
        for item in pendingValues {
            let encoded = try encodeField(item.value)
            var values = nodes[item.index]["values"] as! [String: Any]
            var missing = nodes[item.index]["fieldStatus"] as! [String: Any]
            if let value = encoded.value { values[item.property] = value }
            if let failure = encoded.failure { missing[item.property] = failure }
            nodes[item.index]["values"] = values; nodes[item.index]["fieldStatus"] = missing
        }
        relationReader.retainAvailableCursors(&links)
        guard !nodes.isEmpty else { throw RequestDeadlineExceeded() }
        return ["kind": "nodes", "session": session, "surface": root.scope, "platform": "macos",
            "observed": stamp(began), "roots": [ref], "nodes": nodes,
            "requestedRelations": relations, "relations": links,
            "unread": known.values.filter { !collected.contains($0) }.sorted(), "stopReasons": reasons.sorted()]
    }

    func relation(_ ref: String, session: String, property: String, cursor: String?, limit: Int) throws -> [String: Any] {
        let root = try readTarget(ref)
        let began = Int64(Date().timeIntervalSince1970 * 1000)
        let deadline = min((RequestDiagnostics.deadline ?? .infinity) - 0.2, ProcessInfo.processInfo.systemUptime + 5)
        AXMessaging.configure(root.element)
        let result = try relationReader.read(root.element, source: ref, property: property, limit: limit, cursor: cursor,
            withinBudget: {
                try Task.checkCancellation()
                return ProcessInfo.processInfo.systemUptime < deadline
            })
        var link: [String: Any]
        switch result {
        case .failure(let failure): link = failure
        case .page(let page):
            var targets = [String](), expired = false
            for element in page.targets {
                try RequestDiagnostics.checkCancellation()
                guard ProcessInfo.processInfo.systemUptime < deadline else { break }
                if CFEqual(element, root.element) { targets.append(ref); continue }
                guard let (life, _) = try related(element, app: root.application, scope: root.scope,
                    parent: property == "AXChildren" ? root : nil) else { expired = true; break }
                targets.append(life)
            }
            link = relationReader.encode(page, targets: targets)
            if expired { link["detail"] = "a related native object expired before registration; read the relation source again" }
        }
        return ["kind": "relation", "session": session, "surface": root.scope, "platform": "macos",
                "observed": stamp(began), "relation": link]
    }
}
