@main struct NativeContainmentTests {
    static func main() throws {
        var parents = [4: 3, 3: 2, 2: 1]
        var children = [1: [2, 9], 2: [3], 3: [4], 4: [], 9: []]
        var reads = 0
        func expect(_ actual: NativeContainment.Evidence, _ expected: NativeContainment.Evidence) { precondition(actual == expected) }
        func evidence(_ anchors: [Int] = [1, 2], budget: Int = 2048) throws -> NativeContainment.Evidence {
            try NativeContainment.check(leaf: 4, within: anchors, same: ==,
                parent: { parents[$0] }, children: { reads += 1; return children[$0] }, check: {}, maxNodes: budget)
        }
        expect(try evidence(), .confirmed)
        precondition(reads == 0, "Complete parent evidence needs no forward tree scan")

        // Layout replacement does not replace the requested group or control.
        parents = [4: 5, 5: 2, 2: 1]
        children[2] = [5]; children[5] = [4]
        expect(try evidence(), .confirmed)

        // Incomplete reverse links can use the exact forward relationship.
        parents = [:]
        expect(try evidence(), .confirmed)
        precondition(reads > 0)

        // Same window, same control, different logical group: must not dispatch.
        parents = [4: 9, 9: 1]
        reads = 0
        expect(try evidence(), .changed)
        precondition(reads == 0, "A conflicting complete parent chain cannot be overridden by stale children")
        parents = [:]; children[5] = []; children[9] = [4]
        expect(try evidence(), .changed)
        children.removeValue(forKey: 5)
        expect(try evidence(), .unavailable)

        children[5] = [4]
        expect(try evidence(budget: 1), .unavailable)
        expect(try evidence([1, 1]), .unavailable)
        expect(try evidence([1, 4]), .unavailable)
        expect(try evidence([]), .confirmed)

        enum Cancelled: Error { case stopped }
        do {
            _ = try NativeContainment.check(leaf: 4, within: [1], same: ==,
                parent: { _ in nil }, children: { _ in [] }, check: { throw Cancelled.stopped })
            preconditionFailure("Cancellation must stop containment before input")
        } catch Cancelled.stopped { }
        print("PASS: selected containment supports either AX direction, preserves logical anchors, and rejects conflicting scope")
    }
}
