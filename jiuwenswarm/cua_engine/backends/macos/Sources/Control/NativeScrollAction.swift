/// A page action is a provider-declared discrete operation. Increment and
/// decrement do not specify a page length; fractional pages need wheel input.
enum NativeScrollAction {
    struct PageButtonSpec: Equatable {
        let scrollBar: String
        let orientation: String
        let subrole: String
        let pages: Int
    }

    struct PageButtonCandidate {
        // IDs only deduplicate the same native object within this one read.
        let id: Int
        let role: String
        let subrole: String?
        let actions: [String]?
        let enabled: Bool?
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

    static func validatePageOwnership(processMatches: Bool, windowMatches: Bool?, parentMatches: Bool?) throws {
        guard processMatches else { throw DriverError("native scroll control belongs to another process or is unavailable", code: .staleTarget) }
        guard windowMatches != false else { throw DriverError("native scroll control belongs to another window", code: .staleTarget) }
        guard parentMatches != false else { throw DriverError("native scroll control parent conflicts with its standard relationship", code: .staleTarget) }
    }

    static func pageButton(_ candidates: [PageButtonCandidate], spec: PageButtonSpec) throws -> Int? {
        let matches = candidates.filter { $0.role == "AXButton" && $0.subrole == spec.subrole }
        guard Set(matches.map(\.id)).count <= 1 else {
            throw DriverError("native scroll bar has ambiguous page buttons; no further page action sent", code: .inputUnavailable)
        }
        guard let candidate = matches.first else { return nil }
        guard candidate.enabled != false else { throw DriverError("native page button is disabled", code: .inputUnavailable) }
        guard let actions = candidate.actions else {
            throw DriverError("native page button actions are unavailable; no further page action sent", code: .inputUnavailable)
        }
        return actions.contains("AXPress") ? candidate.id : nil
    }

    /// Resolve anew before every page: native scrollbar proxies may be replaced
    /// after a successful press. A failure never selects a different route.
    @MainActor
    static func performPages<T>(_ pages: Int, resolve: () throws -> T, dispatch: (T) throws -> Void) async throws {
        for _ in 0..<pages {
            await Task.yield()
            try Task.checkCancellation()
            let target = try resolve()
            try dispatch(target)
        }
    }
}
