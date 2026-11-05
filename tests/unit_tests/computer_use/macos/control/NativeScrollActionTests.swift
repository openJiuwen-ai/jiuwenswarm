@main struct NativeScrollActionTests {
    @MainActor static func main() async throws {
        let directions = ["up", "down", "left", "right"]
        for direction in directions {
            let spec = NativeScrollAction.pageButtonSpec(direction: direction, pages: 2)!
            let vertical = direction == "up" || direction == "down"
            precondition(spec.scrollBar == (vertical ? "AXVerticalScrollBar" : "AXHorizontalScrollBar"))
            precondition(spec.orientation == (vertical ? "AXVerticalOrientation" : "AXHorizontalOrientation"))
            precondition(spec.subrole == (["up", "left"].contains(direction) ? "AXDecrementPage" : "AXIncrementPage"))
            precondition(spec.pages == 2)
        }
        for pages in [0, -1, 0.5, 1.5, Double.nan, Double.infinity, Double.greatestFiniteMagnitude] {
            precondition(NativeScrollAction.pageButtonSpec(direction: "down", pages: pages) == nil)
        }
        precondition(NativeScrollAction.pageButtonSpec(direction: "diagonal", pages: 1) == nil)
        let spec = NativeScrollAction.pageButtonSpec(direction: "down", pages: 3)!
        func rejected(_ operation: () throws -> Void) {
            do { try operation(); preconditionFailure("conflicting or unknown evidence must not select a fallback") }
            catch is DriverError {} catch { preconditionFailure("unexpected error: \(error)") }
        }
        try NativeScrollAction.validateScrollBar(role: "AXScrollBar", orientation: nil, enabled: nil, spec: spec)
        try NativeScrollAction.validateScrollBar(role: "AXScrollBar", orientation: "AXUnknownOrientation", enabled: true, spec: spec)
        rejected { try NativeScrollAction.validateScrollBar(role: "AXGroup", orientation: nil, enabled: true, spec: spec) }
        rejected { try NativeScrollAction.validateScrollBar(role: "AXScrollBar", orientation: "AXHorizontalOrientation", enabled: true, spec: spec) }
        rejected { try NativeScrollAction.validateScrollBar(role: "AXScrollBar", orientation: nil, enabled: false, spec: spec) }
        print("PASS standard page-button relationships, directions, fractions and scrollbar evidence")
    }
}
