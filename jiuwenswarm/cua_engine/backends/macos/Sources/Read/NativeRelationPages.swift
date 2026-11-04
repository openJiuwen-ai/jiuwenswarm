import Foundation

/// A continuation owns the exact native prefix already observed, not a naked
/// offset into a mutable list. No presentation or target-selection policy.
@MainActor final class NativeRelationPages<Value> {
    struct Selection {
        let source: String
        let property: String
        let total: Int?
        let prefix: [Value]
        var offset: Int { prefix.count }
    }
    private let equal: (Value, Value) -> Bool
    private let maxEntries: Int
    let maxPrefix: Int
    private let maxElements: Int
    private var entries = [String: Selection]()
    private var order = [String]()
    private var elements = 0

    init(maxEntries: Int = 256, maxPrefix: Int = 8192, maxElements: Int = 32768,
         equal: @escaping (Value, Value) -> Bool) {
        self.maxEntries = maxEntries; self.maxPrefix = maxPrefix; self.maxElements = maxElements
        self.equal = equal
    }

    func select(_ cursor: String?, source: String, property: String) throws -> Selection {
        guard let cursor else { return Selection(source: source, property: property, total: nil, prefix: []) }
        guard let selection = entries[cursor] else {
            throw DriverError("native relation cursor expired; read its source again", code: .snapshotExpired)
        }
        guard selection.source == source, selection.property == property else {
            throw DriverError("native relation cursor belongs to another source or property", code: .invalidRequest)
        }
        return selection
    }

    func validate(_ selection: Selection, total: Int, prefix: [Value]) throws {
        guard selection.total == nil || selection.total == total,
              prefix.count >= selection.prefix.count,
              zip(selection.prefix, prefix).allSatisfy({ equal($0.0, $0.1) }) else {
            throw DriverError("native relation changed before continuation; read its source again", code: .snapshotExpired)
        }
    }

    func retain(source: String, property: String, total: Int, prefix: [Value]) -> String? {
        guard prefix.count < total, prefix.count < maxPrefix, prefix.count <= maxElements, maxEntries > 0 else { return nil }
        let cursor = UUID().uuidString
        entries[cursor] = Selection(source: source, property: property, total: total, prefix: prefix)
        order.append(cursor); elements += prefix.count
        while order.count > maxEntries || elements > maxElements {
            let expired = order.removeFirst()
            elements -= entries.removeValue(forKey: expired)?.prefix.count ?? 0
        }
        return cursor
    }

    func contains(_ cursor: String) -> Bool { entries[cursor] != nil }
}
