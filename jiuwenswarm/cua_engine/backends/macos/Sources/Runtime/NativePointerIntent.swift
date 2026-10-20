import Foundation

/// Only the original pointer intent is accepted. Preparation cannot replace
/// its coordinates, turn a click into AXPress, or silently request a new image.
struct NativePointerIntent {
    enum Kind: String { case click, scroll, drag }
    let kind: Kind
    // nil means this is an addressed Element click/scroll. Its current position is
    // resolved natively, never supplied by Python or borrowed from an image.
    let start: CGPoint?
    let end: CGPoint?
    let button: String
    let count: Int
    let direction: String?
    let pages: Double?

    // Internal foreground desktop adapter. The public SDK keeps its existing
    // click-count and left-drag contract; no extra model API is introduced.
    init(desktopKind: Kind, button: String, count: Int = 1) throws {
        guard ["left", "right", "middle"].contains(button), (1...3).contains(count) else {
            throw DriverError("invalid desktop pointer button/count", code: .invalidRequest)
        }
        kind = desktopKind; self.button = button; self.count = count
        start = nil; end = nil; direction = nil; pages = nil
    }

    init(unit: String, kind: String, parameters: Any?, foreground: Bool, element: Bool = false) throws {
        guard let kind = Kind(rawValue: kind), ["prepare_receiver", "pointer_sequence"].contains(unit),
              !foreground || unit == "prepare_receiver" else {
            throw DriverError("invalid pointer unit", code: .invalidRequest)
        }
        self.kind = kind
        guard !element || kind == .click || kind == .scroll else {
            throw DriverError("element pointer input supports click and scroll", code: .invalidRequest)
        }
        let p: [String: Any]
        switch kind {
        case .click:
            let keys = Set((parameters as? [String: Any] ?? [:]).keys)
            let coordinates: Set<String> = element ? [] : ["x", "y"]
            guard keys.isSubset(of: coordinates.union(["button", "count"])), keys.isSuperset(of: coordinates) else {
                throw DriverError(element ? "element click accepts only button/count" : "pointer click requires coordinates and optional button/count", code: .invalidRequest)
            }
            p = try object(parameters, keys: keys)
            button = p["button"] == nil ? "left" : try string(p["button"], label: "button")
            let n = p["count"] == nil ? 1 : try number(p["count"], label: "count")
            guard ["left", "right", "middle"].contains(button), n == 1 || n == 2 else {
                throw DriverError("invalid pointer button/count", code: .invalidRequest)
            }
            count = Int(n); direction = nil; pages = nil
        case .scroll:
            if element {
                let hasPages = (parameters as? [String: Any])?.keys.contains("pages") == true
                p = try object(parameters, keys: hasPages ? ["direction", "pages"] : ["direction"])
            } else {
                p = try object(parameters, keys: ["x", "y", "direction", "pages"])
            }
            let direction = try string(p["direction"], label: "direction")
            let pages = try number(p["pages"] ?? 1, label: "pages")
            guard ["up", "down", "left", "right"].contains(direction), pages > 0 else {
                throw DriverError("invalid pointer scroll direction/pages", code: .invalidRequest)
            }
            self.direction = direction; self.pages = pages; button = "left"; count = 1
        case .drag:
            p = try object(parameters, keys: ["fromX", "fromY", "toX", "toY"])
            button = "left"; count = 1; direction = nil; pages = nil
        }
        func point(_ x: String, _ y: String) throws -> CGPoint {
            let x = try number(p[x], label: x), y = try number(p[y], label: y)
            guard x >= 0, y >= 0 else { throw DriverError("pointer coordinates must be nonnegative", code: .invalidRequest) }
            return CGPoint(x: x, y: y)
        }
        start = try element ? nil : kind == .drag ? point("fromX", "fromY") : point("x", "y")
        end = try kind == .drag ? point("toX", "toY") : nil
        if kind == .drag, end == start { throw DriverError("drag endpoints must differ", code: .invalidRequest) }
    }
}
