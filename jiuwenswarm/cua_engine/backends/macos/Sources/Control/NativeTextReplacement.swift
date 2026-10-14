import Foundation

/// Only complete ordinary control values can use this route. An AXTextArea
/// inside a combo box is its value editor; a document/standalone text area is
/// not. Selection and identity are checked again immediately before mutation.
enum NativeTextReplacement {
    enum Route { case selectedText, value }
    enum Surface { case native, web, unknown }

    /// Bounded, live ancestry proof, following Peekaboo's TextInputRoute design.
    /// Unknown rejects this AX-edit route only; callers may prove keyboard input
    /// independently. It is not permission to guess that an editor is native.
    static func surface<Node>(from element: Node, pid: Int32, maxNodes: Int = 32, budget: TimeInterval = 0.25,
                              now: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
                              cancelled: () -> Bool = { Task.isCancelled },
                              same: (Node, Node) -> Bool,
                              read: (Node, TimeInterval) -> (role: String, pid: Int32, parent: Node?)?) -> Surface {
        guard pid > 0, maxNodes > 0, budget.isFinite, budget > 0 else { return .unknown }
        let end = now() + budget
        var current = element, seen = [Node]()
        while seen.count < maxNodes {
            let remaining = end - now()
            guard remaining > 0, !cancelled(), !seen.contains(where: { same($0, current) }) else { return .unknown }
            seen.append(current)
            guard let node = read(current, min(remaining / 3, 0.05)), now() < end, !cancelled(),
                  node.pid == pid, !node.role.isEmpty else { return .unknown }
            if node.role == "AXWebArea" { return .web }
            if ["AXWindow", "AXApplication"].contains(node.role) { return .native }
            guard let parent = node.parent else { return .unknown }
            current = parent
        }
        return .unknown
    }

    static func route(ownerRole: String, editorRole: String, surface: Surface,
                      selectable: Bool, value: String?, textWritable: Bool?, selectionWritable: Bool?, valueWritable: Bool?,
                      ownerSubrole: String? = nil, editorSubrole: String? = nil, replace: Bool = true) -> Route? {
        // A search field is an interactive query, not just a stored value.
        // AX setters can change its displayed text without delivering normal
        // editing/search callbacks, including when clearing the query. Choose
        // normal input before mutation; never replay a completed native write.
        guard ownerSubrole != "AXSearchField", editorSubrole != "AXSearchField",
              ["AXTextField", "AXComboBox"].contains(ownerRole),
              ["AXTextField", "AXTextArea"].contains(editorRole)
                || (ownerRole == "AXComboBox" && editorRole == "AXComboBox"), !selectable,
              surface == .native,
              let value, !value.contains("\n"), !value.contains("\r") else { return nil }
        if textWritable == true, !replace || selectionWritable == true { return .selectedText }
        return replace && valueWritable == true ? .value : nil
    }

    /// AX text ranges are UTF-16 offsets, not Swift grapheme counts. A malformed
    /// range cannot authorize a native edit or manufacture an expected value.
    static func inserting(_ text: String, into value: String, range: NSRange) -> String? {
        let units = Array(value.utf16)
        guard range.location >= 0, range.length >= 0, range.location <= units.count,
              range.length <= units.count - range.location else { return nil }
        func boundary(_ index: Int) -> Bool {
            index == 0 || index == units.count || !((0xD800...0xDBFF).contains(units[index - 1]) && (0xDC00...0xDFFF).contains(units[index]))
        }
        guard boundary(range.location), boundary(range.location + range.length) else { return nil }
        return (value as NSString).replacingCharacters(in: range, with: text)
    }
}
