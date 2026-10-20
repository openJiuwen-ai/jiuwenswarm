import AppKit

@MainActor final class EventsView: NSView {
    var clicks = [Int](), rights = 0, middles = 0, drags = 0, ups = 0, wheels = 0
    override var acceptsFirstResponder: Bool { true }
    override func draw(_ dirtyRect: NSRect) { NSColor.systemBlue.setFill(); bounds.fill() }
    override func mouseDown(with event: NSEvent) { clicks.append(event.clickCount) }
    override func rightMouseDown(with event: NSEvent) { rights += 1 }
    override func otherMouseDown(with event: NSEvent) { middles += 1 }
    override func mouseDragged(with event: NSEvent) { drags += 1 }
    override func rightMouseDragged(with event: NSEvent) { drags += 1 }
    override func otherMouseDragged(with event: NSEvent) { drags += 1 }
    override func mouseUp(with event: NSEvent) { ups += 1 }
    override func rightMouseUp(with event: NSEvent) { ups += 1 }
    override func otherMouseUp(with event: NSEvent) { ups += 1 }
    override func scrollWheel(with event: NSEvent) { wheels += 1 }
}

@MainActor final class Fixture: NSObject, NSApplicationDelegate {
    let path: URL
    var windows = [NSWindow](), fields = [NSTextField](), boxes = [NSButton](), events = [EventsView]()
    var timer: Timer?
    init(path: String) { self.path = URL(fileURLWithPath: path) }
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu(), edit = NSMenu(title: "Edit")
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        menu.addItem(editItem); menu.setSubmenu(edit, for: editItem)
        NSApplication.shared.mainMenu = menu
        for index in 0..<2 {
            let window = NSWindow(contentRect: NSRect(x: 180 + index * 160, y: 180 + index * 80, width: 520, height: 340),
                styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "CUA Fixture \(index + 1)"
            window.isReleasedWhenClosed = false
            let field = NSTextField(frame: NSRect(x: 30, y: 230, width: 400, height: 30))
            field.setAccessibilityLabel("Fixture input \(index + 1)")
            let box = NSButton(checkboxWithTitle: "Enabled", target: self, action: #selector(changed(_:)))
            box.frame = NSRect(x: 30, y: 175, width: 150, height: 30)
            let surface = EventsView(frame: NSRect(x: 30, y: 40, width: 400, height: 90))
            window.contentView?.addSubview(field); window.contentView?.addSubview(box); window.contentView?.addSubview(surface)
            windows.append(window); fields.append(field); boxes.append(box); events.append(surface)
            window.makeKeyAndOrderFront(nil)
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
        record()
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.record() }
        }
    }
    @objc func changed(_ sender: NSControl) { record() }
    func record() {
        func point(_ view: NSView, _ window: NSWindow) -> [Double] {
            let x = view is NSButton ? view.frame.minX + 8 : view.frame.midX
            return [x, window.frame.height - view.frame.midY]
        }
        let rows: [[String: Any]] = windows.indices.map { index in
            let window = windows[index], surface = events[index]
            return ["id": window.windowNumber, "width": window.frame.width, "height": window.frame.height,
                "field": point(fields[index], window), "box": point(boxes[index], window),
                "surface": point(surface, window), "text": fields[index].stringValue,
                "checked": boxes[index].state == .on, "clicks": surface.clicks, "rights": surface.rights,
                "middles": surface.middles, "drags": surface.drags, "ups": surface.ups, "wheels": surface.wheels]
        }
        if let data = try? JSONSerialization.data(withJSONObject: ["pid": ProcessInfo.processInfo.processIdentifier, "windows": rows]) {
            try? data.write(to: path, options: .atomic)
        }
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let delegate = Fixture(path: CommandLine.arguments.last!)
    app.delegate = delegate
    app.run()
}
