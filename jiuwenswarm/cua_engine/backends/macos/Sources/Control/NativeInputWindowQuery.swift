import AppKit
import ApplicationServices

/// Read-only public fallback for unavailable WindowServer hit SPI. Apple defines
/// the system-wide AX hit as z-order based. A window catalogue alone cannot
/// establish an input hit through transparent or pass-through windows.
@MainActor enum NativeInputWindowQuery {
    struct PublicIdentity {
        let result: NativeInputWindowHit.Accessibility
        let window: NativeInputWindowHit.PublicWindow?
    }

    static func accessibility(at point: CGPoint, retained: WindowGeometry.WindowMapping? = nil,
                              nativeWindow: AXUIElement? = nil) -> NativeInputWindowHit.Accessibility {
        guard point.x.isFinite, point.y.isFinite,
              abs(point.x) <= Double(Float.greatestFiniteMagnitude),
              abs(point.y) <= Double(Float.greatestFiniteMagnitude) else { return .unresolvedWindow }
        var hit: AXUIElement?
        let status = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &hit)
        if status == .noValue { return .noElement }
        guard status == .success else { return .failed(status.rawValue) }
        guard let hit else { return .noElement }
        var pid: pid_t = 0
        guard AXUIElementGetPid(hit, &pid) == .success, pid > 0 else { return .unresolvedWindow }
        let owner: AXUIElement
        if retained?.pid == pid, let nativeWindow, CFEqual(hit, nativeWindow) {
            // An exact retained object needs no additional role/parent labels.
            owner = nativeWindow
        } else {
            guard let value = WindowGeometry.owningWindow(hit, pid: pid,
                in: retained?.pid == pid ? nativeWindow : nil) else { return .unresolvedWindow }
            owner = value
        }

        if let retained, let nativeWindow, pid == retained.pid, CFEqual(owner, nativeWindow) {
            let expected = NativeInputWindowHit.PublicWindow(id: retained.id, pid: retained.pid, frame: retained.frame)
            guard NativeInputWindowHit.confirmsRetainedWindow(expected: expected,
                current: WindowServerInfo.window(retained.id).flatMap(row), at: point) else { return .unresolvedWindow }
            return .window(retained.id)
        }
        // The AX hit can identify a covering window. Never relabel it as the
        // retained target just because it has the same PID or overlapping bounds.
        if let id = BackgroundEvents.nativeWindowID(owner) {
            guard let current = WindowServerInfo.window(id).flatMap(row),
                  current.pid == pid, current.frame.contains(point) else { return .unresolvedWindow }
            return .window(current.id)
        }
        return publicIdentity(of: owner, pid: pid, at: point).result
    }

    /// Resolve this AX window's CG identity when the private AX-window bridge is
    /// absent. This function does not establish a physical input hit. In the hit
    /// fallback its caller already obtained the owner from system-wide AX.
    static func publicIdentity(of window: AXUIElement, pid: pid_t, at point: CGPoint? = nil) -> PublicIdentity {
        var owner: pid_t = 0
        guard AXUIElementGetPid(window, &owner) == .success, owner == pid,
              let frame = WindowGeometry.hint(window)?.frame,
              let rows = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] else {
            return .init(result: .unresolvedWindow, window: nil)
        }
        let windows = rows.compactMap(row)
        let result = NativeInputWindowHit.mapPublicWindow(pid: pid, frame: frame,
            at: point ?? CGPoint(x: frame.midX, y: frame.midY), windows: windows)
        if case .window(let id) = result {
            return .init(result: result, window: windows.first { $0.id == id && $0.pid == pid })
        }
        return .init(result: result, window: nil)
    }

    static func row(_ raw: [String: Any]) -> NativeInputWindowHit.PublicWindow? {
        guard let id = (raw[kCGWindowNumber as String] as? NSNumber)?.uint32Value, id != 0,
              let pid = (raw[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value, pid > 0,
              let bounds = raw[kCGWindowBounds as String] as? [String: Any],
              let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary),
              [frame.minX, frame.minY, frame.width, frame.height].allSatisfy(\.isFinite),
              frame.width > 0, frame.height > 0 else { return nil }
        return .init(id: id, pid: pid, frame: frame)
    }
}
