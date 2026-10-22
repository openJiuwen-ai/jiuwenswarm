import AppKit
import ApplicationServices

/// One fixed receiver and one process-addressed keyboard route. This unit never
/// searches for an editor, clicks content, or chooses foreground fallback.
/// Python selects preparation; every dispatch samples the receiver again.
@MainActor struct NativeKeyboardUnit {
    let application: NSRunningApplication
    let window: AXUIElement
    let element: AXUIElement?
    var receiverRelation: NativeReceiverRelation? = nil

    typealias State = NativeReceiverSnapshot

    private var background: BackgroundTarget { BackgroundTarget(application) }

    private func checkEnabled() throws {
        if let element, AXAccess.read(element, kAXEnabledAttribute).value as? Bool == false {
            throw DriverError("the selected keyboard target is disabled", code: .inputUnavailable)
        }
    }

    /// Keyboard routing is independent of PNGs and screenshot dimensions. A
    /// native ID identifies this exact window; bounds only fill the SPI record.
    func mapping() throws -> WindowGeometry.WindowMapping {
        try NativeWindowUnit(application: application, window: window).mapping()
    }

    func sample() throws -> State {
        let context = try NativeReceiverSnapshot.read(application: application, window: window, receiverRelation: receiverRelation)
        let deliveryReady = (try? mapping()).map { BackgroundEvents.keyboardPreparationIsCurrent(background, window: $0) } ?? false
        return State(windowRoute: context.windowRoute == "yes" && !deliveryReady ? "no" : context.windowRoute,
                     focus: context.focus, ancestors: context.ancestors, foreground: context.foreground,
                     relatedEditor: context.relatedEditor, window: context.window)
    }

    func readiness(kind: NativeKeyboardIntent.Kind, key: String? = nil, replacement: NativeReplacementUnit.Selection? = nil,
                   prepared: Bool = false, state: State? = nil) throws -> NativeReadiness {
        let state = try state ?? sample()
        let receiver = state.receiver(element)
        let needsWindow = NativeKeyboardIntent.usesWindowCommand(kind, key: key, replacementShortcut: replacement == .shortcut)
            && !state.foreground && !prepared
        var conditions = [("receiver", receiver == "yes" && needsWindow ? "unknown" : receiver)]
        // A preparation receipt records this operation's native window setup,
        // not application consumption. Live receiver checks still apply at
        // every dispatch; it never authorizes activation or replay afterwards.
        if kind == .replace {
            let selection = element.flatMap { NativeReplacementUnit(element: $0).snapshot() }
            conditions.append(("selection", selection == nil ? "unknown" : "yes"))
        }
        return NativeReadiness(conditions)
    }

    func prepare(_ request: NativeKeyboardIntent, replacement: NativeReplacementUnit.Selection? = nil,
                 foreground: Bool, check: @escaping @MainActor () throws -> Void,
                 onPreparation: @escaping @MainActor (String) -> Void,
                 onCleanupUnknown: @escaping @MainActor () -> Void) async throws {
        try check()
        try checkEnabled()
        try BackgroundTarget.validateReleasedInput()
        if try readiness(kind: request.kind, key: request.key, replacement: replacement).ready { return }
        let mapping = try mapping()
        if foreground {
            // Admission (host opt-in + prior background failure) is checked by
            // the operation endpoint. This helper executes only that choice.
            let assist = ForegroundAssist()
            try await assist.prepare(background, window: mapping.id,
                onAction: { onPreparation("foreground window activation") })
        } else {
            do {
                try await BackgroundEvents.prepare(background, window: mapping, requireKeyboardFocus: false,
                    onAction: { onPreparation("background window preparation") }, beforeAction: check)
            } catch let error as DriverError where error.code == .inputUnavailable {
                // An unavailable boundary, disabled control, held user input,
                // cancellation or expired operation is not fallback permission.
                try check(); try checkEnabled(); try BackgroundTarget.validateReleasedInput()
                throw NativePreparationUnavailable(description: error.description)
            }
        }
        try check()
        if request.usesWindowCommand(replacementShortcut: replacement == .shortcut), !(try sample().foreground) {
            do {
                // Use an observed, noninteractive native title. No guessed
                // point in document content, synthetic trial key or AXRaise.
                try await NativeWindowUnit(application: application, window: window).prepareTitle(
                    check: check, onPreparation: onPreparation, onCleanupUnknown: onCleanupUnknown)
            } catch let error as DriverError where error.code == .inputUnavailable {
                try check(); try checkEnabled(); try BackgroundTarget.validateReleasedInput()
                throw NativePreparationUnavailable(description: error.description)
            }
        }
        let receiver = NativeReceiverPreparation(application: application, window: window, element: element, receiverRelation: receiverRelation)
        try await receiver.prepare(foreground: false,
            check: { try check(); try checkEnabled() },
            ready: { try readiness(kind: request.kind, key: request.key, replacement: replacement, prepared: true).ready },
            onPreparation: onPreparation)
    }

    func deliver(_ request: NativeKeyboardIntent, replacement: NativeReplacementUnit.Selection? = nil, prepared: Bool = false,
                 check: @escaping @MainActor () throws -> Void,
                 onDispatch: @escaping @MainActor () throws -> Void,
                 onPosted: @escaping @MainActor () -> Void, onPreparation: (String) -> Void,
                 onClipboardRestore: (PasteboardTransaction.RestoreResult) -> Void,
                 onCleanupUnknown: @escaping () -> Void) async throws -> Bool {
        try check()
        try checkEnabled()
        try BackgroundEvents.requireKeyboardRoute()
        try BackgroundTarget.validateReleasedInput()
        let initial = try sample()
        guard try readiness(kind: request.kind, key: request.key, replacement: replacement, prepared: prepared, state: initial).ready else {
            throw DriverError("the selected keyboard unit is not ready before dispatch", code: .inputUnavailable)
        }
        let continuity = NativeReceiverContinuity(element: element, window: window, state: initial)
        let recipient = try BackgroundEvents.keyboardRecipient(mapping())
        func validateReceiver() throws {
            try check()
            try self.checkEnabled()
            let current = try self.sample()
            try continuity?.validate(current)
            guard try self.readiness(kind: request.kind, key: request.key, replacement: replacement, prepared: prepared, state: current).ready else {
                throw DriverError("keyboard unit readiness changed; remaining key events were not sent", code: .inputUnavailable)
            }
        }
        switch request.kind {
        case .focus: throw DriverError("focus is preparation, not keyboard delivery", code: .invalidRequest)
        case .key:
            try await Input.preparedKey(request.value!, recipient: recipient, validate: validateReceiver,
                onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
        case .insert:
            try await Input.preparedText(request.value!, recipient: recipient, applicationPID: application.processIdentifier,
                validate: validateReceiver, onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
        case .replace:
            guard let element, let replacement else {
                throw DriverError("replacement requires an addressed editing/selection contract", code: .invalidRequest)
            }
            return try await NativeReplacementUnit(element: element).deliver(text: request.value!, selection: replacement,
                recipient: recipient, applicationPID: application.processIdentifier, check: validateReceiver,
                onDispatch: onDispatch, onPosted: onPosted, onCleanupUnknown: onCleanupUnknown)
        case .paste:
            let transaction = try PasteboardTransaction()
            var sent = false
            // Cleanup stays inside this selected unit and its operation lease,
            // on success, cancellation and partial/unknown input alike.
            do {
                try validateReceiver()
                onPreparation("temporary clipboard payload")
                try transaction.install(request.value!)
                try await Input.preparedKey("super+v", recipient: recipient, validate: {
                    try validateReceiver(); try transaction.validateOwnership()
                }, onDispatch: onDispatch, onPosted: { sent = true; onPosted() }, onCleanupUnknown: onCleanupUnknown)
            } catch {
                onClipboardRestore(await transaction.finish(afterInput: sent))
                throw error
            }
            onClipboardRestore(await transaction.finish(afterInput: sent))
        }
        return false
    }
}
