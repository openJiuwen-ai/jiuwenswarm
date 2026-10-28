import Foundation
import ApplicationServices

@main struct NativeTextSelectionTests {
    static func main() {
        func range(_ location: Int, _ length: Int) -> AXValue {
            var value = CFRange(location: location, length: length)
            return AXValueCreate(.cfRange, &value)!
        }
        var fields: [String: AXRead<CFTypeRef>] = [
            kAXNumberOfCharactersAttribute: .value(NSNumber(value: 7)),
            kAXSelectedTextRangeAttribute: .value(range(0, 7))
        ]
        func sample() -> NativeTextSelection? { .read { fields[$0] ?? .unsupported } }
        precondition(sample()?.full == true)
        fields[kAXSelectedTextRangeAttribute] = .value(range(2, 3))
        precondition(sample()?.full == false)
        for value in [range(-1, 1), range(0, -1), range(8, 0), range(0, Int.max)] {
            fields[kAXSelectedTextRangeAttribute] = .value(value)
            precondition(sample() == nil)
        }
        fields[kAXSelectedTextRangeAttribute] = .value(range(0, 7))
        for field in [kAXNumberOfCharactersAttribute, kAXSelectedTextRangeAttribute, kAXSelectedTextRangesAttribute] {
            let saved = fields[field]
            for state: AXRead<CFTypeRef> in [.invalid, .unavailable(.cannotComplete), .value("wrong type" as CFString)] {
                fields[field] = state
                precondition(sample() == nil, "unreadable \(field) must not establish full selection")
            }
            fields[field] = saved
        }
        for value: NSNumber in [true, -1, 1.5] {
            fields[kAXNumberOfCharactersAttribute] = .value(value)
            precondition(sample() == nil)
        }
        fields[kAXNumberOfCharactersAttribute] = .value(NSNumber(value: 7))
        for ranges in [[range(0, 7)], []] {
            fields[kAXSelectedTextRangesAttribute] = .value(ranges as CFArray)
            precondition(sample()?.full == true)
        }
        for ranges in [[range(0, 3), range(3, 4)], [range(1, 6)]] {
            fields[kAXSelectedTextRangesAttribute] = .value(ranges as CFArray)
            precondition(sample()?.full == false)
        }
        fields[kAXSelectedTextRangesAttribute] = nil
        var reads = 0
        let changing = NativeTextSelection.read { key in
            if key == kAXNumberOfCharactersAttribute {
                reads += 1
                return .value(NSNumber(value: reads == 1 ? 7 : 8))
            }
            return fields[key] ?? .unsupported
        }
        precondition(changing == nil && reads == 2)
        fields[kAXNumberOfCharactersAttribute] = .unsupported
        let text = "A😀e\u{301}"
        fields[kAXValueAttribute] = .value(text as CFString)
        fields[kAXSelectedTextRangeAttribute] = .value(range(0, text.utf16.count))
        precondition(sample()?.full == true && sample()?.count == 5)
        fields[kAXValueAttribute] = .value("" as CFString)
        fields[kAXSelectedTextRangeAttribute] = .value(range(0, 0))
        precondition(sample()?.full == true && sample()?.count == 0)
        print("PASS full text selection uses live UTF-16 extent; invalid, changing and multiple ranges are not full selection")
    }
}
