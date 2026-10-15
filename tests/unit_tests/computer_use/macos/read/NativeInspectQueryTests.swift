import Foundation

@main struct NativeInspectQueryTests {
    static func accepted(_ query: Any) -> Bool {
        do { _ = try NativeInspectQuery(query); return true }
        catch let error as DriverError {
            precondition(error.code == .invalidRequest, "malformed query must be an invalid request")
            return false
        }
        catch { preconditionFailure("unexpected decoder failure: \(error)") }
    }

    static func main() throws {
        if CommandLine.arguments.dropFirst().first == "--stdio" {
            // The real Python encoder supplies these frames during the native
            // test run. No desktop session or application objects are involved.
            while let line = readLine() {
                let query = try JSONSerialization.jsonObject(with: Data(line.utf8), options: [.fragmentsAllowed])
                print(accepted(query) ? "true" : "false")
            }
            return
        }
        let source = URL(fileURLWithPath: CommandLine.arguments[1])
        let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: source)) as! [[String: Any]]
        for test in cases {
            precondition(accepted(test["query"]!) == test["valid"] as! Bool, test["name"] as! String)
        }
        let within = (0..<17).map { "node-\($0)" }
        precondition(!accepted(["kind": "inputContext", "within": within]))
        if case .inputContext(let values, let actuator, _, _) = try NativeInspectQuery(["kind": "inputContext", "within": ["root", "group"]]) {
            precondition(values == ["root", "group"] && actuator == nil)
        } else { preconditionFailure("lost context query") }
        if case .inputContext(let values, let actuator, _, _) = try NativeInspectQuery(["kind": "inputContext", "within": [],
            "actuator": ["node": "editor", "relation": "containedEditor"]]) {
            precondition(values.isEmpty && actuator?.node == "editor")
        } else { preconditionFailure("lost selected editor") }
        let scrollBasis: [[String: Any]] = [["observed": "facts", "node": "region",
            "fields": ["AXRole"], "actions": false, "relations": []]]
        for hit in ["region", "passive-label"] {
            let raw: [String: Any] = ["node": hit, "region": "region", "relation": "scrollPointer"]
            let selected = try NativeInspectQuery.Actuator(raw)
            precondition(selected.node == hit && selected.region == "region" && selected.bar == nil)
            precondition(NSDictionary(dictionary: selected.wire).isEqual(to: raw))
            precondition(Set(selected.references) == Set([hit, "region"]))
            precondition(accepted(["kind": "inputContext", "within": [], "actuator": raw, "basis": scrollBasis]))
            precondition(!accepted(["kind": "inputContext", "within": [], "actuator": raw]))
            precondition(!accepted(["kind": "inputContext", "within": [], "actuator": raw, "basis": []]))
        }
        for selected in [
            ["node": "hit", "relation": "scrollPointer"],
            ["node": "hit", "region": "area", "relation": "scrollPointer", "bar": "bar"],
            ["node": "hit", "region": "area", "relation": "scrollPointer", "x": 10],
            ["node": "hit", "region": "", "relation": "scrollPointer"]
        ] as [[String: Any]] {
            precondition(!accepted(["kind": "inputContext", "within": [], "actuator": selected, "basis": scrollBasis]))
        }
        precondition(!accepted(["kind": "inputContext", "within": [],
            "actuator": ["node": "hit", "region": "area", "relation": "scrollPointer"],
            "receiver": ["relation": "containedFocus", "node": "focus"],
            "basis": [["observed": "facts", "node": "hit", "fields": ["AXRole"], "actions": false, "relations": []]]]))
        if case .capturedText(let ref, let offset, let length) = try NativeInspectQuery([
            "kind": "capturedText", "textRef": "captured-field", "offset": 3, "length": 4096]) {
            precondition(ref == "captured-field" && offset == 3 && length == 4096)
        } else { preconditionFailure("lost captured text range") }
        print("PASS \(cases.count) shared native inspection request contracts")
    }
}
