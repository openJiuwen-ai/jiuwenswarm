import Foundation
import CoreGraphics

@main struct WindowServerInfoTests {
    static func main() {
        let wanted: CGWindowID = 42
        let own: [String: Any] = [kCGWindowNumber as String: wanted, kCGWindowOwnerPID as String: 123]
        let other: [String: Any] = [kCGWindowNumber as String: 99, kCGWindowOwnerPID as String: 123]
        let changed: [String: Any] = [kCGWindowNumber as String: wanted, kCGWindowOwnerPID as String: 456]
        var calls = 0
        let found = WindowServerInfo.window(wanted) { option, id in
            calls += 1
            if option == .optionIncludingWindow { assert(id == wanted); return [] }
            assert(option == .optionAll && id == kCGNullWindowID)
            return [other, own]
        }
        assert((found?[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == 123 && calls == 2)
        calls = 0
        let conflict = WindowServerInfo.window(wanted) { option, _ in
            calls += 1
            return option == .optionIncludingWindow ? [other, changed] : [own]
        }
        assert((conflict?[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == 456 && calls == 1)
        assert(WindowServerInfo.window(wanted, query: { _, _ in [other] }) == nil)
        assert(WindowServerInfo.window(wanted, query: { _, _ in [] }) == nil)
        print("PASS window catalogue: exact-ID fallback, missing windows, unrelated rows and owner conflicts remain distinguishable")
    }
}
