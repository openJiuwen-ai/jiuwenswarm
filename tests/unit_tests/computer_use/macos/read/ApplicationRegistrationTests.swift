import AppKit
import ApplicationServices

// Exercise the real target validator with only the AppKit registry unavailable.
// The kernel identity is always read through the production syscall path.
@main struct ApplicationRegistrationTests {
    @MainActor static func main() async throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let running = NSRunningApplication.current
        try verifyElementEvidence(running)
        try await verifyCapturedTextScope()
        precondition(NSRunningApplication(processIdentifier: running.processIdentifier)?.isEqual(running) == true)
        let target = BackgroundTarget(running)
        try target.validate(applicationForPID: { _ in nil })
        print("PASS a missing registry entry retains an exactly matching, previously registered kernel lifetime")

        let unconfirmed = BackgroundTarget(running, applicationForPID: { _ in nil })
        do {
            try unconfirmed.validate(applicationForPID: { _ in nil })
            preconditionFailure("an unregistered capture must not gain a kernel-only binding")
        } catch let error as DriverError { precondition(error.code == .staleTarget) }
        try unconfirmed.validate()
        print("PASS unknown registration is refused until a current matching application can be verified")

        // The same missing registration is pending during startup, not a stale
        // process. This does not grant normal validation or input permission.
        let ready = try unconfirmed.launchRegistrationReady(applicationForPID: { _ in nil })
        precondition(!ready)
        var polls = 0, waits = 0
        try await ApplicationDiscovery.waitUntilReady(unconfirmed, isFinishedLaunching: { true },
            applicationForPID: { _ in polls += 1; return polls < 3 ? nil : running },
            attempts: 4, pause: { waits += 1 })
        precondition(polls == 3 && waits == 2)
        print("PASS delayed registration is awaited within the same exact process lifetime")

        polls = 0; waits = 0
        try await ApplicationDiscovery.waitUntilReady(target, isFinishedLaunching: { polls >= 3 },
            applicationForPID: { _ in polls += 1; return running },
            attempts: 4, pause: { waits += 1 })
        precondition(polls == 3 && waits == 2)
        print("PASS registration alone does not bypass unfinished application initialization")

        polls = 0; waits = 0
        do {
            try await ApplicationDiscovery.waitUntilReady(unconfirmed, isFinishedLaunching: { true },
                applicationForPID: { _ in polls += 1; return nil },
                attempts: 3, pause: { waits += 1 })
            preconditionFailure("an application that never registers must not become ready")
        } catch let error as DriverError {
            precondition(error.code == .executionFailed && error.description.contains("startup is not ready"))
        }
        precondition(polls == 3 && waits == 2)
        do {
            try unconfirmed.validate(applicationForPID: { _ in nil })
            preconditionFailure("startup polling must not authorize an unregistered input target")
        } catch let error as DriverError { precondition(error.code == .staleTarget) }
        print("PASS missing registration stays bounded and never grants input permission")

        let different = NSWorkspace.shared.runningApplications.first { $0.processIdentifier != running.processIdentifier }!
        do {
            try target.validate(applicationForPID: { _ in different })
            preconditionFailure("conflicting registered identity must not be overridden by kernel evidence")
        } catch let error as DriverError { precondition(error.code == .staleTarget) }
        print("PASS a conflicting registry identity is still refused")

        waits = 0
        do {
            try await ApplicationDiscovery.waitUntilReady(unconfirmed, isFinishedLaunching: { true },
                applicationForPID: { _ in different }, attempts: 3, pause: { waits += 1 })
            preconditionFailure("startup must not wait through a conflicting identity")
        } catch let error as DriverError { precondition(error.code == .staleTarget) }
        precondition(waits == 0)
        let cancelled = Task { @MainActor in
            try await ApplicationDiscovery.waitUntilReady(unconfirmed, isFinishedLaunching: { true },
                applicationForPID: { _ in nil }, attempts: 3, pause: { throw CancellationError() })
        }
        do { try await cancelled.value; preconditionFailure("cancelled startup must not continue") }
        catch is CancellationError { }
        print("PASS conflicting identity and cancellation stop startup without a retry")
    }

    // The real registry holds opaque AX references here. Evidence is supplied
    // explicitly to exercise reconciliation without asking any application to
    // change its UI or pretending this is native input acceptance.
    @MainActor static func verifyElementEvidence(_ running: NSRunningApplication) throws {
        let registry = ApplicationElements()
        let object = AXUIElementCreateApplication(running.processIdentifier)
        let document = AXUIElementCreateSystemWide()
        let key = ApplicationElements.Identity(pid: running.processIdentifier, launched: running.launchDate,
                                              element: object, scope: "working-window")
        let documentKey = ApplicationElements.Identity(pid: running.processIdentifier, launched: running.launchDate, element: document)
        func make(_ values: [String: String], absent: Set<String> = [], documentValues: [String: String] = [:],
                  knownProvider: Bool = false, provider: NativeProcessIdentity? = nil) -> ApplicationElements.Target {
            ApplicationElements.Target(application: running, launched: running.launchDate, element: object,
                scope: "working-window", role: values["AXRole"] ?? "",
                identity: ElementIdentity(facts: .init(values: values, absent: absent), roleHint: "AXButton"),
                provider: provider, providerKnown: knownProvider,
                context: [.init(native: documentKey, identity: .init(facts: .init(values: documentValues)))])
        }
        func observe(_ target: ApplicationElements.Target) -> String {
            registry.observe(key, evidence: .init(identity: target.identity, provider: target.provider,
                context: target.context), value: target)
        }
        let full = ["AXRole": "AXButton", "AXIdentifier": "item", "AXTitle": "Original command"]
        let life = observe(make(full, absent: ["AXSubrole"],
            documentValues: ["AXRole": "AXWindow", "AXDocument": "file:///one"], knownProvider: true))
        let alias = registry.bind(life, text: "immutable text")!
        let unicodeAlias = registry.bind(life, text: "A🙂e\u{301}\r\nZ")!
        let missingValue = registry.bind(life, text: nil)!
        let partial = make(["AXRole": "AXButton"], documentValues: ["AXRole": "AXWindow"])
        precondition(observe(partial) == life)
        let retained = registry.target(alias)!
        precondition(retained.identity.attributes == full && retained.providerKnown)
        precondition(retained.context[0].identity.attributes["AXDocument"] == "file:///one")
        precondition(retained.identity.check(.init(values: ["AXRole": "AXButton"])) == .unavailable,
                     "preserved history is not proof of current input readiness")
        // Previously absent optional evidence can become available. It then
        // constrains this lifetime; later unknown reads cannot erase it.
        precondition(observe(make(["AXRole": "AXButton", "AXSubrole": "newly exposed"])) == life)
        precondition(observe(partial) == life)
        precondition(registry.target(alias)!.identity.attributes["AXSubrole"] == "newly exposed")
        let replaced = observe(make(["AXRole": "AXButton", "AXSubrole": "different control"]))
        precondition(replaced != life && registry.target(alias) == nil)
        precondition(registry.text(alias)?.value == "immutable text")
        let reader = NativeApplicationReader(registry: registry, inspection: NativeElementInspection(registry: registry))
        let original = try reader.readText(alias, offset: 0, length: 100)
        precondition(original["text"] as? String == "immutable text")
        let first = try reader.readText(unicodeAlias, offset: 0, length: 2)
        precondition(first["text"] as? String == "A" && first["next"] as? Int == 1 && first["total"] as? Int == 8)
        let emoji = try reader.readText(unicodeAlias, offset: 1, length: 2)
        precondition(emoji["text"] as? String == "🙂" && emoji["next"] as? Int == 3)
        let rest = try reader.readText(unicodeAlias, offset: 3, length: 10)
        precondition(rest["text"] as? String == "e\u{301}\r\nZ" && rest["next"] is NSNull)
        let end = try reader.readText(unicodeAlias, offset: 8, length: 2)
        precondition(end["text"] as? String == "" && end["next"] is NSNull)
        for offset in [2, 9] {
            do {
                _ = try reader.readText(unicodeAlias, offset: offset, length: 2)
                preconditionFailure("invalid UTF-16 page boundary was accepted")
            } catch let error as DriverError { precondition(error.code == .invalidRequest) }
        }
        do {
            _ = try reader.readText(missingValue, offset: 0, length: 2)
            preconditionFailure("an uncaptured value must not become empty text")
        } catch let error as DriverError { precondition(error.code != .snapshotExpired) }
        print("PASS immutable UTF-16 pages outlive retired native targets; missing values stay unavailable")
        precondition(observe(make(full)) != life, "an old action alias cannot revive")
        let current = observe(make(full, knownProvider: true))
        precondition(observe(make(["AXRole": "AXButton"], absent: ["AXIdentifier"])) != current,
                     "explicit disappearance differs from a failed attribute query")
        let known = observe(make(full, knownProvider: true))
        precondition(observe(make(full, provider: NativeProcessIdentity.read(running.processIdentifier)!)) != known,
                     "known provider changes cannot be hidden by unknown-as-null")
        let contextual = observe(make(full, documentValues: ["AXRole": "AXWindow", "AXDocument": "file:///one"]))
        precondition(observe(make(full, documentValues: ["AXRole": "AXWindow", "AXDocument": "file:///two"])) != contextual)
        let logical = ["AXRole": "AXTextField", "AXRowIndexRange": "1:1", "AXColumnIndexRange": "7:1"]
        let cellLife = observe(make(logical))
        let cellAlias = registry.bind(cellLife, text: "old cell value")!
        let pin = registry.retain(cellAlias)!
        precondition(observe(make(["AXRole": "AXTextField"])) == cellLife)
        precondition(registry.target(cellAlias)!.identity.attributes == logical)
        let recycled = observe(make(logical.merging(["AXRowIndexRange": "21:1"]) { _, new in new }))
        precondition(recycled != cellLife && registry.target(cellAlias) == nil,
                     "retaining the native object cannot retain its old logical item after recycling")
        precondition(registry.text(cellAlias)?.value == "old cell value")
        pin()
        precondition(observe(make(logical)) != cellLife, "returning to an old position does not revive a stale reference")
        let unlabelledCell = make(["AXRole": "AXCell", "AXRowIndexRange": "1:1"])
        let path = ApplicationElements.childContext(unlabelledCell)
        precondition(path.count == unlabelledCell.context.count + 1 && path.last?.identity == unlabelledCell.identity,
                     "a positional item boundary must survive when only its child is requested")
        print("PASS real element registry: unknown/refined evidence, metadata updates, explicit conflicts and immutable captured text")
        print("PASS retained cell references expire on reuse and positional ancestors remain in child input scope")
    }

    @MainActor static func verifyCapturedTextScope() async throws {
        // The production endpoint must reach the immutable store without an
        // accessible application, window, keyboard receiver or screen capture.
        let session = "captured-text-probe"
        let state = NativeDesktopState()
        let endpoint = NativeExecution(state: state, session: session, cancelActive: {})
        let scope: [String: Any] = ["session": session, "instance": NSNull(), "rootWindow": NSNull(),
                                   "surface": NSNull(), "node": NSNull(), "capture": NSNull()]
        let opened = try await endpoint.perform("begin", ["mode": "read", "target": scope,
            "intent": ["kind": "read", "parameters": [:]]], timeoutMs: 1000)
        do {
            _ = try await endpoint.perform("inspect", ["operation": opened["operation"]!, "target": scope,
                "query": ["kind": "capturedText", "textRef": "not-retained", "offset": 0, "length": 2]], timeoutMs: 1000)
            preconditionFailure("an expired capture must not be reconstructed from live UI")
        } catch let error as DriverError { precondition(error.code == .snapshotExpired) }
        let closed = try await endpoint.perform("finish", ["operation": opened["operation"]!], timeoutMs: 1000)
        precondition(closed["cleanup"] as? String == "complete" && !endpoint.hasUnfinishedOperation)
        print("PASS capturedText uses a session read operation with no live UI dependency")
    }
}
