import Foundation
import CoreGraphics

/// A single-window query can omit an existing ordered-out/auxiliary window.
/// Absence from that query is not evidence that its lifetime has ended.
enum WindowServerInfo {
    static func window(_ id: CGWindowID,
                       query: (CGWindowListOption, CGWindowID) -> [[String: Any]] = {
                           CGWindowListCopyWindowInfo($0, $1) as? [[String: Any]] ?? []
                       }) -> [String: Any]? {
        func matching(_ rows: [[String: Any]]) -> [String: Any]? {
            rows.first { ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == id }
        }
        // Preserve a conflicting direct result for the caller to reject. Do
        // not search by title, geometry or PID for a replacement window.
        return matching(query(.optionIncludingWindow, id))
            ?? matching(query(.optionAll, kCGNullWindowID))
    }
}
