import Foundation

/// Evidence that an observed native object still represents the same target.
/// Mutable input values, selection, enabled state and window titles are not identity.
struct ElementIdentity: Equatable {
    struct Facts {
        var values: [String: String] = [:]
        var absent: Set<String> = []
    }
    enum Check { case matched, unavailable, changed }
    let attributes: [String: String]
    let absent: Set<String>

    // A retained provider object can be recycled for another virtual cell.
    // These are native, advertised positions, not labels or a parsed A1 name.
    static let positionKeys: Set<String> = ["AXRowIndexRange", "AXColumnIndexRange"]
    static let keys = ["AXRole", "AXSubrole", "AXIdentifier", "AXURL", "AXTitle", "AXDescription", "AXDocument"]
        + positionKeys.sorted()
    private static let labelledRoles: Set<String> = ["AXButton", "AXMenuItem", "AXMenuBarItem", "AXCheckBox", "AXRadioButton", "AXLink", "AXRow"]

    init(role: String, subrole: String?, identifier: String?, url: String?, title: String?, description: String?, document: String? = nil) {
        var attributes = role.isEmpty ? [:] : ["AXRole": role]
        for (key, value) in [("AXSubrole", subrole), ("AXIdentifier", identifier), ("AXURL", url), ("AXDocument", document)] {
            if let value, !value.isEmpty { attributes[key] = value }
        }
        // A reused command/record with a different label is a different action.
        // Editor text can legitimately change during this observation's lifetime.
        if Self.labelledRoles.contains(role) {
            for (key, value) in [("AXTitle", title), ("AXDescription", description)] {
                if let value, !value.isEmpty { attributes[key] = value }
            }
        }
        self.attributes = attributes
        self.absent = [] // Optional arguments do not establish native absence.
    }

    init(facts: Facts, roleHint: String? = nil) {
        var keys = Set(Self.keys)
        if !Self.labelledRoles.contains(facts.values["AXRole"] ?? roleHint ?? "") {
            keys.subtract(["AXTitle", "AXDescription"])
        }
        attributes = facts.values.filter { keys.contains($0.key) && !$0.value.isEmpty }
        absent = facts.absent.union(facts.values.filter { $0.value.isEmpty }.map(\.key)).intersection(keys)
    }

    private init(attributes: [String: String], absent: Set<String>) {
        self.attributes = attributes; self.absent = absent
    }

    /// Missing evidence does not contradict the old identity. Preserve known
    /// constraints while permitting later reads to refine previously unknown fields.
    func refining(with next: Self) -> Self? {
        guard attributes.allSatisfy({ key, value in
            !next.absent.contains(key) && (next.attributes[key] == nil || next.attributes[key] == value)
        }) else { return nil }
        let merged = attributes.merging(next.attributes) { _, new in new }
        // Absence can contradict a previously observed value, but is not a
        // permanent identity constraint. A provider may expose an optional ID
        // later; failure to query a never-present ID must not block all input.
        return Self(attributes: merged, absent: absent.union(next.absent).subtracting(merged.keys))
    }

    /// Check current facts, never merged history. A failed query cannot prove
    /// success, and cannot permanently retire an otherwise retained object.
    func check(_ current: Facts) -> Check {
        if attributes.contains(where: { key, value in
            current.absent.contains(key) || current.values[key].map { $0 != value } == true
        }) { return .changed }
        guard attributes["AXRole"] != nil,
              attributes.allSatisfy({ current.values[$0.key] == $0.value }) else { return .unavailable }
        return .matched
    }

    func matches(_ current: [String: String]) -> Bool {
        check(Facts(values: current)) == .matched
    }
}
