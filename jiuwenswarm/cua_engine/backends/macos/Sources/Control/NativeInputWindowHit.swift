import Foundation
import CoreGraphics

/// Evidence for the window at a screen point. A public accessibility hit is
/// kept distinct from WindowServer's input hit; neither proves a control effect.
struct NativeInputWindowHit: Equatable {
    enum WindowServer: Equatable {
        case window(CGWindowID), noWindow, unavailable, failed(Int32)
    }
    enum Accessibility: Equatable {
        case window(CGWindowID), noElement, failed(Int32), unresolvedWindow, ambiguousWindow
    }
    enum Source: String { case windowServer, accessibility }

    let windowServer: WindowServer
    let accessibility: Accessibility?

    var windowID: CGWindowID? {
        if case .window(let id) = windowServer { return id }
        if case .window(let id) = accessibility { return id }
        return nil
    }
    var source: Source? {
        if case .window = windowServer { return .windowServer }
        if case .window = accessibility { return .accessibility }
        return nil
    }

    /// A successful private query, including an empty hit, is authoritative.
    /// Only unavailable/failed private queries need the independent public read.
    static func resolve(_ result: WindowServer, publicHit: () -> Accessibility) -> Self {
        switch result {
        case .window, .noWindow: return .init(windowServer: result, accessibility: nil)
        case .unavailable, .failed: return .init(windowServer: result, accessibility: publicHit())
        }
    }

    struct PublicWindow {
        let id: CGWindowID
        let pid: pid_t
        let frame: CGRect
    }

    /// Absence of the private AX bridge allows public identity evidence. An
    /// already returned native ID that vanished or conflicts never does.
    static func identity(pid: pid_t, nativeID: CGWindowID?,
                         current: (CGWindowID) -> PublicWindow?, publicIdentity: () -> PublicWindow?) -> PublicWindow? {
        let result: PublicWindow?
        if let nativeID {
            guard let value = current(nativeID), value.id == nativeID else { return nil }
            result = value
        } else {
            result = publicIdentity()
        }
        guard let result, result.pid == pid, result.id != 0, usable(result.frame) else { return nil }
        return result
    }

    /// AX already supplied the hit and owning window. These rows only map that
    /// window to a CG identity; their order and overlap never establish a hit.
    static func mapPublicWindow(pid: pid_t, frame: CGRect, at point: CGPoint,
                                windows: [PublicWindow]) -> Accessibility {
        guard usable(frame), frame.contains(point) else { return .unresolvedWindow }
        let candidates = windows.filter {
            $0.id != 0 && $0.pid == pid && usable($0.frame) && sameFrame($0.frame, frame)
        }
        let ids = Set(candidates.map(\.id))
        guard ids.count == 1, let id = ids.first else {
            return ids.isEmpty ? .unresolvedWindow : .ambiguousWindow
        }
        return .window(id)
    }

    static func confirmsRetainedWindow(expected: PublicWindow, current: PublicWindow?, at point: CGPoint) -> Bool {
        guard let current else { return false }
        return current.id == expected.id && current.pid == expected.pid
            && usable(current.frame) && sameFrame(current.frame, expected.frame) && current.frame.contains(point)
    }

    private static func usable(_ frame: CGRect) -> Bool {
        [frame.minX, frame.minY, frame.width, frame.height].allSatisfy(\.isFinite)
            && frame.width > 0 && frame.height > 0
    }

    private static func sameFrame(_ left: CGRect, _ right: CGRect) -> Bool {
        abs(left.minX - right.minX) < 0.5 && abs(left.minY - right.minY) < 0.5
            && abs(left.width - right.width) < 0.5 && abs(left.height - right.height) < 0.5
    }
}
