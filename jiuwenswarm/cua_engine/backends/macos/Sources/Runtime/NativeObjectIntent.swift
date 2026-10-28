import Foundation

/// Wire validation for an already selected object primitive. Semantic action
/// selection belongs to Python; pointer requests cannot enter this endpoint.
struct NativeObjectIntent {
    let kind: String
    let value: String?

    init(unit: String, kind: String, parameters: Any?) throws {
        self.kind = kind
        let route = unit == "prepare_receiver" ? "native_action" : unit
        switch (route, kind) {
        case ("native_action", "action"):
            let p = try object(parameters, keys: ["action"])
            value = try string(p["action"], label: "action")
        case ("native_action", "click"):
            guard let p = parameters as? [String: Any], Set(p.keys).isSubset(of: ["button", "count"]) else {
                throw DriverError("native click only accepts normal primary-click options", code: .invalidRequest)
            }
            if let button = p["button"], try string(button, label: "button") != "left" {
                throw DriverError("non-primary clicks require a pointer unit", code: .invalidRequest)
            }
            if let count = p["count"], try number(count, label: "count") != 1 {
                throw DriverError("multiple clicks require a pointer unit", code: .invalidRequest)
            }
            value = nil
        case ("native_action", "openMenu"):
            _ = try object(parameters, keys: [])
            value = nil
        case ("native_text_edit", "replace"), ("native_text_edit", "insert"):
            let p = try object(parameters, keys: ["text"])
            let text = try string(p["text"], label: "text", nonempty: false)
            try validateText(text)
            value = text
        default:
            throw DriverError("native object unit cannot execute this intent", code: .invalidRequest)
        }
    }
}
