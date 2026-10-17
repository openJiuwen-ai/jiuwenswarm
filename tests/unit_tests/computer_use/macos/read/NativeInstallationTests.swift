import Foundation

@main struct NativeInstallationTests {
    static func main() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? fm.removeItem(at: root) }
        let bundle = root.appendingPathComponent("Observed.app")
        let executable = bundle.appendingPathComponent("Contents/MacOS/probe")
        try fm.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
        try fm.copyItem(at: URL(fileURLWithPath: CommandLine.arguments[0]), to: executable)
        let plist = bundle.appendingPathComponent("Contents/Info.plist")
        let info: [String: Any] = ["CFBundleExecutable": "probe", "CFBundlePackageType": "APPL", "CFBundleIdentifier": "local.installation.fixture"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: plist)
        let observed = try NativeInstallation(path: bundle.path)
        try observed.validate()
        let alias = root.appendingPathComponent("Alias.app")
        try fm.createSymbolicLink(at: alias, withDestinationURL: bundle)
        let aliased = try NativeInstallation(path: alias.path)
        precondition(aliased == observed, "Alias identity mismatch: \(aliased) != \(observed)")

        func stale(_ action: () throws -> Void) throws {
            try action()
            do { try observed.validate(); preconditionFailure("changed installation accepted") }
            catch let error as DriverError { precondition(error.code == .staleTarget) }
        }
        try stale {
            let replacement = executable.appendingPathExtension("replacement")
            try fm.copyItem(at: executable, to: replacement)
            try fm.removeItem(at: executable)
            try fm.moveItem(at: replacement, to: executable)
        }
        let newVersion = try NativeInstallation(path: bundle.path)
        precondition(newVersion != observed)
        try newVersion.validate()
        try fm.removeItem(at: bundle)
        do { try newVersion.validate(); preconditionFailure("removed installation accepted") }
        catch let error as DriverError { precondition(error.code == .staleTarget) }
        print("PASS observed installation identity, aliases, executable replacement and removal; no application launched")
    }
}
