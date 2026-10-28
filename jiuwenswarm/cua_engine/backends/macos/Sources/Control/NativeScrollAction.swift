/// A page action is a provider-declared discrete operation. Increment and
/// decrement do not specify a page length; fractional pages need wheel input.
enum NativeScrollAction {
    struct PageButtonSpec: Equatable {
        let scrollBar: String
        let orientation: String
        let subrole: String
        let pages: Int
    }

    static func pageButtonSpec(direction: String, pages: Double) -> PageButtonSpec? {
        guard pages > 0, let count = Int(exactly: pages),
              ["up", "down", "left", "right"].contains(direction) else { return nil }
        let vertical = direction == "up" || direction == "down"
        return PageButtonSpec(scrollBar: vertical ? "AXVerticalScrollBar" : "AXHorizontalScrollBar",
            orientation: vertical ? "AXVerticalOrientation" : "AXHorizontalOrientation",
            subrole: direction == "down" || direction == "right" ? "AXIncrementPage" : "AXDecrementPage",
            pages: count)
    }

    static func validateScrollBar(role: String, orientation: String?, enabled: Bool?, spec: PageButtonSpec) throws {
        guard role == "AXScrollBar" else { throw DriverError("native scroll bar relationship has an unexpected role", code: .staleTarget) }
        if let orientation, ["AXHorizontalOrientation", "AXVerticalOrientation"].contains(orientation), orientation != spec.orientation {
            throw DriverError("native scroll bar orientation conflicts with its standard relationship", code: .staleTarget)
        }
        guard enabled != false else { throw DriverError("native scroll bar is disabled", code: .inputUnavailable) }
    }

}
