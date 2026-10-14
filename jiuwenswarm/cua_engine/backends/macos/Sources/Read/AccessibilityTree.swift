import Foundation

/// Platform-neutral window relationships. Unknown ownership/modality stays unknown.
struct AccessibilityWindowInfo {
    var ref: String
    var title: String
    var kind: String = "window"
    var parent: String? = nil
    var modal: Bool? = nil
    var onScreen: Bool? = nil
    var document: String? = nil

    var protocolValue: [String: Any] {
        var result: [String: Any] = ["ref": ref, "title": title, "kind": kind]
        if let parent { result["parent"] = parent }
        if let modal { result["modal"] = modal }
        if let onScreen { result["onScreen"] = onScreen }
        if let document { result["document"] = document }
        return result
    }
}

/// An application-wide menu and the windows the provider currently reports.
/// These are observation facts, not proof of a command's eventual receiver.
struct ApplicationMenuContext {
    let mainWindow: String?
    let focusedWindow: String?

    init(mainWindowMatches: [String], focusedWindowMatches: [String]) {
        mainWindow = mainWindowMatches.count == 1 ? mainWindowMatches[0] : nil
        focusedWindow = focusedWindowMatches.count == 1 ? focusedWindowMatches[0] : nil
    }

    var protocolValue: [String: Any] {
        ["mainWindow": mainWindow as Any? ?? NSNull(), "focusedWindow": focusedWindow as Any? ?? NSNull()]
    }
}

/// A bounded native snapshot. Raw containment crosses the wire; common Python renders it.
struct AccessibilityNode {
    var ref: String
    var role: String
    var roleDescription: String = ""
    var name: String = ""
    var description: String = ""
    var value: String? = nil
    var identifier: String = ""
    var enabled: Bool? = nil
    var selected: Bool? = nil
    var expanded: Bool? = nil
    var disclosing: Bool? = nil
    var disclosureLevel: Int? = nil
    var disclosedBy: String? = nil
    var incompleteDisclosureRelation = false
    var placeholder: String = ""
    var expandable = false
    var focused: Bool? = nil
    var selectedText: String? = nil
    var selectionRange: NSRange? = nil
    var shortcut: String? = nil
    var pressable = false
    var selectable = false
    var submenu = false
    var children: [AccessibilityNode] = []
    var help: String = ""
    var url: String = ""
    var focusable = false
    // Native label edges, resolved only against this observation's real elements.
    var titleRef: String? = nil
    var labelTargets: [String] = []
    var incompleteLabelRelation = false
    var linkedLabel = false
    var preserveGroup = false
    var valueExpected = false
    // Native direct-child counts, measured before display compaction.
    var childrenRead: Int? = nil
    var childCount: Int? = nil
    var partialReason: String? = nil
    var childrenStart: Int? = nil
    var nextChildOffset: Int? = nil
    var childrenEnd: Int? = nil
    var childOmissions: [String] = []
    var subrole: String = ""
    var modal: Bool? = nil
    var ownedModals: [String] = []
    var blockedBy: [String] = []
    var valueIsNumber = false
    // nil means unavailable; an empty mark means no visible mark, not proof
    // that this menu command is an unchecked toggle.
    var menuMark: String? = nil
    // Declarations remain separate from executable click/select routes.
    var actions: [(name: String, description: String?)]? = nil
    var actionsUnavailable = false
    var capabilities: ControlCapabilities? = nil
    var editorRef: String? = nil

    /// Returns whether the visible-child list should be read. An empty viewport
    /// must not hide known native children; their visibility remains unknown.
    /// An unavailable native count must likewise remain unknown on the wire.
    @discardableResult
    mutating func recordViewportCount(visibleCount: Int, nativeCount: Int?) -> Bool {
        childCount = nativeCount
        childrenRead = 0
        if visibleCount == 0, let nativeCount, nativeCount > 0 {
            partialReason = "visible children empty; native children retained (visibility unknown)"
            return false
        }
        if nativeCount == nil { partialReason = "viewport only" }
        reconcileViewportCount(visibleCount)
        return true
    }

    /// Queries are not atomic. A later viewport cannot contain more children
    /// than a trustworthy total; keep observed children and drop that total.
    mutating func reconcileViewportCount(_ visibleCount: Int) {
        guard let total = childCount else { return }
        if visibleCount > total {
            childCount = nil
            partialReason = "inconsistent native child counts"
        } else if visibleCount < total {
            partialReason = partialReason ?? "viewport only"
        }
    }

    /// Wire observations keep raw containment. Presentation is performed by the common SDK.
    var protocolNode: [String: Any] {
        func roleName(_ role: String) -> String {
            let name = role.hasPrefix("AX") ? String(role.dropFirst(2)) : role
            let normalized = name.prefix(1).lowercased() + name.dropFirst()
            return normalized.range(of: "^[a-z][a-zA-Z0-9]*$", options: .regularExpression) == nil ? "unknown" : normalized
        }
        var result: [String: Any] = ["ref": ref, "role": roleName(role), "children": children.map(\.protocolNode)]
        for (key, value) in [("roleDescription", roleDescription), ("name", name), ("description", description),
            ("identifier", identifier), ("help", help), ("url", url), ("placeholder", placeholder), ("subrole", subrole.isEmpty ? "" : roleName(subrole))] where !value.isEmpty { result[key] = value }
        if roleName(role) == "unknown", roleDescription.isEmpty, !role.isEmpty { result["roleDescription"] = role }
        for (key, value) in [("value", value), ("selectedText", selectedText), ("shortcut", shortcut), ("titleRef", titleRef),
            ("partialReason", partialReason), ("menuMark", menuMark), ("disclosedBy", disclosedBy)] { if let value { result[key] = value } }
        for (key, value) in [("enabled", enabled), ("selected", selected), ("expanded", expanded), ("disclosing", disclosing), ("focused", focused), ("modal", modal)] {
            if let value { result[key] = value }
        }
        for (key, value) in [("expandable", expandable), ("pressable", pressable), ("selectable", selectable), ("submenu", submenu),
            ("focusable", focusable), ("incompleteLabelRelation", incompleteLabelRelation), ("linkedLabel", linkedLabel),
            ("preserveGroup", preserveGroup), ("valueExpected", valueExpected), ("valueIsNumber", valueIsNumber),
            ("incompleteDisclosureRelation", incompleteDisclosureRelation)] where value { result[key] = value }
        for (key, value) in [("childrenRead", childrenRead), ("childCount", childCount), ("childrenStart", childrenStart),
            ("nextChildOffset", nextChildOffset), ("childrenEnd", childrenEnd), ("disclosureLevel", disclosureLevel)] { if let value { result[key] = value } }
        for (key, value) in [("labelTargets", labelTargets), ("ownedModals", ownedModals), ("blockedBy", blockedBy), ("childOmissions", childOmissions)] where !value.isEmpty { result[key] = value }
        if let range = selectionRange, range.location != NSNotFound, range.location >= 0, range.length >= 0,
           range.location <= 9_007_199_254_740_991, range.length <= 9_007_199_254_740_991 - range.location {
            result["selectionRange"] = ["location": range.location, "length": range.length]
        }
        if let actions {
            result["actions"] = actions.map { action -> [String: String] in
                var value = ["name": action.name]
                if let description = action.description, !description.isEmpty { value["description"] = description }
                return value
            }
        }
        if actionsUnavailable { result["actionsUnavailable"] = true }
        if let capabilities { result["capabilities"] = capabilities.protocolValue }
        if let editorRef { result["editorRef"] = editorRef }
        return result
    }

    /// Derive interaction scope only from declared native modality, not names,
    /// coordinates, focus or a newly appeared node. Nested dialogs form nested scopes.
    func withInteractions(inherited: [String] = []) -> AccessibilityNode {
        func modals(_ node: AccessibilityNode) -> [String] {
            if node.modal == true { return [node.ref] }
            if ["AXWindow", "AXSheet"].contains(node.role) { return [] }
            return node.children.flatMap(modals)
        }
        var node = self
        let scope = ["AXWindow", "AXSheet"].contains(role) || modal == true
        let own = scope ? Array(Set(ownedModals + children.flatMap(modals))).sorted() : []
        let incoming = inherited.contains(ref) || role == "AXWindow" ? [] : inherited
        node.blockedBy = Array(Set(incoming + own)).sorted()
        if !node.blockedBy.isEmpty, node.capabilities != nil { node.capabilities = .unavailable }
        node.children = children.map { $0.withInteractions(inherited: node.blockedBy) }
        return node
    }

    static func bounded(_ text: String, limit: Int) -> String {
        guard text.count > limit else { return text }
        let head = limit / 4
        return String(text.prefix(head)) + "\n[… \(text.count - limit) characters omitted …]\n" + String(text.suffix(limit - head))
    }

    /// AX menu modifiers use Command by default, unlike CGEventFlags.
    /// This is a display label from native data, not a pressKey argument.
    static func menuShortcut(character: String?, modifiers: Int?) -> String? {
        guard let character, character.unicodeScalars.count == 1,
              let scalar = character.unicodeScalars.first,
              let modifiers, (0...15).contains(modifiers) else { return nil }
        let key: String
        switch scalar.value {
        case 0xF704...0xF70F: key = "F\(scalar.value - 0xF704 + 1)"
        case 0xF700: key = "↑"
        case 0xF701: key = "↓"
        case 0xF702: key = "←"
        case 0xF703: key = "→"
        case 32: key = "Space"
        default:
            guard !CharacterSet.controlCharacters.contains(scalar),
                  !CharacterSet.whitespacesAndNewlines.contains(scalar),
                  !CharacterSet.illegalCharacters.contains(scalar),
                  scalar.properties.generalCategory != .privateUse else { return nil }
            key = character
        }
        let flags = [(4, "⌃"), (2, "⌥"), (1, "⇧")].filter { modifiers & $0.0 != 0 }.map { $0.1 }.joined()
        return flags + (modifiers & 8 == 0 ? "⌘" : "") + key
    }

    /// Keep UI strings on their own line: labels cannot forge a node, footer, or diff marker.
    static func inline(_ text: String) -> String {
        text.unicodeScalars.map { scalar in
            switch scalar.value {
            case 10: return "\\n"
            case 13: return "\\r"
            case 9: return "\\t"
            case 0...31, 127...159, 0x2028, 0x2029: return "\\u{\(String(scalar.value, radix: 16))}"
            default: return String(scalar)
            }
        }.joined()
    }

    /// Plain fields stay readable; JSON strings protect punctuation, whitespace and reserved markers.
    static func field(_ text: String) -> String {
        let reserved = text.hasPrefix("<") || text.hasPrefix("+") || text.hasPrefix("~") || text.hasPrefix("|")
            || text.contains("(partial:") || text.contains("(showing ")
        let quote = text.isEmpty || text != text.trimmingCharacters(in: .whitespacesAndNewlines)
            || text.contains(",") || text.contains("\"") || text.contains("\\") || reserved
            || text.unicodeScalars.contains { $0.value < 32 || (127...159).contains($0.value) || [0x2028, 0x2029].contains($0.value) }
        return quote ? quoted(text) : text
    }

    static func quoted(_ text: String) -> String {
        let data = try! JSONSerialization.data(withJSONObject: text, options: [.fragmentsAllowed, .withoutEscapingSlashes])
        return String(decoding: data, as: UTF8.self).unicodeScalars.map { scalar in
            switch scalar.value {
            case 127...159, 0x2028, 0x2029: return "\\u" + String(format: "%04x", scalar.value)
            default: return String(scalar)
            }
        }.joined()
    }

}
