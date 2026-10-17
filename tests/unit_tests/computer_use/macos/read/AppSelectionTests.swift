import Foundation

@main struct AppSelectionTests {
    static func main() {
        typealias W = AppSelection.Window
        let a = W(ref: "1", parent: nil, modal: false), b = W(ref: "2", parent: nil, modal: false)
        let sheet = W(ref: "3", parent: "1", modal: true)
        precondition(AppSelection.current(root: "1", windows: [a, b], focused: "2") == "1")
        precondition(AppSelection.current(root: "1", windows: [a, b, sheet], focused: "2") == "3")
        precondition(AppSelection.current(root: "2", windows: [a, b, sheet], focused: "3") == "2")
        let nested = W(ref: "4", parent: "3", modal: true)
        precondition(AppSelection.current(root: "1", windows: [a, sheet, nested], focused: "1") == "4")
        let unknown = W(ref: "5", parent: nil, modal: false)
        precondition(AppSelection.current(root: "1", windows: [a, unknown], focused: "5") == "1")
        let global = W(ref: "6", parent: nil, modal: true)
        precondition(AppSelection.current(root: "1", windows: [a, b, global, W(ref: "7", parent: "6", modal: true)], focused: "1") == "7")
        precondition(AppSelection.current(root: "1", windows: [a, global, W(ref: "8", parent: nil, modal: true)], focused: "6") == "1")

        print("PASS App working-window and modal selection")
    }
}
