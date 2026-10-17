import AppKit
import ApplicationServices

@main struct NativeMenuSurfacesTests {
    @MainActor static func main() {
        let app = NSRunningApplication.current, process = NativeProcessIdentity.read(getpid())!
        let menu = AXUIElementCreateApplication(getpid()) // Graph tests do not inspect this placeholder.
        func window(_ id: CGWindowID, _ parent: CGWindowID? = nil, knownMenu: Bool = true) -> NativeWindows.Window {
            NativeWindows.Window(application: app, process: process,
                mapping: .init(id: id, pid: getpid(), frame: CGRect(x: 10, y: 10, width: 200, height: 100),
                    title: nil, width: 200, height: 100), onScreen: true,
                level: Int(CGWindowLevelForKey(.popUpMenuWindow)), menu: knownMenu ? menu : nil, menuParent: parent)
        }
        precondition(NativeMenuSurfaces.current([]) == nil)
        precondition(NativeMenuSurfaces.current([window(1)]) == 1)
        precondition(NativeMenuSurfaces.current([window(3, 2), window(1), window(2, 1)]) == 3)
        precondition(NativeMenuSurfaces.current([window(1), window(2)]) == nil, "unrelated menus are not one chain")
        precondition(NativeMenuSurfaces.current([window(1), window(2, 1), window(3, 1)]) == nil, "ambiguous branches stay explicit")
        precondition(NativeMenuSurfaces.current([window(1, 2), window(2, 1)]) == nil, "cycles do not pick an arbitrary surface")
        precondition(NativeMenuSurfaces.current([window(1), window(2, 99)]) == nil, "missing parent is not inferred")
        precondition(NativeMenuSurfaces.current([window(1), window(2, knownMenu: false)]) == nil, "unknown window cannot disappear")
        let ordinary = NativeMenuSurfaces.classify([window(1, knownMenu: false)])
        precondition(ordinary.count == 1 && ordinary[0].menu == nil, "popup layer alone is not native menu evidence")
        print("PASS: menu chain, ambiguity, missing ownership and unknown-surface preservation")
    }
}
