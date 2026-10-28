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

}
