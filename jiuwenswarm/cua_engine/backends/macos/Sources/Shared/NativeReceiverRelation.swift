import ApplicationServices

/// Recheck the exact relation selected in Python. Structural semantic fields
/// are guarded by SelectionBasis; this unit never searches for another member.
@MainActor struct NativeReceiverRelation {
    let editor: AXUIElement
    let related: AXUIElement
    let selection: NativeInspectQuery.Receiver

    func matches(focused: AXUIElement, window: AXUIElement, pid: pid_t) throws -> Bool {
        try RequestDiagnostics.checkCancellation()
        let anchor: AXUIElement
        let depth: Int
        switch selection {
        case .containedFocus:
            guard CFEqual(focused, related) else { return false }
            anchor = editor; depth = 8
        case .virtualFocus(_, let via):
            let property = via == "owns" ? "AXOwns" : "AXLinkedUIElements"
            guard [kAXTextFieldRole, kAXComboBoxRole].contains(AXAccess.read(editor, kAXRoleAttribute).value as? String ?? ""),
                  AXAccess.read(editor, "AXHasPopup").value as? Bool == true,
                  [kAXListRole, kAXOutlineRole, kAXTableRole].contains(AXAccess.read(related, kAXRoleAttribute).value as? String ?? ""),
                  AXAccess.read(focused, kAXFocusedAttribute).value as? Bool == true,
                  let focusedRole = AXAccess.read(focused, kAXRoleAttribute).value as? String, !focusedRole.isEmpty,
                  ![kAXTextFieldRole, kAXTextAreaRole, kAXComboBoxRole, kAXIncrementorRole].contains(focusedRole),
                  let owned = AXAccess.elements(editor, property, limit: 64), owned.contains(where: { CFEqual($0, related) }),
                  let selected = AXAccess.elements(related, kAXSelectedChildrenAttribute, limit: 2),
                  selected.count == 1, CFEqual(selected[0], focused) else { return false }
            anchor = related; depth = 11  // Same 12 visited-node budget as VirtualFocusQuery.
        }
        for element in [editor, related, focused] {
            var actual: pid_t = 0
            guard AXUIElementGetPid(element, &actual) == .success, actual == pid,
                  let owner = AXAccess.element(element, kAXWindowAttribute), CFEqual(owner, window) else { return false }
        }
        var current = focused, seen = [AXUIElement]()
        for _ in 0..<depth {
            try RequestDiagnostics.checkCancellation()
            if CFEqual(current, anchor) { return true }
            guard !seen.contains(where: { CFEqual($0, current) }),
                  let parent = AXAccess.element(current, kAXParentAttribute) else { return false }
            seen.append(current); current = parent
        }
        return CFEqual(current, anchor)
    }
}
