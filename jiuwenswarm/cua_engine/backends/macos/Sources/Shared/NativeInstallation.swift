import Foundation

/// A captured installation identity. An observed bundle selector is not a
/// persistent capability to launch whatever later occupies the same path.
struct NativeInstallation: Hashable {
    struct FileIdentity: Hashable {
        let device: UInt64
        let inode: UInt64
        let modified: Date
        let size: UInt64

        init(_ url: URL) throws {
            let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
            guard let device = attrs[.systemNumber] as? NSNumber,
                  let inode = attrs[.systemFileNumber] as? NSNumber,
                  let modified = attrs[.modificationDate] as? Date,
                  let size = attrs[.size] as? NSNumber else {
                throw DriverError("installation identity is unavailable", code: .observationIncomplete)
            }
            self.device = device.uint64Value; self.inode = inode.uint64Value
            self.modified = modified; self.size = size.uint64Value
        }
    }
    let url: URL
    let executable: URL
    let files: [FileIdentity]

    init(path: String) throws {
        let canonical = URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath()
        // Foundation may retain a different directory slash for a symlink and
        // its target. That is presentation, not a new installation identity.
        url = URL(fileURLWithPath: canonical.path, isDirectory: true)
        guard url.pathExtension.lowercased() == "app", let executable = Bundle(url: url)?.executableURL else {
            throw DriverError("observed installation is not an executable application bundle", code: .invalidRequest)
        }
        self.executable = executable.standardizedFileURL.resolvingSymlinksInPath()
        files = try [url, self.executable, url.appendingPathComponent("Contents/Info.plist")].map(FileIdentity.init)
    }

    func validate() throws {
        guard let current = try? NativeInstallation(path: url.path), current == self else {
            throw DriverError("the observed application installation changed or disappeared", code: .staleTarget,
                              next: "read the application directory again before launching")
        }
    }
}
