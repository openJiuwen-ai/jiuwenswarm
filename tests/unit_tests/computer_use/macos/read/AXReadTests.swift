import ApplicationServices

@main struct AXReadTests {
    static func main() {
        guard case .value(false) = AXRead<Bool>.result(.success, false),
              case .unsupported = AXRead<Bool>.result(.attributeUnsupported, false),
              case .unavailable(.cannotComplete) = AXRead<Bool>.result(.cannotComplete, false),
              case .invalid = AXRead<Bool>.result(.invalidUIElement, false),
              case .unavailable(.noValue) = AXRead<String>.result(.success, nil) else { fatalError("AX result distinctions lost") }
        precondition(AXRead<Bool>.result(.cannotComplete, false).value == nil)
        print("PASS: unavailable, unsupported, invalid and explicit false AX results remain distinct")
    }
}
