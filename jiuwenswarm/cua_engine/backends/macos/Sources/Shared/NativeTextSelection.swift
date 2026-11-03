import Foundation
import ApplicationServices

/// A complete selection belongs to the currently observed text extent.
/// No content string equality, selected row or caret/focus flag substitutes for
/// this check. Platform readers retain their own text-offset units.
struct NativeTextSelection {
    let count: Int
    let location: Int?
    let length: Int?
    var contiguous = true

    var full: Bool {
        count >= 0 && contiguous && location == 0 && length == count
    }

    /// Decode a live native sample. Keeping field acquisition explicit lets
    /// tests exercise provider errors and changes between reads without UI input.
    static func read(_ field: (String) -> AXRead<CFTypeRef>) -> Self? {
        func extent() -> Int? {
            switch field(kAXNumberOfCharactersAttribute) {
            case .value(let raw):
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      let count = Int(number.stringValue), count >= 0 else { return nil }
                return count
            case .unsupported, .unavailable(.noValue):
                // AppKit AX/NSRange uses UTF-16, not grapheme/scalar counts.
                return (field(kAXValueAttribute).value as? String).map { ($0 as NSString).length }
            default: return nil
            }
        }
        func range(_ raw: CFTypeRef) -> CFRange? {
            guard CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
            var result = CFRange()
            return AXValueGetValue(unsafeDowncast(raw, to: AXValue.self), .cfRange, &result) ? result : nil
        }
        guard let count = extent() else { return nil }
        let selection: CFRange?
        switch field(kAXSelectedTextRangeAttribute) {
        case .value(let raw):
            guard let value = range(raw), value.location >= 0, value.length >= 0,
                  value.location <= count, value.length <= count - value.location else { return nil }
            selection = value
        case .unsupported, .unavailable(.noValue): selection = nil
        default: return nil
        }
        var contiguous = true
        switch field(kAXSelectedTextRangesAttribute) {
        case .value(let raw):
            guard let ranges = raw as? [CFTypeRef] else { return nil }
            let decoded = ranges.compactMap(range)
            guard decoded.count == ranges.count else { return nil }
            contiguous = decoded.isEmpty || (decoded.count == 1 && decoded[0].location == selection?.location
                                             && decoded[0].length == selection?.length)
        case .unsupported, .unavailable(.noValue): break
        default: return nil
        }
        // A changing text extent cannot prove a complete selection. Equal
        // extent is not content equality and imposes no post-input comparison.
        guard extent() == count else { return nil }
        return Self(count: count, location: selection?.location, length: selection?.length, contiguous: contiguous)
    }
}
