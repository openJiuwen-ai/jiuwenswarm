import Foundation

/// Bounded, escaped labels in native diagnostics; UI projection belongs to Python.
enum DiagnosticText {
    static func bounded(_ text: String, limit: Int) -> String {
        guard text.count > limit else { return text }
        let head = limit / 4
        return String(text.prefix(head)) + "\n[… \(text.count - limit) characters omitted …]\n" + String(text.suffix(limit - head))
    }

    static func quoted(_ text: String) -> String {
        let data = try! JSONSerialization.data(withJSONObject: text, options: [.fragmentsAllowed, .withoutEscapingSlashes])
        return String(decoding: data, as: UTF8.self).unicodeScalars.map { scalar in
            switch scalar.value {
            case 127...159, 0x2028, 0x2029: return "\\u" + String(format: "%04x", scalar.value)
            default: return String(scalar)
            }
        }.joined()
    }
}
