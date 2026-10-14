import Foundation

/// Native names are opaque; descriptions are localized aliases, not commands
/// to translate, normalize, parse or approximate.
enum NativeAction {
    struct Item: Equatable {
        let name: String
        let description: String?
    }

    /// AXPerformAction and AXCopyActionDescription address a name, not an
    /// array entry. Keep one executable declaration per name; raw observation
    /// retains the provider's original array. An incomplete catalogue must not
    /// prevent independently observed pointer/text routes from being queried.
    static func boundedNames(_ names: [String], limit: Int = 128) -> [String]? {
        var seen = Set<String>(), result = [String]()
        for name in names where !name.isEmpty && seen.insert(name).inserted {
            guard result.count < limit else { return nil }
            result.append(name)
        }
        return result
    }

    static func resolve(_ requested: String, actions: [Item]?) throws -> String {
        guard !requested.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw DriverError("action must be a nonempty string", code: .invalidRequest)
        }
        guard let actions else {
            throw DriverError("native actions could not be read; no action sent", code: .inputUnavailable)
        }
        // An exact opaque name is authoritative, including when another
        // action's human-readable description happens to be that string.
        if actions.contains(where: { !$0.name.isEmpty && $0.name == requested }) { return requested }
        let matches = Set(actions.filter { !$0.name.isEmpty && $0.description == requested }.map(\.name))
        guard !matches.isEmpty else {
            throw DriverError("this element does not declare action \(requested.debugDescription); no action sent", code: .inputUnavailable,
                next: "read the element and copy one of its Secondary Actions exactly")
        }
        guard matches.count == 1 else {
            throw DriverError("action \(requested.debugDescription) is ambiguous; native names: \(matches.sorted().map(\.debugDescription).joined(separator: ", ")); no action sent",
                code: .ambiguousTarget, next: "use the intended exact native action name shown in the observation or this error")
        }
        return matches.first!
    }
}
