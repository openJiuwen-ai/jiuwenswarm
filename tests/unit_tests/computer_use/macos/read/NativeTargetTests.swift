import Foundation

@main struct NativeTargetTests {
    static func main() throws {
        let directory: [String: Any] = ["session": "s", "instance": NSNull(), "rootWindow": NSNull(),
            "surface": NSNull(), "node": NSNull(), "capture": NSNull()]
        let scopes: [[String: Any]] = [[:], ["instance": "app", "surface": "menu"],
            ["instance": "app", "rootWindow": "window", "surface": "surface"],
            ["instance": "app", "rootWindow": "window", "surface": "surface", "node": "element"],
            ["instance": "app", "rootWindow": "window", "surface": "surface", "capture": "image"],
            ["capture": "desktop-image"]]
        for scope in scopes {
            let wire = directory.merging(scope) { _, new in new }
            let target = try NativeTarget(wire)
            precondition(NSDictionary(dictionary: wire).isEqual(to: target.wire))
            precondition(target.isDiscovery == scope.isEmpty)
            try target.requireSession("s")
            do { try target.requireSession("different"); preconditionFailure() }
            catch { precondition((error as? DriverError)?.code == .staleTarget) }
        }
        for key in directory.keys {
            var missing = directory; missing.removeValue(forKey: key)
            refused(missing)
            for invalid: Any in [17, true, [], [:], " "] {
                var wire = directory; wire[key] = invalid; refused(wire)
            }
        }
        refused(directory.merging(["extra": "field"]) { _, new in new })
        refused(directory.merging(["session": NSNull()]) { _, new in new })
        // Canonicalizing one binding must not rewrite a retained input/image binding.
        let original = try NativeTarget(directory.merging(["node": "element"]) { _, new in new })
        var window = original; window.node = nil; window.rootWindow = "window"
        precondition(original.node == "element" && original.rootWindow == nil)
        precondition(window.wire["node"] is NSNull && window.rootWindow == "window")
        print("PASS target identity round trips, explicit nulls, session isolation and value-copy canonicalization")
    }

    static func refused(_ wire: [String: Any]) {
        do { _ = try NativeTarget(wire); preconditionFailure("malformed binding accepted: \(wire)") }
        catch { precondition((error as? DriverError)?.code == .invalidRequest) }
    }
}
