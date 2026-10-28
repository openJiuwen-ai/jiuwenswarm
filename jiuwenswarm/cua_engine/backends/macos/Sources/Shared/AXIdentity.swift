import Foundation
import ApplicationServices

/// Preserve the difference between a missing native attribute and a failed
/// query. This encodes native evidence; it does not choose an input target.
enum AXIdentity {
    static func decode(_ status: AXError, _ raw: CFArray?, keys: [String]) -> AXRead<ElementIdentity.Facts> {
        guard status == .success else { return .result(status, nil) }
        guard let values = raw as? [Any], values.count == keys.count else { return .unavailable(.failure) }
        var facts = ElementIdentity.Facts()
        for (key, value) in zip(keys, values) {
            let raw = value as CFTypeRef
            let position = ElementIdentity.positionKeys.contains(key)
            if !position, CFGetTypeID(raw) == CFStringGetTypeID() { facts.values[key] = value as? String }
            else if !position, CFGetTypeID(raw) == CFURLGetTypeID() { facts.values[key] = (value as! URL).absoluteString }
            else if CFGetTypeID(raw) == AXValueGetTypeID() {
                let ax = unsafeDowncast(raw, to: AXValue.self)
                if position, AXValueGetType(ax) == .cfRange {
                    var range = CFRange(location: 0, length: 0)
                    if AXValueGetValue(ax, .cfRange, &range), range.location >= 0,
                       range.location != NSNotFound, range.length > 0,
                       range.location <= Int.max - range.length {
                        // Internal lossless equality evidence; rendering still
                        // receives the original typed range from the collector.
                        facts.values[key] = "\(range.location):\(range.length)"
                    }
                    continue
                }
                var error = AXError.failure
                if AXValueGetType(ax) == .axError && AXValueGetValue(ax, .axError, &error) {
                    if error == .invalidUIElement { return .invalid }
                    if [.attributeUnsupported, .notImplemented, .noValue].contains(error) { facts.absent.insert(key) }
                }
            }
            // A malformed scalar or cannotComplete leaves this field unknown.
        }
        return .value(facts)
    }

    /// AppKit may answer unadvertised range getters with default zero ranges.
    /// An absent declaration and a failed attribute-name query are different:
    /// only the former contradicts an earlier explicitly advertised position.
    static func positionRequest(_ keys: [String], status: AXError, names: CFArray?) -> (keys: [String], absent: Set<String>) {
        let positions = Set(keys).intersection(ElementIdentity.positionKeys)
        let declared = status == .success ? names as? [String] : nil
        let absent = declared.map { positions.subtracting($0) } ?? []
        return (keys.filter { !positions.contains($0) || declared?.contains($0) == true }, absent)
    }

    static func read(_ element: AXUIElement, keys: [String] = ElementIdentity.keys) -> AXRead<ElementIdentity.Facts> {
        guard !keys.isEmpty else { return .value(.init()) }
        var requested = keys
        var absent = Set<String>()
        if keys.contains(where: ElementIdentity.positionKeys.contains) {
            var names: CFArray?
            let status = AXUIElementCopyAttributeNames(element, &names)
            if status == .invalidUIElement { return .invalid }
            (requested, absent) = positionRequest(keys, status: status, names: names)
        }
        var raw: CFArray?
        let status = requested.isEmpty ? AXError.success
            : AXUIElementCopyMultipleAttributeValues(element, requested as CFArray, [], &raw)
        let decoded = requested.isEmpty ? AXRead<ElementIdentity.Facts>.value(.init()) : decode(status, raw, keys: requested)
        if case .value(var facts) = decoded {
            facts.absent.formUnion(absent)
            return .value(facts)
        }
        return decoded
    }
}
