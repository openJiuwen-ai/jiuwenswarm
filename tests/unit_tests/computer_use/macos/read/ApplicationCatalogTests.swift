import Foundation

@main struct ApplicationCatalogTests {
    static func main() throws {
        let fm = FileManager.default
        let temp = fm.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent(UUID().uuidString)
        defer { try? fm.removeItem(at: temp) }
        let root = temp.appendingPathComponent("Applications")
        try fm.createDirectory(at: root, withIntermediateDirectories: true)

        func bundle(_ parent: URL, _ name: String, _ identifier: String?, metadata: [String: Any] = [:]) throws -> URL {
            let url = parent.appendingPathComponent(name + ".app")
            let executable = url.appendingPathComponent("Contents/MacOS/test")
            try fm.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
            // Bundle validates executable metadata; use an existing Mach-O without launching it.
            try fm.copyItem(at: URL(fileURLWithPath: CommandLine.arguments[0]), to: executable)
            var info: [String: Any] = ["CFBundleName": name, "CFBundleDisplayName": "Display " + name, "CFBundleExecutable": "test", "CFBundlePackageType": "APPL"]
            if let identifier { info["CFBundleIdentifier"] = identifier }
            info.merge(metadata) { _, replacement in replacement }
            try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: url.appendingPathComponent("Contents/Info.plist"))
            return url
        }

        let calculator = try bundle(root, "Calculator", "test.calculator")
        let editor = try bundle(root.appendingPathComponent("Utilities"), "Editor", "test.editor")
        precondition(Bundle(url: editor)?.executableURL != nil, "Fixture must be a valid application bundle")
        _ = try bundle(calculator.appendingPathComponent("Contents/Helpers"), "Internal", "test.internal")
        _ = try bundle(root.appendingPathComponent(".hidden"), "Hidden", "test.hidden")
        try fm.createDirectory(at: root.appendingPathComponent("Invalid.app"), withIntermediateDirectories: true)
        try fm.createSymbolicLink(at: root.appendingPathComponent("Alias.app"), withDestinationURL: calculator)
        let outside = try bundle(temp.appendingPathComponent("Elsewhere"), "Outside", "test.outside")
        let active = [ApplicationCatalog.Running(id: "instance:calculator:1", url: calculator, identifier: "test.calculator", name: "计算器"),
            ApplicationCatalog.Running(id: "instance:calculator:2", url: calculator, identifier: "test.calculator", name: "Another instance"),
            ApplicationCatalog.Running(id: "instance:outside", url: outside, identifier: "test.outside", name: "Outside")]
        let apps = ApplicationCatalog.list(roots: [root, temp.appendingPathComponent("Missing")], running: active)
        precondition(Set(apps.map(\.id)) == ["instance:calculator:1", "instance:calculator:2", "test.editor", "instance:outside"], "Unexpected apps: \(apps.map(\.json))")
        precondition(apps.first(where: { $0.id == "instance:calculator:1" })?.displayName == "计算器")
        precondition(apps.filter(\.isRunning).count == 3)
        precondition(apps.last?.id == "test.editor")
        precondition(apps.last?.displayName == "Display Editor")
        print("PASS: discovery preserves independent instances of the same bundle, skips nested installations, and deduplicates installation aliases")

        let copy = try bundle(root, "Editor Copy", "test.editor")
        let noID = try bundle(root, "No Identifier", nil)
        let copied = ApplicationCatalog.list(roots: [root], running: [])
        precondition(Set(copied.map(\.id)) == Set(["test.calculator", editor.path, copy.path, noID.path]))
        precondition(copied.allSatisfy { !$0.isRunning })
        print("PASS: duplicate/missing bundle IDs use unambiguous absolute selectors")

        try fm.removeItem(at: copy)
        let refreshed = ApplicationCatalog.list(roots: [root], running: [])
        precondition(refreshed.contains { $0.id == "test.editor" })
        precondition(!refreshed.contains { $0.id == copy.path || $0.id == "test.outside" })
        precondition(refreshed.first(where: { $0.id == "test.calculator" })?.isRunning == false)
        print("PASS: each enumeration refreshes discovery and running state")

        let uiRoot = temp.appendingPathComponent("UI Sorting")
        let helper = try bundle(uiRoot, "A Helper", "ui.helper", metadata: ["LSUIElement": true])
        _ = try bundle(uiRoot, "B Background", "ui.background", metadata: ["LSBackgroundOnly": "1"])
        let unknown = try bundle(uiRoot, "C Unknown", "ui.unknown", metadata: ["LSUIElement": "not-a-boolean"])
        _ = try bundle(uiRoot, "D Missing Policy", "ui.incomplete", metadata: ["CFBundlePackageType": "????"])
        let regular = try bundle(uiRoot, "Z Regular", "ui.regular")
        _ = try bundle(uiRoot, "Y Explicit Regular", "ui.explicit", metadata: ["LSUIElement": false, "LSBackgroundOnly": "0"])
        let installed = ApplicationCatalog.list(roots: [uiRoot], running: [])
        precondition(installed.map(\.id) == ["ui.explicit", "ui.regular", "ui.helper", "ui.background", "ui.unknown", "ui.incomplete"], "Wrong GUI ranking: \(installed.map(\.json))")
        precondition(installed.allSatisfy { Set($0.json.keys) == ["id", "displayName", "isRunning"] })
        print("PASS: declared regular applications sort first; helpers and unknown policies stay discoverable")

        let mixed = ApplicationCatalog.list(roots: [uiRoot], running: [
            .init(id: "instance:helper", url: helper, identifier: "ui.helper", name: "Z Live Regular", regularUI: true),
            .init(id: "instance:regular", url: regular, identifier: "ui.regular", name: "A Live Agent", regularUI: false),
            .init(id: "instance:unknown", url: unknown, identifier: "ui.unknown", name: "B Live Unknown")
        ])
        precondition(Array(mixed.prefix(3).map(\.id)) == ["instance:helper", "instance:regular", "instance:unknown"], "Running policy must override bundle declarations: \(mixed.map(\.json))")
        precondition(mixed.prefix(3).allSatisfy(\.isRunning))
        precondition(mixed[3].id == "ui.explicit", "Installed regular apps stay behind all running apps")
        print("PASS: running status is the primary group and live activation policies override bundle metadata")

        let repeated = ApplicationCatalog.list(roots: [uiRoot], running: [
            .init(id: "instance:helper", url: helper, identifier: "ui.helper", name: "Z First", regularUI: false),
            .init(id: "instance:helper", url: helper, identifier: "ui.helper", name: "Repeated observation", regularUI: false),
            .init(id: "instance:second", url: helper, identifier: "ui.helper", name: "A Second", regularUI: true),
            .init(id: "instance:unbundled", url: nil, identifier: nil, name: "Unbundled", regularUI: true)
        ])
        precondition(repeated.filter(\.isRunning).count == 3)
        precondition(repeated.first?.id == "instance:second")
        precondition(repeated.first(where: { $0.id == "instance:helper" })?.displayName == "Z First")
        precondition(repeated.contains { $0.id == "instance:unbundled" })
        precondition(!repeated.contains { $0.id == "ui.helper" })
        print("PASS: only repeated observations of the same lifetime merge; an unbundled running app remains selectable")

        let hiddenRoot = temp.appendingPathComponent("Hidden entry points")
        try fm.createDirectory(at: hiddenRoot, withIntermediateDirectories: true)
        let linked = try bundle(temp.appendingPathComponent("System payload"), "Linked Browser", "test.linked")
        var link = hiddenRoot.appendingPathComponent("Linked Browser.app")
        try fm.createSymbolicLink(at: link, withDestinationURL: linked)
        var hidden = URLResourceValues(); hidden.isHidden = true
        try link.setResourceValues(hidden)
        let visibleBeforeFix = fm.enumerator(at: hiddenRoot, includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles, .skipsPackageDescendants])!.allObjects as! [URL]
        precondition(!visibleBeforeFix.contains { $0.standardizedFileURL.path == link.standardizedFileURL.path }, "Fixture must reproduce a Finder-hidden application link")
        var hiddenApp = try bundle(hiddenRoot, "Hidden App", "test.hidden-app")
        try hiddenApp.setResourceValues(hidden)
        var hiddenContainer = hiddenRoot.appendingPathComponent("Hidden Container")
        _ = try bundle(hiddenContainer, "Internal", "test.hidden-container")
        try hiddenContainer.setResourceValues(hidden)
        _ = try bundle(hiddenRoot, ".Private", "test.dot-app")
        _ = try bundle(hiddenRoot.appendingPathComponent(".private"), "Internal", "test.dot-container")
        try fm.createSymbolicLink(at: hiddenRoot.appendingPathComponent("Broken.app"),
            withDestinationURL: temp.appendingPathComponent("Missing.app"))
        let discovered = ApplicationCatalog.list(roots: [hiddenRoot], running: [])
        precondition(Set(discovered.map(\.id)) == ["test.linked", "test.hidden-app"],
            "Hidden launchable entries must survive without traversing private containers: \(discovered.map(\.json))")
        precondition(ApplicationCatalog.installedURLs(roots: [hiddenRoot]).contains { $0.path == link.standardizedFileURL.path })
        print("PASS: hidden application links and bundles remain selectable; private containers and broken links are excluded")

        let copyOutside = try bundle(temp.appendingPathComponent("OtherInstall"), "Editor", "test.editor")
        let sameID = ApplicationCatalog.list(roots: [root], running: [
            .init(id: "instance:outside-copy", url: copyOutside, identifier: "test.editor", name: "Editor")
        ])
        precondition(sameID.contains { $0.id == editor.path && !$0.isRunning })
        precondition(sameID.contains { $0.id == "instance:outside-copy" && $0.isRunning })
        print("PASS: active installation outside search roots makes another installation's bundle ID ambiguous")

        var checks = 0
        let stopped = ApplicationCatalog.list(roots: [root], running: active, continuing: {
            checks += 1
            return false
        })
        precondition(checks > 0)
        precondition(Set(stopped.map(\.id)) == Set(active.map(\.id)))
        precondition(stopped.allSatisfy(\.isRunning))
        precondition(ApplicationCatalog.installedURLs(roots: [root], continuing: { false }).isEmpty)
        print("PASS: bounded discovery stops installation traversal without losing already observed running lifetimes")
    }
}
