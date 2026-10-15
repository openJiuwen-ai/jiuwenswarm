import Foundation

@main struct NativeOperationTests {
    static func intent(_ kind: String, parameters: [String: Any] = [:]) throws -> NativeIntent {
        try NativeIntent(["kind": kind, "parameters": parameters], mode: kind == "read" ? "read" : "write")
    }
    @MainActor static func main() async throws {
        var parameters: [String: Any] = ["key": "super+p"]
        let original = try intent("key", parameters: parameters)
        let bound = NativeOperation(timeoutMs: 10_000, writable: true, intent: original,
            target: [:], release: {}, cancelActive: {})
        var closed = [Bool]()
        bound.onClose = { closed.append($0) }
        parameters["key"] = "super+q"
        precondition(bound.request.kind == "key" && bound.request.parameters["key"] as? String == "super+p")
        precondition(bound.finish() == "complete")
        precondition(bound.finish() == "complete" && closed == [true])
        var released = 0, cancelled = 0, calls = 0
        let op = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("action"), target: [:],
            release: { released += 1 }, cancelActive: { cancelled += 1 })
        let payload = Data("action-one".utf8)
        let result = try await op.execute(step: 1, payload: payload) {
            calls += 1
            return ["dispatch": "dispatched", "cleanup": "complete"]
        }
        precondition(result["dispatch"] as? String == "dispatched" && calls == 1 && released == 0)
        let replay = try await op.execute(step: 1, payload: payload) { calls += 100; return [:] }
        precondition(replay["dispatch"] as? String == "dispatched" && calls == 1)
        do { _ = try await op.execute(step: 1, payload: Data("changed".utf8)) { calls += 100; return [:] }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        for _ in 0..<2 {
            do { _ = try await op.execute(step: 2, payload: payload) { calls += 100; return [:] }; preconditionFailure() }
            catch {
                precondition((error as? DriverError)?.code == .executionUnknown)
                precondition((error as? DriverError)?.effect == .possible)
            }
        }
        precondition(op.finish() == "complete" && released == 1 && cancelled == 0)
        precondition(op.finish() == "complete" && released == 1)
        _ = try await op.execute(step: 1, payload: payload) { calls += 100; return [:] }
        for _ in 0..<2 {
            do { _ = try await op.execute(step: 3, payload: payload) { calls += 100; return [:] }; preconditionFailure() }
            catch { precondition((error as? DriverError)?.code == .executionFailed) }
        }
        precondition(calls == 1)

        // Idle time between inspect and execute consumes the same operation budget.
        var clock: TimeInterval = 1
        let expired = NativeOperation(timeoutMs: 100, writable: true, intent: try intent("action"), target: [:],
            now: { clock }, release: { released += 1 }, cancelActive: { cancelled += 1 })
        clock = 2
        do { try expired.check(); preconditionFailure() } catch {}
        precondition(expired.state == .closed && released == 2)

        let read = NativeOperation(timeoutMs: 10_000, writable: false, intent: try intent("read"), target: [:], release: {}, cancelActive: {})
        do { try read.confirmPreparation("menu"); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        do { _ = try await read.execute(step: 1, payload: payload) { calls += 100; return [:] }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        precondition(read.finish() == "complete" && calls == 1)

        // Stop during a suspended unit retains the lease until cleanup returns.
        let active = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("action"), target: [:],
            release: { released += 1 }, cancelActive: { cancelled += 1 })
        _ = try await active.execute(step: 1, payload: payload) {
            active.stop("cancelled")
            precondition(active.state == .stopping && active.busy && released == 2 && cancelled == 1)
            await Task.yield()
            precondition(released == 2)
            return ["dispatch": "partial", "cleanup": "complete"]
        }
        precondition(active.state == .closed && released == 3)

        let uncertain = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("action"), target: [:],
            release: { released += 1 }, cancelActive: {})
        _ = try await uncertain.execute(step: 1, payload: payload) { ["dispatch": "unknown", "cleanup": "unknown"] }
        precondition(uncertain.finish() == "unknown" && released == 3)

        let prepared = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("action"), target: [:], release: {}, cancelActive: {})
        precondition(!prepared.preparationConfirmed("menu"))
        try prepared.confirmPreparation("menu")
        precondition(prepared.preparationConfirmed("menu") && !prepared.preparationConfirmed("other-menu"))
        _ = try await prepared.execute(step: 1, payload: payload) { ["dispatch": "notDispatched", "cleanup": "complete"] }
        _ = try await prepared.execute(step: 2, payload: payload) {
            prepared.recordInputDispatch()
            return ["dispatch": "unknown", "cleanup": "complete"]
        }
        do { _ = try await prepared.execute(step: 3, payload: payload) { preconditionFailure() }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .executionUnknown) }
        do { try prepared.confirmPreparation("other-menu"); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .executionUnknown) }
        precondition(prepared.finish() == "complete")
        do { try prepared.confirmPreparation("menu"); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .executionFailed) }

        let keyboard = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("key"), target: [:], release: {}, cancelActive: {})
        do { try keyboard.requireForegroundPreparation("key", hostAllowed: true); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        try keyboard.backgroundPreparationUnavailable("key")
        do { try keyboard.requireForegroundPreparation("key", hostAllowed: false); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .modeUnsupported) }
        do { try keyboard.requireForegroundPreparation("other", hostAllowed: true); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        try keyboard.requireForegroundPreparation("key", hostAllowed: true)
        keyboard.recordInputDispatch()
        do { try keyboard.requireForegroundPreparation("key", hostAllowed: true); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        do { try keyboard.backgroundPreparationUnavailable("key"); preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .executionUnknown) }
        precondition(keyboard.finish() == "complete")

        let dishonest = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("action"), target: [:], release: { released += 1 }, cancelActive: {})
        do {
            _ = try await dishonest.execute(step: 1, payload: payload) {
                dishonest.recordInputDispatch()
                return ["dispatch": "notDispatched", "cleanup": "complete"]
            }
            preconditionFailure()
        } catch { precondition((error as? DriverError)?.code == .executionUnknown) }
        precondition(dishonest.finish() == "unknown" && released == 3)

        // Expiry/cancellation during a screenshot await must retain the read
        // scope until the OS call returns, and never publish late pixels.
        let capture = NativeOperation(timeoutMs: 10_000, writable: false, intent: try intent("read"), target: [:],
            release: { released += 1 }, cancelActive: { cancelled += 1 })
        capture.onClose = { closed.append($0) }
        do {
            _ = try await capture.observe {
                capture.stop("cancelled")
                precondition(capture.state == .stopping && capture.busy && released == 3)
                precondition(closed == [true], "presentation must stay active until the native await unwinds")
                await Task.yield()
                precondition(released == 3)
                return "late pixels"
            }
            preconditionFailure()
        } catch { precondition((error as? DriverError)?.code == .executionFailed) }
        precondition(capture.state == .closed && released == 4 && !capture.inputStarted)
        precondition(closed == [true, false])

        // A fixed window lease can discover a node, but can never select a
        // second node after the first input target has been bound.
        let window: [String: Any] = ["session": "s", "instance": "i", "rootWindow": "r", "surface": "w", "node": NSNull(), "capture": NSNull()]
        var first = window; first["node"] = "first"
        var second = window; second["node"] = "second"
        let scoped = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("click"), target: window, release: {}, cancelActive: {})
        precondition(scoped.permits(first) && scoped.permits(second))
        for field in ["session", "instance", "rootWindow", "surface", "capture"] {
            var changed = first; changed[field] = "other"
            precondition(!scoped.permits(changed))
        }
        var extra = first; extra["skipValidation"] = true
        precondition(!scoped.permits(extra))
        var pins = 0, unpins = 0
        try scoped.bindInputTarget(first, within: ["root", "group"]) { pins += 1; return { unpins += 1 } }
        try scoped.bindInputTarget(first, within: ["root", "group"]) { pins += 1; return { unpins += 1 } }
        do { try scoped.bindInputTarget(second) { pins += 1; return {} }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .staleTarget) }
        do { try scoped.bindInputTarget(first, within: ["root"]) { pins += 1; return {} }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .staleTarget) }
        precondition(pins == 1 && unpins == 0)
        precondition(scoped.finish() == "complete" && unpins == 1)
        precondition(scoped.finish() == "complete" && unpins == 1)

        let addressed = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("click"), target: first, release: {}, cancelActive: {})
        precondition(addressed.permits(first) && !addressed.permits(second) && !addressed.permits(window))
        precondition(addressed.permitsObservation(second) && !addressed.permitsObservation(window))
        for field in ["session", "instance", "rootWindow", "surface", "capture"] {
            var changed = second; changed[field] = "other"
            precondition(!addressed.permitsObservation(changed))
        }
        precondition(!addressed.permitsObservation(extra))
        do { try addressed.bindInputTarget(second) { pins += 1; return {} }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .staleTarget) }
        _ = addressed.finish()
        let readonly = NativeOperation(timeoutMs: 10_000, writable: false, intent: try intent("read"), target: window, release: {}, cancelActive: {})
        precondition(readonly.permits(first))
        do { try readonly.bindInputTarget(first) { pins += 1; return {} }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        _ = readonly.finish()
        var discovery = window
        for field in ["instance", "rootWindow", "surface"] { discovery[field] = NSNull() }
        let directory = NativeOperation(timeoutMs: 10_000, writable: false, intent: try intent("read"), target: discovery, release: {}, cancelActive: {})
        precondition(!directory.permits(first) && !directory.permits(window))
        _ = directory.finish()

        // An application UI root can be read without inventing a window. It
        // still cannot cross session, application or surface boundaries.
        var menuScope = window; menuScope["rootWindow"] = NSNull(); menuScope["surface"] = "menu"
        let menuRead = NativeOperation(timeoutMs: 10_000, writable: false, intent: try intent("read"),
            target: menuScope, release: {}, cancelActive: {})
        var menuItem = menuScope; menuItem["node"] = "command"
        precondition(menuRead.permitsObservation(menuItem) && menuRead.permits(menuItem))
        for field in ["session", "instance", "rootWindow", "surface", "capture"] {
            var changed = menuItem; changed[field] = "other"
            precondition(!menuRead.permitsObservation(changed) && !menuRead.permits(changed))
        }
        do { try menuRead.bindInputTarget(menuItem) { pins += 1; return {} }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
        _ = menuRead.finish()

        let incomplete = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("click"), target: window, release: {}, cancelActive: {})
        try incomplete.bindInputTarget(first) { pins += 1; return { unpins += 1 } }
        _ = try await incomplete.execute(step: 1, payload: payload) { ["dispatch": "unknown", "cleanup": "unknown"] }
        precondition(incomplete.finish() == "unknown" && unpins == 1)

        // A chosen editor may be replaced before dispatch. All selected native
        // refs stay pinned, without granting authority to change the owner.
        var editorPins = 0, editorUnpins = 0
        let editors = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("insert"), target: first, release: {}, cancelActive: {})
        for ref in ["old-editor", "old-editor", "new-editor"] {
            try editors.retainInputReference(ref) { editorPins += 1; return { editorUnpins += 1 } }
        }
        precondition(editorPins == 2 && editorUnpins == 0 && !editors.permits(second))
        do { try editors.retainInputReference("expired") { nil }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .staleTarget) }
        for n in 0..<14 { try editors.retainInputReference("editor-\(n)") { editorPins += 1; return { editorUnpins += 1 } } }
        do { try editors.retainInputReference("over-budget") { preconditionFailure() }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .inputUnavailable) }
        editors.recordInputDispatch()
        do { try editors.retainInputReference("new-editor") { preconditionFailure() }; preconditionFailure() }
        catch { precondition((error as? DriverError)?.code == .executionUnknown) }
        precondition(editors.finish() == "complete" && editorUnpins == 16)
        precondition(editors.finish() == "complete" && editorUnpins == 16)
        let uncertainEditor = NativeOperation(timeoutMs: 10_000, writable: true, intent: try intent("insert"), target: first, release: {}, cancelActive: {})
        try uncertainEditor.retainInputReference("editor") { editorPins += 1; return { editorUnpins += 1 } }
        _ = try await uncertainEditor.execute(step: 1, payload: payload) { ["dispatch": "unknown", "cleanup": "unknown"] }
        precondition(uncertainEditor.finish() == "unknown" && editorUnpins == 16)
        print("PASS native operation: cross-RPC lease, step dedup/conflict, expired/read rejection, stop cleanup, unknown quarantine")
    }
}
