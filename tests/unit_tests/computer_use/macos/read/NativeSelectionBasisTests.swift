import AppKit
import ApplicationServices

@main @MainActor struct NativeSelectionBasisTests {
    static func changed(_ expected: NativeSelectionBasis.Fact, _ actual: NativeSelectionBasis.Fact) {
        do { try NativeSelectionBasis.compare(expected, actual); preconditionFailure("changed dependency was accepted") }
        catch is NativeSelectionBasis.Changed { }
        catch { preconditionFailure("wrong structural change classification: \(error)") }
    }

    static func main() throws {
        let yes = try NativeSelectionBasis.scalar(value: true, status: nil)
        let one = try NativeSelectionBasis.scalar(value: 1, status: nil)
        changed(yes, one) // JSON/Foundation coercion must not equate native types.
        try NativeSelectionBasis.compare(yes, NativeSelectionBasis.scalar(value: true, status: nil))
        let unknown = try NativeSelectionBasis.scalar(value: nil, status: ["state": "error"])
        do { try NativeSelectionBasis.compare(yes, unknown); preconditionFailure("unreadable field accepted") }
        catch let error as DriverError { precondition(error.code == .observationIncomplete) }
        changed(unknown, yes)
        let original = try NativeSelectionBasis.actions(["state": "complete", "total": 2,
            "items": [["name": "AXPress", "description": "点击"], ["name": "AXShowMenu", "description": "菜单"]]])
        let translated = try NativeSelectionBasis.actions(["state": "complete", "total": 2,
            "items": [["name": "AXShowMenu", "description": "menu"], ["name": "AXPress", "description": "press"]]])
        try NativeSelectionBasis.compare(original, translated)
        changed(original, try NativeSelectionBasis.actions(["state": "partial", "total": 3,
            "items": [["name": "AXPress"], ["name": "AXShowMenu"]]]))
        let a = AXUIElementCreateApplication(1), b = AXUIElementCreateApplication(2)
        let first = NativeSelectionBasis.Fact(data: Data("{}".utf8), objects: [a, b])
        try NativeSelectionBasis.compare(first, NativeSelectionBasis.Fact(data: first.data,
            objects: [AXUIElementCreateApplication(1), AXUIElementCreateApplication(2)]))
        changed(first, NativeSelectionBasis.Fact(data: first.data, objects: [b, a]))
        changed(first, NativeSelectionBasis.Fact(data: first.data, objects: [a]))
        print("PASS selection fact types, unknown evidence, action identity, ordered native relationship objects")
    }
}
