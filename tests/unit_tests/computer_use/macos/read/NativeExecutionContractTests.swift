import Foundation

@main struct NativeExecutionContractTests {
    @MainActor static func main() throws {
        var effects = 0
        let prepared = NativeExecutionReceipt(target: ["node": "target"], onEffect: { effects += 1 })
        prepared.recordPreparation("native element focus")
        prepared.confirmation = "confirmed"
        precondition(effects == 1 && prepared.dispatch == "notDispatched" && prepared.acceptedCount == 0)
        precondition(prepared.wire["preparation"] as? [String] == ["native element focus"])
        precondition(prepared.wire["errorCode"] is NSNull && prepared.wire["context"] is NSNull)
        precondition(prepared.wire["application"] == nil)

        // Identical failure text must retain distinct before/after-dispatch evidence.
        for (dispatch, accepted, expected) in [("notDispatched", false, "notDispatched"),
            ("unknown", false, "unknown"), ("unknown", true, "partial")] {
            let receipt = NativeExecutionReceipt(target: [:], onEffect: {})
            receipt.dispatch = dispatch
            if accepted { receipt.recordAccepted() }
            receipt.recordFailure(DriverError("provider stopped", code: .inputUnavailable))
            precondition(receipt.dispatch == expected && receipt.confirmation == "notRequested")
            precondition(receipt.errorCode == .inputUnavailable && !receipt.succeeded)
            precondition(receipt.wire["retryReason"] is NSNull, "formatting cannot authorize a retry")
        }
        let unavailable: Error = NativePreparationUnavailable(description: "route unavailable")
        precondition((unavailable as? ClassifiedFailure)?.code == .inputUnavailable)
        let boundary: Error = DriverError("route unavailable", code: .inputUnavailable)
        precondition(!(boundary is NativePreparationUnavailable))

        // Cleanup uncertainty suppresses retries even if a preparatory route failed.
        for outcome: PasteboardTransaction.RestoreResult in [.restored, .ownershipLost, .failed] {
            let receipt = NativeExecutionReceipt(target: [:], onEffect: {})
            receipt.retryReason = "preparationUnavailable"
            receipt.recordClipboardRestore(outcome)
            let clean = receipt.cleanup == "complete"
            precondition((receipt.wire["retryReason"] is NSNull) == !clean)
            precondition(receipt.succeeded == clean)
            precondition((receipt.wire["detail"] as! String).lowercased().contains("clipboard"))
            receipt.recordCleanupUnknown()
            receipt.recordClipboardRestore(.failed)
            precondition(receipt.cleanup == "unknown" && receipt.wire["retryReason"] is NSNull)
        }
        let launch = NativeExecutionReceipt(target: ["application": "installation"],
            detail: "Application launch pending.", confirmation: "notRequested", onEffect: {})
        launch.application = ["id": "running-app"]
        launch.confirmation = "confirmed"
        precondition(launch.dispatch == "notDispatched", "already running does not imply a launch")
        precondition((launch.wire["application"] as? [String: String])?["id"] == "running-app")
        _ = try JSONSerialization.data(withJSONObject: launch.wire)

        // Channel and preparation declarations must not collapse AX value edits,
        // selection edits, keys, native actions and screenshot input into one route.
        let cases: [(NativeInputUnit, String, String, String)] = [
            (.textValue(.init(role: "AXTextField", subrole: nil)), "native_text_edit", "object", "no"),
            (.selectedText(.init(role: "AXTextArea", replace: true)), "native_text_edit", "editor", "yes"),
            (.receiverContext(.init(role: "AXTextArea")), "prepare_receiver", "editor", "yes"),
            (.keyboard(.paste), "clipboard_paste", "keyboard", "yes"),
            (.keyboard(.replace), "key_sequence", "keyboard", "yes"),
            (.action("AXPress", nil), "native_action", "object", "no"),
            (.menuAction("AXPress", "Open"), "native_action", "object", "yes"),
            (.scroll, "native_scroll", "object", "no"),
            (.pointer(.click), "pointer_sequence", "pointer", "yes"),
            (.desktop("click"), "pointer_sequence", "pointer", "no"),
            (.desktop("insert"), "key_sequence", "keyboard", "no")]
        let pending = NativeReadiness([("receiver", "unknown")])
        for (unit, name, channel, background) in cases {
            let wire = unit.declaration(id: "contract", supported: false, readiness: pending, exclusive: false)
            precondition(wire["unit"] as? String == name && wire["channel"] as? String == channel)
            precondition(wire["backgroundPreparation"] as? String == background)
            precondition(wire["supported"] as? String == "no" && wire["readiness"] as? String == "unknown")
            precondition(wire["pending"] as? [String] == ["receiver"])
        }
        let pointer = NativeInputUnit.pointer(.click).declaration(id: "p", supported: true, readiness: NativeReadiness(), exclusive: true)
        precondition(pointer["backgroundPreparation"] as? String == "no")
        let menu = NativeInputUnit.menuAction("AXPress", "Open").declaration(id: "m", supported: true, readiness: NativeReadiness(), exclusive: false)
        precondition(menu["backgroundPreparation"] as? String == "no")
        precondition(menu["action"] as? [String: String] == ["name": "AXPress", "description": "Open"])
        print("PASS native declarations and receipts preserve dispatch, preparation, cleanup and retry evidence")
    }
}
