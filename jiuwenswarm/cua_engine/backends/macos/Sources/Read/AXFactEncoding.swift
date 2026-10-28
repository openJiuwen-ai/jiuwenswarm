import Foundation
import ApplicationServices

/// Encode AX query results without mistaking malformed data for an empty UI.
/// Provider failures belong to their field/branch, not the whole protocol frame.
enum AXFactEncoding {
    /// A successful value getter may still be an AppKit default for an
    /// undeclared attribute. Retain the declaration result separately from a
    /// legitimate zero/empty value; a failed catalogue is unknown, not absent.
    static func advertisedFieldFailures(_ fields: [String], status: AXError, names: CFArray?) -> [String: [String: Any]] {
        guard status == .success, let names = names as? [String], !names.contains(where: { $0.isEmpty }) else {
            return Dictionary(uniqueKeysWithValues: fields.map {
                ($0, ["state": "error", "detail": "native attribute catalogue unavailable (AXError \(status.rawValue))"])
            })
        }
        let declared = Set(names)
        return Dictionary(uniqueKeysWithValues: fields.filter { !declared.contains($0) }.map {
            ($0, ["state": "unsupported", "detail": "not advertised by the accessibility provider"])
        })
    }

    static func status(_ error: AXError) -> [String: Any] {
        let state: String
        switch error {
        case .attributeUnsupported, .actionUnsupported, .notImplemented: state = "unsupported"
        case .noValue: state = "absent"
        default: state = "error"
        }
        return ["state": state, "detail": "AXError \(error.rawValue)"]
    }

    static func scalar(_ value: CFTypeRef?, elementReference: String? = nil) -> (value: Any?, failure: [String: Any]?) {
        guard let value else { return (nil, ["state": "absent"]) }
        let type = CFGetTypeID(value)
        if type == CFStringGetTypeID() { return (value as! String, nil) }
        if type == CFBooleanGetTypeID() { return ((value as! NSNumber).boolValue, nil) }
        if type == CFNumberGetTypeID(), (value as! NSNumber).doubleValue.isFinite { return (value, nil) }
        if type == CFURLGetTypeID() { return ((value as! URL).absoluteString, nil) }
        // AXValue is not necessarily text/a number. Preserve CFDate's native
        // epoch and precision; localization belongs to the Python presentation.
        if type == CFDateGetTypeID() {
            let seconds = CFDateGetAbsoluteTime(unsafeDowncast(value, to: CFDate.self))
            if seconds.isFinite { return (["nativeType": "CFDate", "absoluteTime": seconds], nil) }
        }
        // Radio groups, for example, return the selected AX object. Never
        // stringify its address or borrow its label in place of its identity.
        if type == AXUIElementGetTypeID(), let elementReference, !elementReference.isEmpty {
            return (["nativeType": "AXUIElement", "node": elementReference], nil)
        }
        if type == AXValueGetTypeID() {
            let raw = unsafeDowncast(value, to: AXValue.self)
            if AXValueGetType(raw) == .axError {
                var error = AXError.failure
                if AXValueGetValue(raw, .axError, &error) { return (nil, status(error)) }
            }
            if AXValueGetType(raw) == .cfRange {
                var range = CFRange()
                if AXValueGetValue(raw, .cfRange, &range), range.location >= 0, range.length >= 0 {
                    return (["location": range.location, "length": range.length], nil)
                }
            }
        }
        return (nil, ["state": "error", "detail": "native property type is not representable in this field profile"])
    }

    static func actions(_ error: AXError, _ raw: CFArray?) -> [String: Any] {
        guard error == .success, let values = raw as? [Any] else {
            return ["state": "unavailable", "items": [[String: Any]](),
                    "detail": error == .success ? "provider returned no action array" : "AXError \(error.rawValue)"]
        }
        var items = [[String: Any]](), invalid = [String]()
        for (index, value) in values.prefix(128).enumerated() {
            guard let name = value as? String, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  name.utf16.count <= 4096 else { invalid.append("\(index)"); continue }
            items.append(["name": name, "descriptionStatus": ["state": "timeout", "detail": "description not yet queried"]])
        }
        var result: [String: Any] = ["state": values.count == items.count ? "complete" : "partial", "items": items, "total": values.count]
        var detail = [String]()
        if !invalid.isEmpty { detail.append("unusable action name at native indices \(invalid.joined(separator: ","))") }
        if values.count > 128 { detail.append("action count budget") }
        if !detail.isEmpty { result["detail"] = detail.joined(separator: "; ") }
        return result
    }

    static func description(_ error: AXError, _ value: CFTypeRef?) -> [String: Any] {
        guard error == .success else { return ["descriptionStatus": status(error)] }
        guard let value else { return ["descriptionStatus": ["state": "absent"]] }
        guard CFGetTypeID(value) == CFStringGetTypeID() else {
            return ["descriptionStatus": ["state": "error", "detail": "provider returned a non-string action description"]]
        }
        let text = value as! String
        if text.utf16.count > 16_384 {
            return ["description": String(text.unicodeScalars.prefix(4096)),
                    "descriptionStatus": ["state": "truncated", "totalUnits": text.utf16.count, "detail": "action description preview only"]]
        }
        return ["description": text]
    }

    static func relationFailure(source: String, property: String, error: AXError? = nil) -> [String: Any] {
        let failure = error.map(status) ?? ["state": "timeout", "detail": "collection time budget"]
        var result: [String: Any] = ["source": source, "property": property, "targets": [String](),
                                    "certainty": "unknown", "state": failure["state"]!, "detail": failure["detail"]!]
        if failure["state"] as? String == "absent" { result["certainty"] = "confirmed"; result["total"] = 0 }
        return result
    }
}
