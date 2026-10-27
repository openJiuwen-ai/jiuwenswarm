import AppKit
import ApplicationServices
import ImageIO

/// Adapter for Agent Core's existing foreground DesktopBackend contract.
/// Shares discovery, captures, identity checks and paired input with protocol 5;
/// only --cua-backend sessions can call it. Never changes the negotiated mode.
@MainActor final class CuaDesktopBackend {
    struct Target {
        let window: NativeWindows.Window
        let geometry: DesktopGeometry
        let pixels: CGRect
    }
    let state: NativeDesktopState
    private var known = [CGWindowID: NativeWindows.Window]()
    private var targets = [String: Target]()
    init(state: NativeDesktopState) { self.state = state }

    private func catalogue() throws -> [[String: Any]] {
        guard let rows = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            throw DriverError("native window catalogue unavailable", code: .desktopUnavailable)
        }
        let geometry = try DesktopCapture.geometry()
        let pids = Set(rows.compactMap { ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value })
        var found = [CGWindowID: NativeWindows.Window]()
        for pid in pids {
            guard let app = NSRunningApplication(processIdentifier: pid) else { continue }
            for win in NativeWindows.listIfAvailable(app, catalog: rows) ?? [] { found[win.mapping.id] = win }
        }
        known = found
        return rows.enumerated().compactMap { index, row in
            guard let id = (row[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let win = found[id], !AgentPointer.isDecoration(row) else { return nil }
            let rect = pixelRect(win.mapping.frame, geometry)
            let native = try? ForegroundAssist.nativeWindow(win.mapping.pid, id)
            let minimized = native.flatMap { AXAccess.read($0, kAXMinimizedAttribute).value as? Bool } ?? false
            return ["window_id": Int(id), "pid": Int(win.mapping.pid),
                "app_name": win.application.localizedName ?? "", "title": win.mapping.title ?? "",
                "x": Int(rect.minX), "y": Int(rect.minY), "width": Int(rect.width), "height": Int(rect.height),
                "is_on_screen": win.onScreen, "minimized": minimized, "z_index": index]
        }
    }

    private func pixelRect(_ frame: CGRect, _ geometry: DesktopGeometry) -> CGRect {
        let rect = geometry.rect(frame)
        return CGRect(x: floor(rect.minX), y: floor(rect.minY), width: ceil(rect.width), height: ceil(rect.height))
    }

    private func validate(_ target: Target, geometry: Bool = true) throws {
        let win = target.window
        guard NativeProcessIdentity.read(win.process.pid) == win.process,
              let row = WindowServerInfo.window(win.mapping.id),
              (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == win.mapping.pid,
              let raw = row[kCGWindowBounds as String] as? [String: Any],
              let frame = CGRect(dictionaryRepresentation: raw as CFDictionary),
              !geometry || frame == win.mapping.frame else {
            throw DriverError("selected window changed or disappeared; no replacement window was chosen", code: .staleTarget)
        }
        guard try DesktopCapture.geometry() == target.geometry else {
            throw DriverError("display layout changed; observe the window again", code: .staleTarget)
        }
    }

    private func bind(_ id: CGWindowID, activate: Bool) async throws -> [String: Any] {
        guard let window = known[id] else {
            throw DriverError("window was not observed in this session; list_windows first", code: .staleTarget)
        }
        let geometry = try DesktopCapture.geometry()
        let initial = Target(window: window, geometry: geometry, pixels: pixelRect(window.mapping.frame, geometry))
        try validate(initial)
        if activate {
            try Input.requireDesktopInput()
            let native = try ForegroundAssist.nativeWindow(window.mapping.pid, id)
            if let native,
               AXAccess.read(native, kAXMinimizedAttribute).value as? Bool == true {
                state.recordEffect()
                guard AXUIElementSetAttributeValue(native, kAXMinimizedAttribute as CFString, kCFBooleanFalse) == .success else {
                    throw DriverError("could not restore the minimized window", code: .inputUnavailable)
                }
            }
            // Non-key panels can receive pointer input while their document
            // remains the keyboard window. Keyboard dispatch checks separately.
            let pointer = NativePointerUnit(application: window.application,
                window: window.mapping, nativeWindow: native)
            let center = CGPoint(x: window.mapping.frame.midX, y: window.mapping.frame.midY)
            try await ForegroundAssist().prepare(BackgroundTarget(window.application), window: id,
                pointerAt: pointer.foregroundPreparationPoint(at: center),
                onAction: { self.state.recordEffect() })
            _ = try catalogue()
        }
        guard let current = known[id], current.process == window.process else {
            throw DriverError("window owner changed during preparation", code: .staleTarget)
        }
        let token = UUID().uuidString
        if targets.count >= 64 { targets.removeAll() }
        targets[token] = Target(window: current, geometry: geometry, pixels: pixelRect(current.mapping.frame, geometry))
        return ["target": token]
    }

    func perform(_ value: Any?) async throws -> [String: Any] {
        let p = try object(value, keys: ["operation", "arguments", "target"])
        let name = try string(p["operation"], label: "operation")
        guard let args = p["arguments"] as? [String: Any] else { throw DriverError("invalid desktop arguments", code: .invalidRequest) }
        let target: Target?
        if p["target"] is NSNull { target = nil }
        else {
            guard let token = p["target"] as? String, let selected = targets[token] else {
                throw DriverError("desktop target expired; observe again", code: .staleTarget)
            }
            target = selected
        }
        try BackgroundTarget.validateDesktop()
        switch name {
        case "windows": return ["windows": try catalogue()]
        case "screen":
            let g = try DesktopCapture.geometry()
            return ["width": g.width, "height": g.height]
        case "foreground":
            return ["window_id": DesktopCapture.destination()?.window.map { Int($0) } as Any? ?? NSNull()]
        case "cursor":
            guard let location = CGEvent(source: nil)?.location else { throw DriverError("cursor unavailable") }
            let rect = try DesktopCapture.geometry().rect(CGRect(origin: location, size: .zero))
            return ["x": rect.minX, "y": rect.minY]
        case "bind", "activate":
            let id = try number(args["window_id"], label: "window_id")
            guard id > 0, id <= Double(UInt32.max), id.rounded() == id else { throw DriverError("invalid window_id", code: .invalidRequest) }
            return try await bind(CGWindowID(id), activate: name == "activate")
        case "screenshot": return try await screenshot(args, target)
        case "launch": return try await launch(args)
        case "text", "key": try await keyboard(name, args, target)
        case "click", "move", "drag", "scroll": try await pointer(name, args, target)
        default: throw DriverError("unknown desktop operation", code: .invalidRequest)
        }
        return ["dispatched": true]
    }

    private func image(_ png: String) throws -> CGImage {
        guard let bytes = Data(base64Encoded: png), let source = CGImageSourceCreateWithData(bytes as CFData, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { throw DriverError("invalid captured image") }
        return image
    }

    private func screenshot(_ args: [String: Any], _ target: Target?) async throws -> [String: Any] {
        let picture: CGImage
        let width: Int, height: Int
        if let target {
            try validate(target)
            let result = try await WindowCapture(sharing: state.sharing).captureWindow(target.window.application,
                nativeTarget: target.window.mapping)
            let full = try image(result.image.png)
            guard let region = args["region"] as? [Double], region.count == 4, region.allSatisfy(\.isFinite),
                  region[2] > 0, region[3] > 0 else { throw DriverError("invalid screenshot region", code: .invalidRequest) }
            let local = CGRect(x: region[0] - target.pixels.minX, y: region[1] - target.pixels.minY,
                width: region[2], height: region[3])
            guard CGRect(origin: .zero, size: target.pixels.size).contains(local) else {
                throw DriverError("screenshot region is outside the selected window", code: .invalidRequest)
            }
            let crop = CGRect(x: local.minX * Double(full.width) / target.pixels.width,
                y: local.minY * Double(full.height) / target.pixels.height,
                width: local.width * Double(full.width) / target.pixels.width,
                height: local.height * Double(full.height) / target.pixels.height)
            guard let cropped = full.cropping(to: crop) else { throw DriverError("cannot crop window image") }
            picture = cropped; width = Int(region[2]); height = Int(region[3])
            try validate(target)
        } else {
            guard args["region"] is NSNull else { throw DriverError("select a window before a regional screenshot", code: .invalidRequest) }
            let result = try await state.desktop.read()
            guard let screenshot = result["screenshot"] as? [String: Any], let png = screenshot["png"] as? String else {
                throw DriverError("desktop screenshot unavailable", code: .desktopUnavailable)
            }
            picture = try image(png); width = picture.width; height = picture.height
        }
        guard width > 0, height > 0, let canvas = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
            throw DriverError("cannot allocate desktop image")
        }
        canvas.draw(picture, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let result = canvas.makeImage() else { throw DriverError("cannot finalize desktop image") }
        return ["png": try CapturedImage.encodePNG(result)]
    }

    private func checkInput(_ target: Target?, keyboard: Bool = false, geometry: Bool = true) throws {
        try Input.requireDesktopInput()
        if let target {
            try validate(target, geometry: geometry)
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == target.window.mapping.pid else {
                throw DriverError("foreground recipient changed; no input was redirected", code: .inputUnavailable)
            }
            if keyboard {
                let destination = DesktopCapture.destination()
                guard destination?.process == target.window.process,
                      destination?.focusedWindow == nil || destination?.focusedWindow == target.window.mapping.id else {
                    throw DriverError("keyboard recipient is another window", code: .inputUnavailable)
                }
            }
        }
    }

    private func keyboard(_ name: String, _ args: [String: Any], _ target: Target?) async throws {
        let value = try string(args[name == "key" ? "key" : "text"], label: name)
        if name == "key" { try Input.validateKey(value) }
        else { try validateText(value.replacingOccurrences(of: "\n", with: "").replacingOccurrences(of: "\r", with: "").replacingOccurrences(of: "\t", with: "")) }
        try checkInput(target, keyboard: true); try BackgroundTarget.validateReleasedInput()
        guard let recipient = DesktopCapture.destination() else { throw DriverError("keyboard recipient unavailable", code: .inputUnavailable) }
        func check() throws { try checkInput(target, keyboard: true); try recipient.validate(DesktopCapture.destination()) }
        func key(_ chord: String) async throws {
            try await Input.preparedKey(chord, recipient: nil, validate: check,
                onDispatch: { self.state.recordEffect() }, onPosted: {}, onCleanupUnknown: {})
        }
        if name == "key" { try await key(value); return }
        var run = ""
        for char in value.replacingOccurrences(of: "\r\n", with: "\n") {
            if char == "\n" || char == "\r" || char == "\t" {
                if !run.isEmpty {
                    try await Input.preparedText(run, recipient: nil, applicationPID: target?.window.mapping.pid,
                        validate: check, onDispatch: { self.state.recordEffect() }, onPosted: {}, onCleanupUnknown: {})
                    run = ""
                }
                try await key(char == "\t" ? "Tab" : "Return")
            } else { run.append(char) }
        }
        if !run.isEmpty {
            try await Input.preparedText(run, recipient: nil, applicationPID: target?.window.mapping.pid,
                validate: check, onDispatch: { self.state.recordEffect() }, onPosted: {}, onCleanupUnknown: {})
        }
    }

    private func pointer(_ name: String, _ args: [String: Any], _ target: Target?) async throws {
        try checkInput(target); try BackgroundTarget.validateReleasedInput()
        let geometry = try DesktopCapture.geometry()
        func point(_ x: Double, _ y: Double) throws -> CGPoint {
            if let target {
                return CGPoint(x: target.window.mapping.frame.minX + (x - target.pixels.minX) * target.window.mapping.frame.width / target.pixels.width,
                    y: target.window.mapping.frame.minY + (y - target.pixels.minY) * target.window.mapping.frame.height / target.pixels.height)
            }
            return try geometry.point(x: x, y: y, captured: Set(geometry.displays.map(\.id)))
        }
        let start: CGPoint
        var end: CGPoint?
        if name == "drag" {
            guard let from = args["start"] as? [Double], let to = args["end"] as? [Double], from.count == 2, to.count == 2 else { throw DriverError("invalid drag endpoints", code: .invalidRequest) }
            start = try point(from[0], from[1]); end = try point(to[0], to[1])
        } else if let x = args["x"] as? NSNumber, let y = args["y"] as? NSNumber {
            start = try point(x.doubleValue, y.doubleValue)
        } else if name == "scroll", let location = CGEvent(source: nil)?.location { start = location }
        else { throw DriverError("pointer needs finite coordinates", code: .invalidRequest) }
        try geometry.validatePath(from: start, to: end ?? start, captured: Set(geometry.displays.map(\.id)))
        let buttonName = args["button"] as? String ?? "left"
        let button: CGMouseButton = buttonName == "right" ? .right : buttonName == "middle" ? .center : .left
        func check(_ at: CGPoint, _ held: Bool) throws {
            try checkInput(target, geometry: !(name == "drag" && held))
            try BackgroundTarget.validateReleasedInput(allowing: held ? button : nil)
            if let target, !held {
                guard target.window.mapping.frame.contains(at), BackgroundEvents.inputWindow(at: at) == target.window.mapping.id else {
                    throw DriverError("pointer target is outside or covered by another window", code: .inputUnavailable)
                }
            }
        }
        let source = try Input.source()
        if name == "move" || name == "scroll" {
            try check(start, false)
            let event: CGEvent?
            if name == "move" { event = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: start, mouseButton: .left) }
            else {
                let direction = try string(args["direction"], label: "direction")
                let ticks = try number(args["ticks"], label: "ticks")
                guard ["up", "down", "left", "right"].contains(direction), ticks.rounded() == ticks, abs(ticks) <= 10000 else { throw DriverError("invalid wheel ticks", code: .invalidRequest) }
                let delta = Int32(ticks) * (["down", "right"].contains(direction) ? -1 : 1)
                event = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2,
                    wheel1: ["up", "down"].contains(direction) ? delta : 0,
                    wheel2: ["left", "right"].contains(direction) ? delta : 0, wheel3: 0)
            }
            guard let event else { throw DriverError("cannot allocate pointer event") }
            event.location = start
            if name == "scroll" {
                guard let move = CGEvent(mouseEventSource: source, mouseType: .mouseMoved,
                    mouseCursorPosition: start, mouseButton: .left) else { throw DriverError("cannot allocate wheel location event") }
                state.recordEffect(); move.post(tap: .cghidEventTap)
                try await Task.sleep(for: .milliseconds(20))
                try check(start, false)
            }
            state.recordEffect(); event.post(tap: .cghidEventTap)
            try await Task.sleep(for: .milliseconds(30))
            return
        }
        let count = try number(args["count"] ?? 1, label: "count")
        let duration = try number(args["duration"] ?? 0.5, label: "duration")
        guard count.rounded() == count, (1...3).contains(count), (0...10).contains(duration) else { throw DriverError("invalid count or drag duration", code: .invalidRequest) }
        let request = try NativePointerIntent(desktopKind: name == "drag" ? .drag : .click, button: buttonName, count: Int(count))
        let events = try NativePointerEvents(request: request, from: start, to: end,
            viewport: geometry.bounds.size, source: source, durationMs: Int(duration * 1000))
        try await events.deliver(check: check, post: { event, at in event.location = at; event.post(tap: .cghidEventTap) },
            canRelease: { true }, skipped: {}, onDispatch: { self.state.recordEffect() }, onPosted: {})
    }

    private func launch(_ args: [String: Any]) async throws -> [String: Any] {
        let label = try string(args["path"] ?? args["bundle_id"] ?? args["name"], label: "application")
        guard args["aumid"] == nil else { throw DriverError("aumid is Windows-only", code: .invalidRequest) }
        let url: URL?
        if label.hasPrefix("/") { url = URL(fileURLWithPath: label) }
        else {
            let entries = state.discovery.nativeApplications(continuing: { !Task.isCancelled })
            let paths = Set(entries.compactMap { item -> String? in
                guard (item["identifier"] as? String) == label
                    || (item["displayName"] as? String)?.caseInsensitiveCompare(label) == .orderedSame else { return nil }
                return item["path"] as? String
            })
            guard paths.count <= 1 else { throw DriverError("application name is ambiguous; use its exact bundle path", code: .ambiguousTarget) }
            url = paths.first.map { URL(fileURLWithPath: $0) }
        }
        guard let selected = url, selected.pathExtension.lowercased() == "app",
              FileManager.default.fileExists(atPath: selected.path) else {
            throw DriverError("application not found; use its bundle_id or absolute .app path", code: .invalidRequest)
        }
        let installation = try NativeInstallation(path: selected.path)
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.allowsRunningApplicationSubstitution = false
        guard args["additional_arguments"] == nil || args["additional_arguments"] is [String] else { throw DriverError("invalid launch arguments", code: .invalidRequest) }
        configuration.arguments = args["additional_arguments"] as? [String] ?? []
        try installation.validate()
        state.recordEffect()
        let application = try await NSWorkspace.shared.openApplication(at: selected, configuration: configuration)
        guard application.bundleURL?.standardizedFileURL.resolvingSymlinksInPath().path == installation.url.path else {
            throw DriverError("LaunchServices returned a different application", code: .staleTarget)
        }
        try await ApplicationDiscovery.waitUntilReady(BackgroundTarget(application), isFinishedLaunching: { application.isFinishedLaunching })
        let deadline = ProcessInfo.processInfo.systemUptime + 10
        var rows = [[String: Any]]()
        repeat {
            rows = try catalogue().filter { row in
                guard let id = row["window_id"] as? Int, let win = known[CGWindowID(id)] else { return false }
                return win.mapping.pid == application.processIdentifier || NativeWindows.belongs(win.process, to: application)
            }
            if !rows.isEmpty { break }
            try await Task.sleep(for: .milliseconds(100))
        } while ProcessInfo.processInfo.systemUptime < deadline
        return ["pid": Int(application.processIdentifier), "windows": rows]
    }
}
