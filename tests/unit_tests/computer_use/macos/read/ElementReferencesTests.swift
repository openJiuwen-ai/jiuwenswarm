import Foundation

@main struct ElementReferencesTests {
    struct Identity: Hashable {
        let process: Int
        let launch: Int
        let element: Int
    }

    static func main() {
        let refs = ElementReferences<Identity>()
        let first = Identity(process: 1, launch: 1, element: 1)
        let second = Identity(process: 1, launch: 1, element: 2)
        precondition(refs.reference(for: first) == "0")
        precondition(refs.reference(for: second) == "1")
        // Reverse observation order and omit the first element for several reads.
        for _ in 0..<3 { precondition(refs.reference(for: second) == "1") }
        // Another process with the same native element identity gets a different number.
        let otherApp = Identity(process: 2, launch: 1, element: 1)
        precondition(refs.reference(for: otherApp) == "2")
        precondition(refs.reference(for: first) == "0")
        // Re-created controls and PID reuse after a restart are new identities.
        precondition(refs.reference(for: Identity(process: 1, launch: 1, element: 3)) == "3")
        precondition(refs.reference(for: Identity(process: 1, launch: 2, element: 1)) == "4")

        let numbers = ElementReferences<Int>()
        for index in 0..<100 {
            precondition(numbers.reference(for: index) == String(index))
        }
        for index in (0..<100).reversed() {
            precondition(numbers.reference(for: index) == String(index))
        }
        precondition(numbers.reference(for: 100) == "100")
        precondition(ElementReferences<Int>().reference(for: 999) == "0")
        let windows = ElementReferences<String>()
        let axFirst = windows.reference(for: "AX:first:launch1")
        precondition(windows.reference(for: "CG:first:launch1", aliases: ["AX:first:launch1"]) == axFirst,
            "Late WindowServer discovery preserves the previously exposed AX reference")
        let nativeFirst = windows.reference(for: "CG:second:launch1")
        _ = windows.reference(for: "AX:second:launch1")
        precondition(windows.reference(for: "CG:second:launch1", aliases: ["AX:second:launch1"]) == nativeFirst,
            "Restored AX content uses the observed native reference")
        precondition(windows.reference(for: "AX:second:launch1") == nativeFirst)
        let replacement = windows.reference(for: "CG:replacement:launch1", aliases: ["AX:second:launch1"])
        precondition(replacement != nativeFirst, "A recycled AX object must not transfer an old native window's reference")
        precondition(windows.reference(for: "CG:second:launch1") == nativeFirst)
        precondition(windows.reference(for: "AX:second:launch1") == replacement)
        precondition(windows.reference(for: "CG:first:launch2", aliases: ["AX:first:launch2"]) != axFirst,
            "A restarted process must not borrow the previous incarnation's aliases")
        print("PASS: stable decimal references, observation order, process identity and fresh sessions")
        print("PASS: late native discovery and restored AX keep established window references")
    }
}
