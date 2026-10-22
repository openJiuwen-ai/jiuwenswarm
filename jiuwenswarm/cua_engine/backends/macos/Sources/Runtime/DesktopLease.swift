import Foundation
import Darwin

/// Coordinates this project's drivers for the same macOS user. This does not
/// lock out the human or other software, and never changes the desktop mode.
@MainActor final class DesktopLease {
    private var descriptor: Int32

    private init(_ descriptor: Int32) { self.descriptor = descriptor }

    private static func systemPath() throws -> String {
        let size = confstr(_CS_DARWIN_USER_TEMP_DIR, nil, 0)
        guard size > 1 else { throw LeaseError("cannot resolve the user's native temporary directory") }
        var bytes = [CChar](repeating: 0, count: size)
        guard confstr(_CS_DARWIN_USER_TEMP_DIR, &bytes, size) == size else { throw LeaseError("native temporary directory changed") }
        let directory = String(decoding: bytes.dropLast().map { UInt8(bitPattern: $0) }, as: UTF8.self)
        return URL(fileURLWithPath: directory, isDirectory: true).appendingPathComponent("computer-use-desktop-v3.lock").path
    }

    /// Nonblocking flock keeps AppKit and disconnect handling responsive. A
    /// cancelled waiter never becomes an input owner. Do not unlink the file:
    /// replacing its inode would split coordination between waiting drivers.
    static func acquire(testPath: String? = nil) async throws -> DesktopLease {
        let path = try testPath ?? systemPath()
        let fd = open(path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw LeaseError("cannot open desktop coordination file: \(errno)") }
        var transferred = false
        defer { if !transferred { close(fd) } }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == getuid(), (info.st_mode & S_IFMT) == S_IFREG, info.st_nlink == 1 else {
            throw LeaseError("desktop coordination file is not a private regular file owned by this user")
        }
        while true {
            try Task.checkCancellation()
            if flock(fd, LOCK_EX | LOCK_NB) == 0 {
                do { try Task.checkCancellation() } catch { _ = flock(fd, LOCK_UN); throw error }
                transferred = true
                return DesktopLease(fd)
            }
            guard errno == EWOULDBLOCK || errno == EAGAIN || errno == EINTR else { throw LeaseError("cannot acquire desktop coordination: \(errno)") }
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    func release() {
        guard descriptor >= 0 else { return }
        _ = flock(descriptor, LOCK_UN)
        close(descriptor); descriptor = -1
    }

    deinit { if descriptor >= 0 { close(descriptor) } }
}

struct LeaseError: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}
