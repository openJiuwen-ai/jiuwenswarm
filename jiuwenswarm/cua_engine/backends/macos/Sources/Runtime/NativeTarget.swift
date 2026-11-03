import Foundation

/// Wire identity only. Decoding never resolves a native object, samples focus,
/// or authorizes input; readers and input units retain their live checks.
struct NativeTarget {
    let session: String
    let instance: String?
    var rootWindow: String?
    let surface: String?
    var node: String?
    let capture: String?

    init(_ raw: Any?) throws {
        let value = try object(raw, keys: ["session", "instance", "rootWindow", "surface", "node", "capture"])
        session = try string(value["session"], label: "session")
        func reference(_ key: String) throws -> String? {
            value[key] is NSNull ? nil : try string(value[key], label: key)
        }
        instance = try reference("instance"); rootWindow = try reference("rootWindow")
        surface = try reference("surface"); node = try reference("node"); capture = try reference("capture")
    }

    var wire: [String: Any] {
        ["session": session, "instance": instance as Any? ?? NSNull(), "rootWindow": rootWindow as Any? ?? NSNull(),
         "surface": surface as Any? ?? NSNull(), "node": node as Any? ?? NSNull(), "capture": capture as Any? ?? NSNull()]
    }

    var isDiscovery: Bool { instance == nil && rootWindow == nil && surface == nil && node == nil && capture == nil }
    var isDesktopImage: Bool { surface == nil && capture != nil }

    func requireSession(_ expected: String) throws {
        guard session == expected else { throw DriverError("target belongs to another session", code: .staleTarget) }
    }
}
