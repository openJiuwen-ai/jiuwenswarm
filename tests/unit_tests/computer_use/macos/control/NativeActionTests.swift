@main struct NativeActionTests {
    static func main() throws {
        precondition(NativeAction.boundedNames(["AXPress", "AXPress", "AXIncrement", "AXPress"])
                     == ["AXPress", "AXIncrement"])
        precondition(NativeAction.boundedNames(Array(repeating: "AXPress", count: 300)) == ["AXPress"])
        precondition(NativeAction.boundedNames(["", "AXPress", ""]) == ["AXPress"])
        precondition(NativeAction.boundedNames([]) == [])
        precondition(NativeAction.boundedNames((0..<128).map { "Action\($0)" })?.count == 128)
        precondition(NativeAction.boundedNames((0...128).map { "Action\($0)" }) == nil)
        print("PASS native action name deduplication and catalogue bounds")
    }
}
