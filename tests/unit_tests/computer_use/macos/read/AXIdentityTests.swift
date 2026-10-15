import Foundation
import ApplicationServices

@main struct AXIdentityTests {
    static func failure(_ error: AXError) -> AXValue {
        var error = error
        return AXValueCreate(.axError, &error)!
    }
    static func range(_ location: Int, _ length: Int) -> AXValue {
        var range = CFRange(location: location, length: length)
        return AXValueCreate(.cfRange, &range)!
    }
    static func main() {
        let keys = ["AXRole", "AXIdentifier", "AXTitle", "AXURL"]
        let data: [Any] = ["AXButton", failure(.cannotComplete), failure(.noValue), URL(string: "file:///test")!]
        let facts = AXIdentity.decode(.success, data as CFArray, keys: keys).value!
        precondition(facts.values == ["AXRole": "AXButton", "AXURL": "file:///test"])
        precondition(facts.absent == ["AXTitle"] && facts.values["AXIdentifier"] == nil)
        let failed = AXIdentity.decode(.cannotComplete, nil, keys: keys)
        if case .unavailable(.cannotComplete) = failed {} else { fatalError("messaging failure became identity change") }
        if case .invalid = AXIdentity.decode(.success, [failure(.invalidUIElement)] as CFArray, keys: ["AXRole"]) {}
        else { fatalError("destroyed native object was preserved as temporarily unreadable") }
        if case .unavailable = AXIdentity.decode(.success, ["AXButton"] as CFArray, keys: keys) {}
        else { fatalError("malformed native attribute batch accepted") }
        let unknown = AXIdentity.decode(.success, [12, failure(.failure)] as CFArray, keys: ["AXRole", "AXTitle"]).value!
        precondition(unknown.values.isEmpty && unknown.absent.isEmpty)
        for error in [AXError.attributeUnsupported, .noValue, .notImplemented] {
            let read = AXIdentity.decode(.success, [failure(error)] as CFArray, keys: ["AXTitle"]).value!
            precondition(read.absent == ["AXTitle"])
        }
        let positions = ["AXRole", "AXRowIndexRange", "AXColumnIndexRange"]
        let cell = AXIdentity.decode(.success, ["AXTextField", range(1, 1), range(7, 2)] as CFArray, keys: positions).value!
        precondition(cell.values == ["AXRole": "AXTextField", "AXRowIndexRange": "1:1", "AXColumnIndexRange": "7:2"])
        for invalid: Any in [range(-1, 1), range(0, 0), range(1, -1), range(Int.max, 1), range(Int.max - 1, 2), "1:1", 1] {
            let facts = AXIdentity.decode(.success, [invalid] as CFArray, keys: ["AXRowIndexRange"]).value!
            precondition(facts.values.isEmpty && facts.absent.isEmpty, "malformed or empty positions remain unknown")
        }
        let partial = AXIdentity.positionRequest(positions, status: .success, names: ["AXRole", "AXRowIndexRange"] as CFArray)
        precondition(partial.keys == ["AXRole", "AXRowIndexRange"] && partial.absent == ["AXColumnIndexRange"])
        for (status, names) in [(AXError.cannotComplete, nil), (.success, nil), (.success, [1] as CFArray?)] {
            let request = AXIdentity.positionRequest(positions, status: status, names: names)
            precondition(request.keys == ["AXRole"] && request.absent.isEmpty, "failed declaration reads cannot become absence")
        }
        print("PASS AX identity decoding: values, absent attributes, failed fields, invalid objects and malformed batches")
        print("PASS advertised native logical ranges: typed nonempty positions, merged cells and declaration failure")
    }
}
