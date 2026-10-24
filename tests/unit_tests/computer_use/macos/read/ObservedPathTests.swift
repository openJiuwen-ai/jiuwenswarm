@main struct ObservedPathTests {
    static func main() throws {
        // These in-memory nodes have stable, distinct identities. Real providers
        // can recycle AX references by position; this test does not cover that
        // unobservable replacement. Only the explicitly observed node may run.
        let titles = [40: "Duplicate", 41: "Duplicate", 42: "Duplicate"]
        let selected = [10, 20, 30, 41]
        var tree = [10: [20], 20: [30], 30: [40, 41]]
        var dispatched = [Int]()
        func dispatch(_ path: [Int], root: Int = 10) {
            if ObservedPath.matches(path, root: root, same: ==, children: { tree[$0] ?? [] }) {
                dispatched.append(path.last!)
            }
        }
        precondition(titles[40] == titles[41])
        dispatch(selected)
        precondition(dispatched == [41], "A duplicate title must not reject or redirect an exact reference")

        tree[30] = [41, 40]
        dispatch(selected)
        precondition(dispatched == [41, 41], "Sibling reordering must not change the chosen native command")

        // Retaining the old object does not keep it live in the menu.
        tree[30] = [40, 42]
        precondition(titles[42] == titles[41])
        dispatch(selected)
        precondition(dispatched == [41, 41], "A removed command cannot bind to its same-titled replacement")

        tree[30] = [40]
        tree[31] = [41]
        tree[20] = [30, 31]
        dispatch(selected)
        precondition(dispatched == [41, 41], "Moving the same object to another parent invalidates the old path")

        tree[30] = [40, 41]
        tree[10] = [21]
        tree[21] = [30]
        dispatch(selected)
        precondition(dispatched == [41, 41], "Replacing an ancestor cannot silently retarget an observed reference")

        tree[10] = [20]
        dispatch(selected, root: 11)
        precondition(dispatched == [41, 41], "The current native menu bar must be the observed root")
        precondition(!ObservedPath.matches([], root: 10, same: ==, children: { _ in [] }))
        precondition(!ObservedPath.matches([10], root: 10, same: ==, children: { _ in [] }))
        precondition(!ObservedPath.matches([10, 20, 10], root: 10, same: ==,
            children: { $0 == 10 ? [20] : [10] }), "A cyclic path is not usable ancestry")

        enum Unavailable: Error { case children }
        do {
            _ = try ObservedPath.matches(selected, root: 10, same: ==,
                children: { _ in throw Unavailable.children })
            preconditionFailure("A failed native read must not be treated as successful membership")
        } catch Unavailable.children { }
        print("PASS: stable observed menu identities survive duplicate titles/reordering; replacements and broken paths are rejected")
    }
}
