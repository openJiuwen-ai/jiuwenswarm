import AppKit
import ApplicationServices

/// Pixel captures and their immutable coordinate mappings are independent of
/// AX nodes and input receiver preparation.
@MainActor final class NativeCapture {
    struct Entry {
        let binding: [String: Any]
        let surface: NativeDirectory.Surface
        let screen: WindowCapture
        let mapping: WindowGeometry.WindowMapping
    }
    private let directory: NativeDirectory
    private let sharing: WindowSharing
    private var entries = [String: Entry]()
    private var order = [String]()

    init(directory: NativeDirectory, sharing: WindowSharing) {
        self.directory = directory; self.sharing = sharing
    }

    func retained(_ id: String) throws -> Entry {
        guard let entry = entries[id] else {
            throw DriverError("image coordinate mapping expired; capture this window again", code: .snapshotExpired)
        }
        return entry
    }

    func input(_ binding: [String: Any]) throws -> (entry: Entry, blocked: [String]) {
        let entry = try retained(string(binding["capture"], label: "capture"))
        guard NSDictionary(dictionary: entry.binding).isEqual(to: binding) else {
            throw DriverError("capture belongs to another input binding", code: .staleTarget)
        }
        let current = try directory.inputWindowScope(instance: string(binding["instance"], label: "instance"),
            root: string(binding["rootWindow"], label: "rootWindow"), surface: string(binding["surface"], label: "surface"))
        try directory.validateWindow(entry.surface)
        try entry.screen.validate(entry.mapping)
        return (entry, current.blocked)
    }

    func capture(binding: [String: Any], maxDimension: Int) async throws -> [String: Any] {
        let instance = try string(binding["instance"], label: "instance")
        let root = try string(binding["rootWindow"], label: "rootWindow")
        let scope = try string(binding["surface"], label: "surface")
        let interval = CaptureInterval()
        let selected = try directory.windowScope(instance: instance, root: root, surface: scope).surface
        let screen = WindowCapture(sharing: sharing)
        let native = selected.native
        let mapping: WindowGeometry.WindowMapping?
        if let native {
            // Refresh geometry for this new image only. Earlier captures keep
            // their own mapping, and are never repaired by this observation.
            guard let info = WindowServerInfo.window(native.mapping.id),
                  let raw = info[kCGWindowBounds as String] as? [String: Any],
                  let frame = CGRect(dictionaryRepresentation: raw as CFDictionary),
                  frame.minX.isFinite, frame.minY.isFinite, frame.width.isFinite, frame.height.isFinite,
                  frame.width > 1, frame.height > 1 else {
                throw DriverError("native window capture geometry is unavailable", code: .observationIncomplete)
            }
            mapping = .init(id: native.mapping.id, pid: native.process.pid, frame: frame,
                title: info[kCGWindowName as String] as? String,
                width: Int(frame.width.rounded(.up)), height: Int(frame.height.rounded(.up)))
        } else { mapping = nil }
        let captured = try await screen.captureWindow(native?.application ?? selected.owner,
            target: selected.element, nativeTarget: mapping, maxDimension: maxDimension)
        // Capture can await the system. Revalidate lifetimes before publishing
        // its pixels; a title/focus change is not a reason to change the target.
        _ = try directory.windowScope(instance: instance, root: root, surface: scope)
        try screen.validate(captured.window)
        try RequestDiagnostics.checkCancellation()
        var imageBinding = binding
        imageBinding["capture"] = captured.image.id
        entries[captured.image.id] = Entry(binding: imageBinding, surface: selected, screen: screen, mapping: captured.window)
        order.append(captured.image.id)
        while order.count > 64 { entries.removeValue(forKey: order.removeFirst()) }
        let frame = captured.window.frame
        return ["kind": "capture", "binding": binding, "observed": interval.observation(),
            "image": captured.image.protocolValue(kind: .window, scope: scope),
            "coordinates": ["space": "window-pixels", "origin": "top-left", "nativeSpace": "screen-points",
                "toNative": [frame.width / Double(captured.image.width), 0, 0,
                    frame.height / Double(captured.image.height), frame.minX, frame.minY]]]
    }
}
