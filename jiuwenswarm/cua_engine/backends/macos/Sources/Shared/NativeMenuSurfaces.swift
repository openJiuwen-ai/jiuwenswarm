import AppKit
import ApplicationServices

/// An application's visible menu is an interaction surface, not a document.
/// Require native menu semantics AND a unique same-process WindowServer match.
/// A popup layer alone never removes an unknown/custom window from discovery.
@MainActor enum NativeMenuSurfaces {
    static func frame(_ menu: AXUIElement, pid: pid_t) -> CGRect? {
        var owner: pid_t = 0, count: CFIndex = 0
        guard AXUIElementGetPid(menu, &owner) == .success, owner == pid,
              AXAccess.read(menu, kAXRoleAttribute).value as? String == kAXMenuRole,
              AXUIElementGetAttributeValueCount(menu, kAXVisibleChildrenAttribute as CFString, &count) == .success,
              count > 0, let frame = WindowGeometry.hint(menu)?.frame,
              frame.width > 1, frame.height > 1 else { return nil }
        return frame
    }

    /// AXMenu can also describe a persistent inline list. Only a visible,
    /// uniquely matching native popup surface proves that keyboard input must
    /// preserve menu tracking instead of preparing the document normally.
    static func isPresentedPopup(_ menu: AXUIElement, pid: pid_t) -> Bool {
        guard let frame = frame(menu, pid: pid),
              let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] else { return false }
        let matches = windows.filter { info in
            guard (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  (info[kCGWindowLayer as String] as? NSNumber)?.intValue == Int(CGWindowLevelForKey(.popUpMenuWindow)),
                  let raw = info[kCGWindowBounds as String] as? [String: Any],
                  let bounds = CGRect(dictionaryRepresentation: raw as CFDictionary) else { return false }
            return WindowSelection.sameFrame(frame, bounds)
        }
        return matches.count == 1
    }

    static func matches(_ menu: AXUIElement, window: WindowGeometry.WindowMapping) -> Bool {
        guard let frame = frame(menu, pid: window.pid), WindowSelection.sameFrame(frame, window.frame),
              let current = WindowServerInfo.window(window.id),
              (current[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == window.pid,
              (current[kCGWindowLayer as String] as? NSNumber)?.intValue == Int(CGWindowLevelForKey(.popUpMenuWindow)) else { return false }
        return true
    }

    static func classify(_ windows: [NativeWindows.Window]) -> [NativeWindows.Window] {
        guard AXIsProcessTrusted() else { return windows }
        var result = windows
        for pid in Set(windows.filter { $0.level == Int(CGWindowLevelForKey(.popUpMenuWindow)) }.map { $0.process.pid }) {
            let deadline = ProcessInfo.processInfo.systemUptime + 0.15
            let root = AXUIElementCreateApplication(pid)
            AXUIElementSetMessagingTimeout(root, 0.05)
            guard let raw = AXAccess.read(root, kAXMenuBarAttribute).value, CFGetTypeID(raw) == AXUIElementGetTypeID() else { continue }
            var pending: [(element: AXUIElement, parent: AXUIElement?)] = [(unsafeDowncast(raw, to: AXUIElement.self), nil)]
            var seen = [AXUIElement](), menus = [(element: AXUIElement, frame: CGRect, parent: AXUIElement?)]()
            var cursor = 0
            while cursor < pending.count, seen.count < 256, ProcessInfo.processInfo.systemUptime < deadline {
                let item = pending[cursor], element = item.element; cursor += 1
                guard !seen.contains(where: { CFEqual($0, element) }) else { continue }
                seen.append(element); AXUIElementSetMessagingTimeout(element, 0.05)
                let role = AXAccess.read(element, kAXRoleAttribute).value as? String
                if role == kAXMenuRole {
                    // Hidden static menus are not live input surfaces. Do not
                    // expand their potentially large, unrelated command trees.
                    guard let bounds = frame(element, pid: pid) else { continue }
                    menus.append((element, bounds, item.parent))
                }
                pending += (AXAccess.elements(element, kAXChildrenAttribute, limit: 256) ?? []).map {
                    ($0, role == kAXMenuRole ? element : item.parent)
                }
            }
            guard cursor == pending.count else { continue }
            for index in result.indices where result[index].process.pid == pid
                && result[index].level == Int(CGWindowLevelForKey(.popUpMenuWindow)) {
                let window = result[index]
                let candidates = menus.filter { WindowSelection.sameFrame($0.1, window.mapping.frame) }
                guard candidates.count == 1, let menu = candidates.first,
                      windows.filter({ $0.process.pid == pid && $0.level == window.level
                          && WindowSelection.sameFrame($0.mapping.frame, menu.1) }).count == 1 else { continue }
                result[index].menu = menu.0
            }
            for index in result.indices where result[index].process.pid == pid {
                guard let menu = result[index].menu,
                      let parent = menus.first(where: { CFEqual($0.element, menu) })?.parent else { continue }
                let owners = result.filter { $0.process.pid == pid && $0.menu.map { CFEqual($0, parent) } == true }
                if owners.count == 1 { result[index].menuParent = owners[0].mapping.id }
            }
        }
        return result
    }

    /// Only one complete native menu chain may select its innermost popup.
    /// Unrelated/unknown surfaces remain explicit choices, with all pixels kept.
    static func current(_ windows: [NativeWindows.Window]) -> CGWindowID? {
        guard !windows.isEmpty, windows.allSatisfy({ $0.menu != nil }) else { return nil }
        let roots = windows.filter { $0.menuParent == nil }
        guard roots.count == 1 else { return nil }
        var current = roots[0].mapping.id, visited = Set<CGWindowID>()
        while visited.insert(current).inserted {
            let children = windows.filter { $0.menuParent == current }
            if children.isEmpty { return visited.count == windows.count ? current : nil }
            guard children.count == 1 else { return nil }
            current = children[0].mapping.id
        }
        return nil
    }
}
