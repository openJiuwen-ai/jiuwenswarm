import Foundation

@main struct ObservedElementsTests {
    static func main() {
        let store = ObservedElements<String, String, String>(namespace: "test")
        let a = store.observe("nativeA", evidence: "documentA/field", value: "first metadata")
        let before = store.bind(a, text: "old🙂")!
        precondition(store.observe("nativeA", evidence: "documentA/field", value: "new metadata") == a)
        let after = store.bind(a, text: "new")!
        precondition(before != after)
        precondition(store.target(before) == "new metadata")
        precondition(store.text(before)?.value == "old🙂" && store.text(after)?.value == "new")
        // Observing a subtree does not imply that its absent siblings were deleted.
        _ = store.observe("nativeB", evidence: "documentB/field", value: "other")
        precondition(store.target(before) != nil)
        let recycled = store.observe("nativeA", evidence: "documentB/field", value: "recycled object")
        precondition(recycled != a && store.target(before) == nil)
        let restored = store.observe("nativeA", evidence: "documentA/field", value: "same old label")
        precondition(restored != a && restored != recycled && store.target(before) == nil)
        precondition(store.text(before)?.value == "old🙂", "destroyed controls retain their captured text")
        let missing = store.bind(restored, text: nil)!
        precondition(store.text(missing) != nil && store.text(missing)!.value == nil)
        store.invalidate { $0 == "same old label" }
        precondition(store.target(missing) == nil && store.text(missing) != nil)

        let bounded = ObservedElements<Int, Int, Int>(namespace: "bounded", targetLimit: 2, bindingLimit: 3, textLimit: 6)
        let first = bounded.observe(1, evidence: 1, value: 1), one = bounded.bind(first, text: "1234")!
        let two = bounded.bind(first, text: "abcd")!
        precondition(bounded.text(one) == nil && bounded.target(one) == 1, "text eviction cannot invalidate a live action")
        precondition(bounded.text(two)?.value == "abcd")
        let huge = bounded.bind(first, text: "too large to retain")!
        precondition(bounded.target(huge) == 1 && bounded.text(huge) == nil)
        _ = bounded.observe(2, evidence: 2, value: 2)
        _ = bounded.observe(3, evidence: 3, value: 3)
        precondition(bounded.target(two) == nil && bounded.text(two)?.value == "abcd")
        precondition(bounded.observe(1, evidence: 1, value: 1) != first, "cache eviction never revives an old target")
        let leased = ObservedElements<Int, Int, Int>(namespace: "lease", targetLimit: 2, bindingLimit: 2)
        let root = leased.observe(1, evidence: 1, value: 1), alias = leased.bind(root, text: "snapshot")!
        let release = leased.retain(alias)!
        for number in 2...5 {
            let node = leased.observe(number, evidence: number, value: number)
            _ = leased.bind(node, text: nil)
        }
        precondition(leased.target(alias) == 1 && leased.target(root) == 1, "operation retains both live identity and its input alias")
        release(); release()
        _ = leased.observe(6, evidence: 6, value: 6)
        precondition(leased.target(root) == nil)
        let changing = leased.observe(7, evidence: 7, value: 7)
        let releaseChanged = leased.retain(changing)!
        _ = leased.observe(7, evidence: 8, value: 8)
        precondition(leased.target(changing) == nil, "retention never extends the life of a replaced real target")
        releaseChanged()
        let accumulating = ObservedElements<String, [String: String], String>(namespace: "evidence", reconcile: { old, _, new, value in
            guard old.allSatisfy({ new[$0.key] == nil || new[$0.key] == $0.value }) else { return nil }
            return (old.merging(new) { _, new in new }, value)
        })
        let original = accumulating.observe("native", evidence: ["id": "original"], value: "old metadata")
        precondition(accumulating.observe("native", evidence: [:], value: "partial metadata") == original)
        precondition(accumulating.observe("native", evidence: ["document": "one"], value: "new metadata") == original)
        precondition(accumulating.target(original) == "new metadata")
        precondition(accumulating.observe("native", evidence: ["id": "changed"], value: "other control") != original)
        precondition(accumulating.target(original) == nil, "reconciliation must retain accumulated evidence, not only update metadata")
        print("PASS: stable target lifetimes, immutable text, partial reads, identity recycling and bounded independent caches")
    }
}
