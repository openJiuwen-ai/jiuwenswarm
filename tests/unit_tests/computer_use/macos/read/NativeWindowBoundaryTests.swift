import Foundation

@main struct NativeWindowBoundaryTests {
    static func main() throws {
        typealias W = AppSelection.Window
        let root = W(ref: "root", parent: nil, modal: false), other = W(ref: "other", parent: nil, modal: false)
        let panel = W(ref: "panel", parent: "root", modal: false), sheet = W(ref: "sheet", parent: "root", modal: true)
        let second = W(ref: "second", parent: "root", modal: true), nested = W(ref: "nested", parent: "sheet", modal: true)
        let global = W(ref: "global", parent: nil, modal: true)
        func blocked(_ target: String, _ windows: [W]) throws -> [String] {
            try NativeWindowBoundary.blocked(root: "root", target: target, windows: windows)
        }
        // A fixed native root remains usable without any AX window catalogue.
        for (target, windows, expected) in [
            ("root", [], []), ("panel", [root, panel, other], []),
            ("root", [root, sheet, second], ["second", "sheet"]),
            ("sheet", [root, sheet, nested], ["nested"]), ("nested", [root, sheet, nested], []),
            ("root", [root, global], ["global"]),
            ("root", [root, other, W(ref: "other-sheet", parent: "other", modal: true)], [])
        ] {
            let actual = try blocked(target, windows)
            precondition(actual == expected)
        }
        // Selecting a nested modal as a working root must not make its outer
        // app-modal panel a blocker. This is the NSOpenPanel -> Go To case,
        // independent of titles, roles or the application creating the panel.
        let inner = W(ref: "inner", parent: "global", modal: true)
        let leaf = W(ref: "leaf", parent: "inner", modal: false)
        let deeper = W(ref: "deeper", parent: "inner", modal: true)
        for (selectedRoot, target, windows, expected) in [
            ("inner", "inner", [root, global, inner], []),
            ("inner", "leaf", [root, global, inner, leaf], []),
            ("inner", "inner", [root, global, inner, deeper], ["deeper"]),
            ("global", "global", [root, global, inner], ["inner"])
        ] {
            let actual = try NativeWindowBoundary.blocked(root: selectedRoot, target: target, windows: windows)
            precondition(actual == expected)
        }
        // Default-view ambiguity is not permission to interact with a document
        // or its menu while two independent app-modal windows are present.
        let another = W(ref: "another", parent: nil, modal: true)
        let ambiguous = try blocked("root", [root, global, another])
        precondition(ambiguous == ["another", "global"])
        for windows in [[root], [root, W(ref: "panel", parent: "other", modal: false), other],
                        [root, W(ref: "panel", parent: "cycle", modal: false), W(ref: "cycle", parent: "panel", modal: false)]] {
            do { _ = try blocked("panel", windows); preconditionFailure("unconfirmed child scope admitted") }
            catch let error as DriverError { precondition(error.code == .staleTarget) }
        }
        do { _ = try blocked("root", [root, root]); preconditionFailure("ambiguous catalogue admitted") }
        catch let error as DriverError { precondition(error.code == .inputUnavailable) }
        print("PASS native input scope: missing AX, related panel, explicit nested roots, multiple modals, changed ancestry and ambiguity")
    }
}
