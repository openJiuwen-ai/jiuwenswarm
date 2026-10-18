import Foundation

/// Parameters of the selected discrete page unit. Fractional pages and
/// coordinate scrolling belong to a different primitive; never round them.
struct NativeScrollIntent {
    let direction: String
    let pages: Int

    init(unit: String, kind: String, parameters: Any?, foreground: Bool) throws {
        guard unit == "native_scroll", kind == "scroll", !foreground,
              let p = parameters as? [String: Any], Set(p.keys).isSubset(of: ["direction", "pages"]) else {
            throw DriverError("native scroll requires a discrete page intent", code: .invalidRequest)
        }
        direction = try string(p["direction"], label: "direction")
        let amount = try number(p["pages"] ?? 1, label: "pages")
        guard ["up", "down", "left", "right"].contains(direction), amount > 0,
              amount <= 9_007_199_254_740_991, let pages = Int(exactly: amount) else {
            throw DriverError("native page scrolling requires positive integral pages", code: .invalidRequest)
        }
        self.pages = pages
    }
}
