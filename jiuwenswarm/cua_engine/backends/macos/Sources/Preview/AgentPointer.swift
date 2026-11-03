import AppKit
@preconcurrency import ApplicationServices
import QuartzCore

/// Pure decoration. No event posting, activation, AX setters, input validation,
/// sleeps in the caller, or callbacks into control. Slow/broken AX providers
/// are read on a separate queue; neither input nor observations await it.
@MainActor final class AgentPointer {
    @TaskLocal static var current: Activity?
    static let windowTitle = "Computer Use Agent Pointer · v1"
    private static var windowIDs = Set<CGWindowID>()
    private static var captureDepth = 0

    // Hiding only decoration also closes the race where its first window is
    // created after SCShareableContent was fetched. No capture wait/lease is
    // added; animation resumes on its next tick if the activity is still live.
    static func beginDesktopCapture() {
        captureDepth += 1
        for window in NSApplication.shared.windows where windowIDs.contains(CGWindowID(window.windowNumber)) {
            window.orderOut(nil)
        }
    }
    static func endDesktopCapture() { captureDepth -= 1 }

    /// Only our noninteractive marker windows are omitted, never the whole
    /// backend PID (its optional preview has real controls). Other instances
    /// must have both this reserved title and our exact executable identity.
    static func isDecoration(id: CGWindowID, pid: pid_t, title: String?) -> Bool {
        if pid == getpid() { return windowIDs.contains(id) }
        return title == windowTitle && NSRunningApplication(processIdentifier: pid)?.executableURL == Bundle.main.executableURL
    }
    static func isDecoration(_ info: [String: Any]) -> Bool {
        guard let id = (info[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
              let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value else { return false }
        return isDecoration(id: id, pid: pid, title: info[kCGWindowName as String] as? String)
    }

    @MainActor final class Activity {
        private weak var owner: AgentPointer?
        private let generation: UInt64
        private var semantic = false
        private var pulsed = false
        private var succeeded = true
        init(_ owner: AgentPointer, generation: UInt64) { self.owner = owner; self.generation = generation }

        func aim(element: AXUIElement?, window: WindowGeometry.WindowMapping?, pulse: Bool, count: Int = 1) {
            // The first explicit semantic target owns this operation. Later
            // editor/focus preparation is not a second user-facing interaction.
            guard !semantic, let element else { return }
            semantic = true
            owner?.set(.init(window: window, point: nil, element: element), generation: generation, pulses: pulse && !pulsed ? count : 0)
            pulsed = pulsed || pulse
        }

        func move(to point: CGPoint, window: WindowGeometry.WindowMapping?, pulse: Bool = false, count: Int = 1, dragging: Bool? = nil) {
            // Internal editor preparation must not replace the user's semantic
            // target with a hidden implementation control.
            guard !semantic else { return }
            owner?.set(.init(window: window, point: point, element: nil), generation: generation,
                pulses: pulse && !pulsed ? count : 0, dragging: dragging)
            pulsed = pulsed || pulse
        }

        func recordOutcome(success: Bool) { succeeded = success }
        func finish(success: Bool) { owner?.finish(generation: generation, success: success && succeeded) }
    }

    // AX references are retained immutable CF handles. Only the visual queue
    // reads geometry; it does not alter the handle's messaging timeout/state.
    private struct Target: @unchecked Sendable {
        let window: WindowGeometry.WindowMapping?
        let point: CGPoint?
        let element: AXUIElement?
        // A resting cursor belongs to the window, not a stale AX node or
        // screenshot. This offset is display-only and never authorizes input.
        var restingOffset: CGPoint? = nil
    }
    private struct Picture: Sendable {
        let frame: CGRect
        let point: CGPoint
        let level: Int
        let windowID: CGWindowID
        let pid: pid_t
    }
    private enum Resolution: Sendable { case resolved(Picture), unavailable, offscreen }
    private let enabled = ProcessInfo.processInfo.environment["COMPUTER_USE_POINTER"] != "0"
    private var closed = false
    private let queue = DispatchQueue(label: "computer-use.pointer-geometry", qos: .utility)
    private var generation: UInt64 = 0
    private var revision: UInt64 = 0
    private var target: Target?
    private var observedWindow: WindowGeometry.WindowMapping?
    // Window-local display positions only, never AX identities or input routes.
    private var anchors = [Target]()
    private var visualState = AgentPointerVisualState()
    private var active: Bool { visualState.active }
    private var busy = false
    private var pulsePending = 0
    private var dragging = false
    private var lastQuery: TimeInterval = 0
    private var lastPaint: TimeInterval = 0
    private var timer: Timer?
    private var panel: PointerPanel?
    private var appearance: AgentPointerAppearance?
    private var shownWindow: CGWindowID?
    private var shownPID: pid_t?

    func begin() -> Activity {
        generation &+= 1
        // A command boundary is not movement. Keep even a not-yet-painted
        // target; only set() or stop() invalidates its geometry resolution.
        visualState.begin(); pulsePending = 0; dragging = false
        appearance?.cancelFeedback()
        updateAppearance()
        return Activity(self, generation: generation)
    }

    /// Reads do not move an onscreen target. Keep the new observed surface so
    /// a dismissed/hidden transient cannot strand the marker forever.
    func observe(_ window: WindowGeometry.WindowMapping) {
        guard enabled, !closed else { return }
        observedWindow = window
        guard !active, target == nil else { return }
        revision &+= 1; pulsePending = 0
        panel?.orderOut(nil); appearance?.cancelFeedback()
        visualState.observeNewWindow(at: ProcessInfo.processInfo.systemUptime)
        target = Target(window: window, point: nil, element: nil,
            restingOffset: CGPoint(x: window.frame.width / 2, y: window.frame.height / 2))
        startTimer()
        tick()
    }

    private func rememberAnchor(_ target: Target) {
        guard let window = target.window, target.restingOffset != nil else { return }
        anchors.removeAll { $0.window?.id == window.id && $0.window?.pid == window.pid }
        anchors.append(target)
        if anchors.count > 32 { anchors.removeFirst() }
    }

    private func recoverObservedWindow() {
        guard let previous = target?.window else { return }
        guard !active, let window = observedWindow,
              window.id != previous.id || window.pid != previous.pid else { return }
        let offset = anchors.last { $0.window?.id == window.id && $0.window?.pid == window.pid }?.restingOffset
            ?? CGPoint(x: window.frame.width / 2, y: window.frame.height / 2)
        target = Target(window: window, point: nil, element: nil, restingOffset: offset)
        observedWindow = nil
        revision &+= 1; pulsePending = 0; appearance?.cancelFeedback()
        // Recovery is observation, not another operation or a click.
    }

    private func set(_ target: Target, generation: UInt64, pulses: Int, dragging: Bool? = nil) {
        guard enabled, !closed, self.generation == generation, active else { return }
        if self.target?.window?.id != target.window?.id || self.target?.window?.pid != target.window?.pid {
            panel?.orderOut(nil); appearance?.cancelFeedback()
        }
        var anchored = target
        if let point = target.point, let window = target.window {
            // Coordinates are already known at dispatch. Capture their local
            // offset now, without waiting for a display/AX callback.
            anchored.restingOffset = CGPoint(x: point.x - window.frame.minX, y: point.y - window.frame.minY)
        }
        self.target = anchored; observedWindow = nil
        revision &+= 1; pulsePending = max(pulsePending, pulses)
        rememberAnchor(anchored)
        if let dragging { self.dragging = dragging; updateAppearance() }
        startTimer()
        tick()
    }

    private func startTimer() {
        if timer == nil {
            let timer = Timer(timeInterval: 0.08, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.tick() }
            }
            RunLoop.main.add(timer, forMode: .common); self.timer = timer
        }
    }

    private func finish(generation: UInt64, success: Bool) {
        guard generation == self.generation else { return }
        visualState.finish(at: ProcessInfo.processInfo.systemUptime)
        dragging = false; updateAppearance()
        // Failure cancels feedback, not the attempted position. Do not move
        // back to an older control or discard unresolved geometry.
        if !success { pulsePending = 0; appearance?.cancelFeedback() }
        if target == nil { hide() }
    }

    /// A disconnected driver cannot display late observation/operation results.
    func shutdown() {
        closed = true
        stop()
    }

    func stop() {
        generation &+= 1; revision &+= 1; visualState = AgentPointerVisualState(); target = nil
        observedWindow = nil; anchors.removeAll()
        pulsePending = 0; dragging = false
        hide()
        appearance?.stop()
        if let panel { Self.windowIDs.remove(CGWindowID(panel.windowNumber)); panel.close() }
        panel = nil; appearance = nil; shownWindow = nil; shownPID = nil
    }

    private func hide() {
        panel?.orderOut(nil); appearance?.hide()
        timer?.invalidate(); timer = nil
    }

    private func updateAppearance() {
        appearance?.setReducedMotion(NSWorkspace.shared.accessibilityDisplayShouldReduceMotion)
        let idle = visualState.opacity(at: ProcessInfo.processInfo.systemUptime) < 1
        appearance?.setState(dragging ? .dragging : idle ? .idle : .active)
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        guard target != nil else { hide(); return }
        // Idle opacity is independent of AX latency, capture/occlusion and
        // window motion. None of those count as a new agent operation.
        updateAppearance()
        // A hung geometry provider must not freeze a stale overlay over the
        // user's next window. Also coalesce dense drag events to <= 12.5 Hz;
        // Core Animation renders movement independently at display refresh.
        if busy, now - lastPaint > 0.3 { panel?.orderOut(nil) }
        guard now - lastQuery >= 0.08 else { return }
        guard !busy, let target else { return }
        busy = true; lastQuery = now
        let revision = revision, ownWindows = Self.windowIDs
        queue.async { [weak self] in
            let picture = Self.picture(target, excluding: ownWindows)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.busy = false
                guard self.revision == revision, self.target != nil else { return }
                if case .resolved(let picture) = picture {
                    let window = WindowGeometry.WindowMapping(id: picture.windowID, pid: picture.pid,
                        frame: picture.frame, title: nil, width: Int(picture.frame.width), height: Int(picture.frame.height))
                    // Commit geometry even when occluded. Visibility is not
                    // evidence that a target resolved, failed or changed.
                    self.target = Target(window: window, point: nil, element: nil,
                        restingOffset: CGPoint(x: picture.point.x - picture.frame.minX, y: picture.point.y - picture.frame.minY))
                    if let target = self.target { self.rememberAnchor(target) }
                    // Resolve an element once; caret/editor/layout updates must
                    // not keep dragging the cursor around during the operation.
                    if picture.frame.contains(picture.point) {
                        self.draw(picture)
                    } else {
                        self.panel?.orderOut(nil); self.pulsePending = 0
                    }
                } else {
                    self.panel?.orderOut(nil)
                    // Keep the latest intent, never fall back to an older
                    // control just because the window/AX geometry is unavailable.
                    self.pulsePending = 0
                    if case .offscreen = picture { self.recoverObservedWindow() }
                }
            }
        }
    }

    nonisolated private static func picture(_ target: Target, excluding: Set<CGWindowID>) -> Resolution {
        guard let raw = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return .unavailable }
        let windows = raw.filter { item in
            guard let id = (item[kCGWindowNumber as String] as? NSNumber)?.uint32Value else { return false }
            return !excluding.contains(id) && (item[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1 > 0
        }
        func frame(_ item: [String: Any]) -> CGRect? {
            guard let raw = item[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: raw as CFDictionary), AgentPointerGeometry.valid(rect) else { return nil }
            return rect
        }
        func geometry(_ element: AXUIElement) -> CGRect? {
            var raw: CFArray?
            guard AXUIElementCopyMultipleAttributeValues(element, [kAXPositionAttribute, kAXSizeAttribute] as CFArray, [], &raw) == .success,
                  let values = raw as? [Any], values.count == 2,
                  CFGetTypeID(values[0] as CFTypeRef) == AXValueGetTypeID(), CFGetTypeID(values[1] as CFTypeRef) == AXValueGetTypeID() else { return nil }
            var origin = CGPoint.zero, size = CGSize.zero
            guard AXValueGetValue(unsafeDowncast(values[0] as AnyObject, to: AXValue.self), .cgPoint, &origin),
                  AXValueGetValue(unsafeDowncast(values[1] as AnyObject, to: AXValue.self), .cgSize, &size) else { return nil }
            let rect = CGRect(origin: origin, size: size)
            return AgentPointerGeometry.valid(rect) ? rect : nil
        }
        let mappedIndex = target.window.flatMap { window in windows.firstIndex {
            ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == window.id &&
            ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == window.pid
        } }
        var elementRect: CGRect?
        var elementWindow: Int?
        if let element = target.element {
            guard let rect = geometry(element) else {
                // The element may disappear before its first geometry result.
                return target.window != nil && mappedIndex == nil ? .offscreen : .unavailable
            }
            elementRect = rect
            // Resolve the actual surface before consulting the work window:
            // apps can hide that window while a separate popup remains onscreen.
            var rawWindow: CFTypeRef?
            if AXUIElementCopyAttributeValue(element, kAXWindowAttribute as CFString, &rawWindow) == .success,
               let rawWindow, CFGetTypeID(rawWindow) == AXUIElementGetTypeID() {
                let axWindow = unsafeDowncast(rawWindow as AnyObject, to: AXUIElement.self)
                var pid: pid_t = 0
                if AXUIElementGetPid(axWindow, &pid) == .success, let actual = geometry(axWindow) {
                    let matches = windows.indices.filter { i in
                        guard (windows[i][kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                              let f = frame(windows[i]) else { return false }
                        return abs(f.minX - actual.minX) < 1 && abs(f.minY - actual.minY) < 1
                            && abs(f.width - actual.width) < 1 && abs(f.height - actual.height) < 1
                    }
                    if matches.count == 1 { elementWindow = matches.first }
                }
            }
        }
        let index: Int
        if let elementWindow {
            index = elementWindow
        } else if target.window != nil {
            guard let mappedIndex else {
                // Apps often cache dismissed panels instead of destroying
                // their WindowServer identity. Recover only from a subsequent
                // observation; invisibility alone never picks another window.
                return .offscreen
            }
            index = mappedIndex
        } else {
            guard let point = target.point, let found = windows.firstIndex(where: { frame($0)?.contains(point) == true }) else { return .unavailable }
            index = found
        }
        guard let bounds = frame(windows[index]) else { return .unavailable }
        let point: CGPoint
        if let offset = target.restingOffset {
            point = AgentPointerGeometry.restingPoint(offset: offset, in: bounds)
        } else if let rect = elementRect {
            let visible = rect.intersection(bounds)
            guard AgentPointerGeometry.valid(visible) else { return .unavailable }
            point = CGPoint(x: visible.midX, y: visible.midY)
        } else if let location = target.point {
            // A desktop coordinate initially has no window. Resolve its visual
            // surface once; subsequent ticks retain a window-local anchor.
            guard bounds.contains(location) else { return .unavailable }
            point = location
        } else { return .unavailable }
        guard let id = (windows[index][kCGWindowNumber as String] as? NSNumber)?.uint32Value,
              let pid = (windows[index][kCGWindowOwnerPID as String] as? NSNumber)?.int32Value else { return .unavailable }
        return .resolved(Picture(frame: bounds, point: point,
            level: (windows[index][kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0, windowID: id, pid: pid))
    }

    private func draw(_ picture: Picture) {
        guard Self.captureDepth == 0 else { return }
        if panel == nil { createPanel() }
        guard let panel, let appearance else { return }
        let frame = AgentPointerGeometry.appKit(picture.frame, primaryHeight: CGDisplayBounds(CGMainDisplayID()).height)
        let newWindow = shownWindow != picture.windowID || shownPID != picture.pid
        let movedWindow = panel.frame != frame
        let position = CGPoint(x: picture.point.x - picture.frame.minX, y: picture.frame.maxY - picture.point.y)
        CATransaction.begin(); CATransaction.setDisableActions(true)
        panel.setFrame(frame, display: false)
        CATransaction.commit()
        updateAppearance()
        appearance.moveTo(position, animated: !newWindow && !movedWindow, appear: newWindow,
            scale: panel.screen?.backingScaleFactor ?? 2)
        if pulsePending > 0 { appearance.click(count: pulsePending); pulsePending = 0 }
        // Keep decoration immediately above its actual surface, at the same
        // level. WindowServer then composites transparent recording overlays,
        // occluders and window switches correctly. A front window's rectangle
        // alone is not evidence that its pixels cover the target.
        panel.level = NSWindow.Level(rawValue: picture.level)
        panel.order(.above, relativeTo: Int(picture.windowID))
        lastPaint = ProcessInfo.processInfo.systemUptime
        shownWindow = picture.windowID; shownPID = picture.pid
    }

    private func createPanel() {
        let panel = PointerPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = Self.windowTitle; panel.isReleasedWhenClosed = false
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = false
        panel.ignoresMouseEvents = true; panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle, .transient]
        panel.setAccessibilityElement(false)
        let view = NSView(); view.wantsLayer = true; view.setAccessibilityElement(false)
        panel.contentView = view
        let appearance = AgentPointerAppearance()
        view.layer?.addSublayer(appearance.layer)
        self.panel = panel; self.appearance = appearance
        Self.windowIDs.insert(CGWindowID(panel.windowNumber))
    }
}

private final class PointerPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}
