import Foundation

/// Live native identities and immutable captured text have different lifetimes.
/// A new observation can update a confirmed target without retargeting an old
/// handle. Missing nodes in a partial observation do not prove destruction.
final class ObservedElements<Key: Hashable, Evidence: Equatable, Value> {
    struct TextSnapshot { let value: String? }
    private struct Target {
        let key: Key
        var evidence: Evidence
        var value: Value
    }
    private struct Snapshot { let text: String?; let bytes: Int }
    private let namespace: String
    private let targetLimit: Int
    private let bindingLimit: Int
    private let textLimit: Int
    private let reconcile: (Evidence, Value, Evidence, Value) -> (Evidence, Value)?
    private var serial: UInt64 = 0
    private var current = [Key: String]()
    private var targets = [String: Target]()
    private var bindings = [String: String]()
    private var snapshots = [String: Snapshot]()
    private var targetOrder = [String]()
    private var bindingOrder = [String]()
    private var snapshotOrder = [String]()
    private var textBytes = 0
    private var pins = [String: Int]()
    private var pinnedBindings = [String: Int]()

    init(namespace: String, targetLimit: Int = 8_192, bindingLimit: Int = 16_384, textLimit: Int = 16 * 1_024 * 1_024,
         reconcile: ((Evidence, Value, Evidence, Value) -> (Evidence, Value)?)? = nil) {
        precondition(targetLimit > 0 && bindingLimit > 0 && textLimit >= 0)
        self.namespace = namespace; self.targetLimit = targetLimit
        self.bindingLimit = bindingLimit; self.textLimit = textLimit
        self.reconcile = reconcile ?? { old, _, new, value in old == new ? (new, value) : nil }
    }

    private func next(_ kind: String) -> String { serial += 1; return "\(namespace):\(kind)\(serial)" }

    /// Returns a stable lifetime, including across ordinary reads and text edits.
    /// Changed identity evidence retires the old lifetime permanently.
    func observe(_ key: Key, evidence: Evidence, value: Value) -> String {
        if let id = current[key], var known = targets[id] {
            if let (merged, value) = reconcile(known.evidence, known.value, evidence, value) {
                known.evidence = merged; known.value = value; targets[id] = known
                return id
            }
            retire(id)
        }
        let id = next("t")
        current[key] = id; targets[id] = Target(key: key, evidence: evidence, value: value)
        targetOrder.append(id)
        evict()
        return id
    }

    func observed(_ key: Key) -> (id: String, value: Value)? {
        guard let id = current[key], let target = targets[id] else { return nil }
        return (id, target.value)
    }
    func retain(_ reference: String) -> (() -> Void)? {
        let id = bindings[reference] ?? reference
        guard targets[id] != nil else { return nil }
        pins[id, default: 0] += 1
        if bindings[reference] != nil { pinnedBindings[reference, default: 0] += 1 }
        var released = false
        return { [weak self] in
            guard !released, let self else { return }
            released = true
            if let count = self.pins[id], count > 1 { self.pins[id] = count - 1 }
            else { self.pins.removeValue(forKey: id) }
            if let count = self.pinnedBindings[reference], count > 1 { self.pinnedBindings[reference] = count - 1 }
            else { self.pinnedBindings.removeValue(forKey: reference) }
            self.evict()
            self.evictBindings()
        }
    }

    private func evict() {
        while targets.count > targetLimit {
            guard let id = targetOrder.first(where: { pins[$0] == nil }) else { break }
            retire(id)
        }
    }

    private func evictBindings() {
        while bindings.count > bindingLimit {
            guard let token = bindingOrder.first(where: { pinnedBindings[$0] == nil }) else { break }
            bindings.removeValue(forKey: token)
            bindingOrder.removeAll { $0 == token }
        }
    }

    /// Each delivered reference captures its own immutable text, while actions
    /// resolve to the same confirmed native target's most recent metadata.
    func bind(_ id: String, text: String?) -> String? {
        guard targets[id] != nil else { return nil }
        let token = next("e")
        bindings[token] = id; bindingOrder.append(token)
        evictBindings()
        let bytes = text?.utf8.count ?? 0
        if bytes <= textLimit {
            snapshots[token] = Snapshot(text: text, bytes: bytes)
            snapshotOrder.append(token); textBytes += bytes
            while textBytes > textLimit || snapshots.count > bindingLimit {
                let expired = snapshotOrder.removeFirst()
                textBytes -= snapshots.removeValue(forKey: expired)!.bytes
            }
        }
        return token
    }

    // Live-node references and captured-text tokens resolve to the same
    // retained lifetime; immutable text remains available after it expires.
    func target(_ token: String) -> Value? { targets[bindings[token] ?? token]?.value }
    /// Outer nil means expired; an existing snapshot with nil text means no string Value.
    func text(_ token: String) -> TextSnapshot? { snapshots[token].map { TextSnapshot(value: $0.text) } }

    func invalidate(where predicate: (Value) -> Bool) {
        retire(Set(targets.compactMap { predicate($0.value.value) ? $0.key : nil }))
    }

    private func retire(_ id: String) { retire([id]) }

    private func retire(_ ids: Set<String>) {
        guard !ids.isEmpty else { return }
        for id in ids {
            guard let target = targets.removeValue(forKey: id) else { continue }
            pins.removeValue(forKey: id)
            if current[target.key] == id { current.removeValue(forKey: target.key) }
        }
        targetOrder.removeAll { ids.contains($0) }
        // Captured text remains readable; action aliases cannot revive this lifetime.
    }
}
