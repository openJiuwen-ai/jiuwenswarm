import Foundation
import ApplicationServices

@main struct AXFactEncodingTests {
    static func main() {
        let requested = ["AXRowCount", "AXIndex"]
        let advertised = AXFactEncoding.advertisedFieldFailures(requested, status: .success, names: ["AXRole", "AXRowCount"] as CFArray)
        precondition(advertised["AXRowCount"] == nil && advertised["AXIndex"]?["state"] as? String == "unsupported")
        let noAttributes = AXFactEncoding.advertisedFieldFailures(requested, status: .success, names: [] as CFArray)
        precondition(noAttributes.count == 2 && noAttributes.values.allSatisfy { $0["state"] as? String == "unsupported" })
        let unknownAttributes = AXFactEncoding.advertisedFieldFailures(requested, status: .cannotComplete, names: nil)
        precondition(unknownAttributes.count == 2 && unknownAttributes.values.allSatisfy { $0["state"] as? String == "error" })
        let malformedAttributes = AXFactEncoding.advertisedFieldFailures(requested, status: .success, names: ["AXRowCount", 3] as CFArray)
        precondition(malformedAttributes.count == 2 && malformedAttributes.values.allSatisfy { $0["state"] as? String == "error" })
        let absent = AXFactEncoding.relationFailure(source: "root", property: "AXParent", error: .noValue)
        let unsupported = AXFactEncoding.relationFailure(source: "root", property: "AXChildren", error: .attributeUnsupported)
        let failed = AXFactEncoding.relationFailure(source: "root", property: "AXChildren", error: .cannotComplete)
        let timedOut = AXFactEncoding.relationFailure(source: "root", property: "AXChildren")
        precondition(absent["state"] as? String == "absent" && absent["total"] as? Int == 0)
        precondition(unsupported["state"] as? String == "unsupported")
        precondition(failed["state"] as? String == "error" && timedOut["state"] as? String == "timeout")
        precondition(AXFactEncoding.actions(.success, nil)["state"] as? String == "unavailable")
        precondition(AXFactEncoding.actions(.success, [] as CFArray)["state"] as? String == "complete")
        let malformed = AXFactEncoding.actions(.success, ["AXPress", "", 1] as CFArray)
        precondition(malformed["state"] as? String == "partial" && malformed["total"] as? Int == 3)
        precondition((malformed["items"] as? [[String: Any]])?.count == 1)
        let large = AXFactEncoding.actions(.success, Array(repeating: "AXPress", count: 130) as CFArray)
        precondition(large["state"] as? String == "partial" && large["total"] as? Int == 130)
        precondition((large["items"] as? [[String: Any]])?.count == 128)
        let good = AXFactEncoding.description(.success, "" as CFString)
        precondition(good["description"] as? String == "" && good["descriptionStatus"] == nil)
        precondition((AXFactEncoding.description(.noValue, nil)["descriptionStatus"] as? [String: Any])?["state"] as? String == "absent")
        precondition((AXFactEncoding.description(.success, NSNumber(value: 3))["descriptionStatus"] as? [String: Any])?["state"] as? String == "error")
        precondition(AXFactEncoding.scalar(kCFBooleanFalse).value as? Bool == false)
        precondition(AXFactEncoding.scalar(NSNumber(value: 0)).value as? Int == 0)
        precondition(AXFactEncoding.scalar(NSNumber(value: Double.infinity)).failure?["state"] as? String == "error")
        for time in [0.0, -0.25, 813_922_555.1234567] {
            let date = CFDateCreate(kCFAllocatorDefault, time)!
            let encoded = AXFactEncoding.scalar(date)
            let value = encoded.value as! [String: Any]
            precondition(value["nativeType"] as? String == "CFDate" && value["absoluteTime"] as? Double == time)
            precondition(encoded.failure == nil && JSONSerialization.isValidJSONObject(value))
        }
        precondition(AXFactEncoding.scalar(CFDateCreate(kCFAllocatorDefault, .infinity)!).failure != nil)
        let element = AXUIElementCreateApplication(getpid())
        precondition(AXFactEncoding.scalar(element).failure != nil, "An unregistered object must not become a pointer string")
        precondition(AXFactEncoding.scalar(element, elementReference: "node-7").value as? [String: String]
                     == ["nativeType": "AXUIElement", "node": "node-7"])
        var nativeError = AXError.attributeUnsupported
        let raw = AXValueCreate(.axError, &nativeError)!
        precondition(AXFactEncoding.scalar(raw).failure?["state"] as? String == "unsupported")
        // The same raw CFRange encoding carries text and table spans. Native
        // acquisition must not convert indices to display numbers or erase zero.
        var span = CFRange(location: 2, length: 3)
        precondition(AXFactEncoding.scalar(AXValueCreate(.cfRange, &span)!).value as? [String: Int]
                     == ["location": 2, "length": 3])
        span = CFRange(location: 4, length: 0)
        precondition(AXFactEncoding.scalar(AXValueCreate(.cfRange, &span)!).value as? [String: Int]
                     == ["location": 4, "length": 0])
        span = CFRange(location: -1, length: 1)
        precondition(AXFactEncoding.scalar(AXValueCreate(.cfRange, &span)!).failure?["state"] as? String == "error")
        let description = AXFactEncoding.description(.success, String(repeating: "🙂", count: 9000) as CFString)
        precondition((description["description"] as? String)?.unicodeScalars.count == 4096)
        precondition((description["descriptionStatus"] as? [String: Any])?["totalUnits"] as? Int == 18000)
        print("PASS: AX field, relation, catalogue and action-description coverage remain distinct")
    }
}
