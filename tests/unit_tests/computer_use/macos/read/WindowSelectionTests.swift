import Foundation
import CoreGraphics

@main struct WindowSelectionTests {
    static func main() {
        func sheetDocument(_ target: Int, _ roots: [Int], _ graph: [Int: [Int]], unavailable: Int? = nil) -> Int? {
            WindowSelection.sheetDocument(target, roots: roots, same: ==,
                sheets: { $0 == unavailable ? nil : graph[$0, default: []] }, withinBudget: { true })
        }
        precondition(sheetDocument(3, [1, 2], [1: [3]]) == 1)
        precondition(sheetDocument(4, [1, 2, 3], [1: [3], 3: [4]]) == 1,
                     "Nested sheets keep their root document even if AXWindows repeats an intermediate sheet")
        precondition(sheetDocument(2, [1, 2], [1: [3]]) == 2, "Unrelated documents remain separate")
        precondition(sheetDocument(3, [1, 2], [1: [3], 2: [3]]) == nil, "Conflicting owners are not guessed")
        precondition(sheetDocument(3, [1], [1: [3], 3: [1]]) == nil, "Cycles do not establish ownership")
        precondition(sheetDocument(3, [1, 2], [1: [3]], unavailable: 2) == nil, "Incomplete catalogues cannot establish unique ownership")
        precondition(sheetDocument(3, [1, 2], [:]) == nil, "Focused but unowned sheets are not ordinary documents")
        precondition(sheetDocument(34, [1], Dictionary(uniqueKeysWithValues: (1..<34).map { ($0, [$0 + 1]) })) == nil,
                     "Excessive graphs stop within a bounded native query")
        precondition(WindowSelection.sheetDocument(1, roots: [1], same: ==, sheets: { _ in [] }, withinBudget: { false }) == nil)
        let document = CGRect(x: 75, y: 88, width: 819, height: 576)
        let dialog = CGRect(x: 190, y: 241, width: 644, height: 199)
        let old = WindowSelection.Candidate(id: 1, frame: document.offsetBy(dx: 27, dy: -22), title: "Book.xlsx", visible: false)
        let current = WindowSelection.Candidate(id: 2, frame: document, title: "Book.xlsx", visible: true)
        let modal = WindowSelection.Candidate(id: 3, frame: dialog, title: "", visible: true)
        let all = [old, current, modal]
        precondition(WindowSelection.select(all, hint: .init(frame: document, title: "Book.xlsx")) == 2)
        precondition(WindowSelection.select(all, hint: .init(frame: dialog, title: "")) == 3)
        precondition(WindowSelection.select([old, current], hint: .init(frame: dialog, title: "")) == nil,
                     "An uncapturable focused modal must not fall back to its document")
        let duplicate = WindowSelection.Candidate(id: 4, frame: document, title: "Book.xlsx", visible: true)
        precondition(WindowSelection.select([current, duplicate], hint: .init(frame: document, title: "Book.xlsx")) == nil)
        let hiddenTwin = WindowSelection.Candidate(id: 6, frame: document, title: "Book.xlsx", visible: false)
        precondition(WindowSelection.select([current, hiddenTwin], hint: .init(frame: document, title: "Book.xlsx")) == 2)
        precondition(WindowSelection.select([current, hiddenTwin], hint: .init(frame: document, title: "Book.xlsx"), requireUnique: true) == nil,
                     "An explicit AX window must not capture its same-title visible twin")
        precondition(WindowSelection.select([current, hiddenTwin], hint: .init(frame: document, title: "Book.xlsx", id: 6), requireUnique: true) == 6,
                     "A native ID identifies the selected hidden twin without switching to the visible window")
        precondition(WindowSelection.select([current, hiddenTwin], hint: .init(frame: document, title: "Book.xlsx", id: 99), requireUnique: true) == nil,
                     "A missing native ID must not fall back to an identical old window")
        precondition(WindowSelection.select([current], hint: .init(frame: document, title: "New native title", id: 2), requireUnique: true) == 2,
                     "WindowServer titles can lag behind AX; exact identity remains authoritative")
        precondition(WindowSelection.select([current], hint: .init(frame: dialog, title: "Book.xlsx", id: 2), requireUnique: true) == nil,
                     "Even an exact ID cannot reuse coordinates from a different frame")
        precondition(WindowSelection.geometryConflict([current], hint: .init(frame: dialog, title: "Book.xlsx", id: 2))?.id == 2,
                     "A uniquely identified window with inconsistent frames needs a geometry diagnostic, not identity ambiguity")
        precondition(WindowSelection.geometryConflict([current], hint: .init(frame: document, title: "Book.xlsx", id: 2)) == nil)
        precondition(WindowSelection.geometryConflict([current], hint: .init(frame: dialog, title: "Book.xlsx", id: 99)) == nil,
                     "An absent native ID is not a geometry mismatch")
        precondition(WindowSelection.geometryConflict([current], hint: .init(frame: dialog, title: "Book.xlsx")) == nil,
                     "Without an exact ID, conflicting frames cannot prove window identity")
        precondition(WindowSelection.geometryConflict([current, current], hint: .init(frame: dialog, title: "Book.xlsx", id: 2)) == nil,
                     "A duplicate native identity must not be called unique")
        precondition(WindowSelection.identifies(current, hint: .init(frame: dialog, title: "Book.xlsx", id: 2)),
                     "The same ID with changed geometry is one window in the catalogue, not valid old coordinates")
        precondition(WindowSelection.identifies(current, hint: .init(frame: nil, title: "", id: 2)),
                     "Missing AX geometry does not erase a confirmed native identity")
        precondition(WindowSelection.identifies(current, hint: .init(frame: document, title: "Renamed", id: 2)),
                     "Mutable titles do not replace exact native identity")
        precondition(!WindowSelection.identifies(current, hint: .init(frame: document, title: "Book.xlsx", id: 4)),
                     "Different native windows do not merge even with identical names and geometry")
        precondition(WindowSelection.identifies(current, hint: .init(frame: document, title: "Book.xlsx")))
        precondition(!WindowSelection.identifies(current, hint: .init(frame: dialog, title: "Book.xlsx")),
                     "Without native identity, same title cannot resolve conflicting geometry")
        precondition(!WindowSelection.identifies(current, hint: .init(frame: nil, title: "Book.xlsx")),
                     "A name alone is not evidence of identity")
        precondition(!WindowSelection.identifies(current, hint: .init(frame: document, title: "Other.xlsx")))
        precondition(WindowSelection.identifies(current, hint: .init(frame: document, title: "")))
        let otherBook = WindowSelection.Candidate(id: 5, frame: document, title: "Other.xlsx", visible: true)
        precondition(WindowSelection.select([otherBook, current], hint: .init(frame: document, title: "Book.xlsx")) == 2,
                     "Same-size stacked documents must use the observed title")
        precondition(WindowSelection.select([otherBook], hint: .init(frame: document, title: "Book.xlsx")) == nil,
                     "A stale document with matching geometry must not replace the focused document")
        precondition(WindowSelection.select(all, hint: nil) == nil)
        precondition(WindowSelection.select([old], hint: .init(frame: old.frame, title: "Book.xlsx")) == 1,
                     "Background/offscreen windows remain capturable")
        // The dialog is still AXFocusedWindow, but the focused palette control's
        // parent chain stops at a group reachable from a separate native window.
        let graph = [10: [11], 11: [12], 20: [21], 21: [22], 22: [23], 30: [31], 31: [30]]
        var calls = 0
        func containing(_ ancestry: [Int], windows: [Int], budget: Int = 100) -> Int? {
            calls = 0
            return WindowSelection.containing(ancestry, in: windows, same: ==, isWindow: { [10, 20, 30].contains($0) },
                children: { graph[$0] ?? [] }, withinBudget: { calls += 1; return calls <= budget })
        }
        precondition(containing([23, 22], windows: [10, 20]) == 20)
        precondition(containing([12], windows: [10, 20]) == 10)
        precondition(containing([99], windows: [10, 20]) == nil, "Do not infer a window when focus cannot be found")
        precondition(containing([99], windows: [30]) == nil && calls < 10, "Parent/child cycles must be bounded")
        precondition(containing([23], windows: [10, 20], budget: 1) == nil)
        precondition(containing([23, 22], windows: [20, 10]) == 20, "Window list ordering does not choose the target")
        // Missing reverse links in a busy application must not require reading
        // unrelated documents when the operation already identifies its window.
        let busyWindows = Array(1...12)
        var busy = [Int: [Int]]()
        for window in busyWindows { busy[window] = Array((window * 1000)..<(window * 1000 + 60)) }
        busy[12000] = [90000]; busy[90000] = [90001]
        func member(_ windows: [Int], _ target: Int) -> Int? {
            WindowSelection.containing([target], in: windows, same: ==, isWindow: { busyWindows.contains($0) },
                children: { busy[$0] ?? [] }, withinBudget: { true })
        }
        precondition(member(busyWindows, 90001) == nil, "Global membership query exhausts its node ceiling")
        precondition(member([12], 90001) == 12, "Scoped query verifies actual forward-child identity")
        precondition(member([11], 90001) == nil, "A requested window is not itself evidence of membership")
        precondition(member([12], 99999) == nil, "An absent target cannot be fabricated by scoping")
        // Forward-only child links must retain the nearest window/sheet boundary,
        // rather than attributing a popup's focus to its enclosing document.
        let nested = [10: [11, 20], 20: [21], 21: [22]]
        func nestedOwner(_ target: Int, roots: [Int]) -> Int? {
            WindowSelection.containing([target], in: roots, same: ==, isWindow: { [10, 20].contains($0) },
                children: { nested[$0] ?? [] }, withinBudget: { true })
        }
        precondition(nestedOwner(22, roots: [10]) == 20, "A descendant sheet owns its control despite missing reverse links")
        precondition(nestedOwner(22, roots: [20]) == 20, "The sheet remains a valid direct search root")
        precondition(nestedOwner(20, roots: [10]) == 20, "A nested window owns itself")
        precondition(nestedOwner(11, roots: [10]) == 10, "The document still owns its ordinary descendants")
        // A service and presenter expose distinct roots for the very same
        // native child objects. Similar labels or partial overlap are insufficient.
        let hosted = [10: [1, 2], 20: [1, 2], 30: [1, 3], 40: [1], 50: [1, 2], 60: [Int](), 70: [2, 1]]
        func host(_ remote: Int, _ candidates: [Int]) -> Int? {
            WindowSelection.contentHost(remote, candidates: candidates, children: { hosted[$0] }, same: ==)
        }
        precondition(host(10, [20, 30, 40]) == 20)
        precondition(host(10, [30, 40, 70]) == nil, "Partial overlap and reordered content do not prove a host")
        precondition(host(10, [20, 50]) == nil, "Two presenters are ambiguous")
        precondition(host(60, [60]) == nil && host(99, [20]) == nil, "Empty/unavailable children prove no relationship")
        let untitled = WindowSelection.Candidate(id: 99, frame: dialog, title: "", visible: true)
        precondition(WindowSelection.discoverable(untitled, alpha: 1), "Untitled visible popups are discoverable without AX")
        precondition(WindowSelection.discoverable(untitled, alpha: nil), "Unknown alpha is not proof of invisibility")
        precondition(!WindowSelection.discoverable(untitled, alpha: 0), "Fully transparent backing windows have no visual content")
        precondition(!WindowSelection.discoverable(old, alpha: 1), "A title does not distinguish hidden backing buffers from closed windows")
        precondition(!WindowSelection.discoverable(.init(id: 99, frame: dialog, title: "", visible: false), alpha: 1))
        precondition(!WindowSelection.discoverable(.init(id: 99, frame: .zero, title: "Empty", visible: true), alpha: 1))
        precondition(!WindowSelection.discoverable(.init(id: 99, frame: .init(x: 0, y: 0, width: Double.infinity, height: 20), title: "Invalid", visible: true), alpha: 1))
        print("PASS: window selection, catalogue identity and scoped containment")
    }
}
