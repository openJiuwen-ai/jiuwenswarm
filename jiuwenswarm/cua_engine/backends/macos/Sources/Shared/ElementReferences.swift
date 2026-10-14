import Foundation

/// Session-local decimal references. Remember identities across observations; never recycle a number.
final class ElementReferences<Identity: Hashable> {
    private var references = [Identity: String]()
    private var aliasOwners = [Identity: Identity]()
    private var next: UInt64 = 0

    /// A lifetime registry may own the identity; only the decimal counter is shared.
    func allocate() -> String { let value = String(next); next += 1; return value }

    func reference(for identity: Identity, aliases: [Identity] = []) -> String {
        let reference: String
        let unbound = aliases.filter { aliasOwners[$0] == nil || aliasOwners[$0] == identity }
        if let known = references[identity] ?? unbound.compactMap({ references[$0] }).first { reference = known }
        else { reference = allocate() }
        references[identity] = reference
        for alias in aliases { references[alias] = reference; aliasOwners[alias] = identity }
        return reference
    }
}
