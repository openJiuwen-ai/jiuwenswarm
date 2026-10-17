import Foundation
import Darwin

@main struct DesktopLeaseTests {
    @MainActor static func main() async throws {
        if CommandLine.arguments.count > 1 {
            let path = CommandLine.arguments[1], mark = CommandLine.arguments[2]
            let lease = try await DesktopLease.acquire(testPath: path == "--system" ? nil : path)
            defer { lease.release() }
            try Data("entered".utf8).write(to: URL(fileURLWithPath: mark))
            while true { try await Task.sleep(for: .seconds(1)) }
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("desktop.lock").path
        let mark = directory.appendingPathComponent("entered").path
        let held = try await DesktopLease.acquire(testPath: path)
        let child = Process()
        child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        child.arguments = [path, mark]
        try child.run()
        defer { if child.isRunning { kill(child.processIdentifier, SIGKILL) } }
        try await Task.sleep(for: .milliseconds(80))
        precondition(child.isRunning && !FileManager.default.fileExists(atPath: mark), "second process entered while the first held the lease")

        var entered = false
        let cancelled = Task { @MainActor in
            let lease = try await DesktopLease.acquire(testPath: path)
            defer { lease.release() }
            entered = true
        }
        try await Task.sleep(for: .milliseconds(30))
        cancelled.cancel()
        do { try await cancelled.value; preconditionFailure("cancelled waiter entered") }
        catch { precondition(error is CancellationError) }
        precondition(!entered)
        held.release()
        for _ in 0..<100 where !FileManager.default.fileExists(atPath: mark) { try await Task.sleep(for: .milliseconds(10)) }
        precondition(FileManager.default.fileExists(atPath: mark), "second process did not acquire after release")

        // Simulate an ungraceful crash: the kernel releases ownership, without
        // deleting/replacing the lock inode or relying on stale PID files.
        kill(child.processIdentifier, SIGKILL)
        while child.isRunning { try await Task.sleep(for: .milliseconds(10)) }
        let recovered = try await DesktopLease.acquire(testPath: path)
        recovered.release()
        let symlink = directory.appendingPathComponent("symlink").path
        try FileManager.default.createSymbolicLink(atPath: symlink, withDestinationPath: path)
        do { _ = try await DesktopLease.acquire(testPath: symlink); preconditionFailure("followed a symlink") }
        catch { precondition(error is LeaseError) }
        print("PASS desktop coordination: separate processes serialize, cancelled waiters never enter, crash releases ownership, symlinks rejected")
    }
}
