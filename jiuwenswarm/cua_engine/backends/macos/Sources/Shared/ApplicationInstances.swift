import Foundation

/// Session-local selectors identify native application lifetimes, never names or PIDs.
/// Retiring an entry cannot make its selector refer to a replacement instance.
final class ApplicationInstances<Identity: Hashable> {
    private let namespace = UUID().uuidString
    private var next: UInt64 = 0
    private var ids = [Identity: String]()
    private var instances = [String: Identity]()

    static func isInstanceSelector(_ selector: String) -> Bool { selector.hasPrefix("instance:") }

    func id(for identity: Identity) -> String {
        if let id = ids[identity] { return id }
        let id = "instance:\(namespace):\(next)"
        next += 1
        ids[identity] = id
        instances[id] = identity
        return id
    }

    func instance(_ id: String) -> Identity? { instances[id] }

    func retire(where predicate: (Identity) -> Bool) {
        for (identity, id) in ids where predicate(identity) {
            ids.removeValue(forKey: identity)
            instances.removeValue(forKey: id)
        }
    }
}
