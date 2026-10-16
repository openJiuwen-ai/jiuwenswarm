import Foundation
import Darwin

/// Parent PID describes current ancestry, not the lifetime of a process.
struct NativeProcessIdentity: Hashable {
    let pid: pid_t
    let parent: pid_t
    let seconds: UInt64
    let microseconds: UInt64
    let executable: String

    static func read(_ pid: pid_t) -> Self? {
        read(pid, onFailure: nil)
    }

    struct ReadFailure {
        let operation: String
        let result: Int32
        let error: Int32
        let expectedBytes: Int32?

        var diagnostic: [String: Any] {
            var value: [String: Any] = ["operation": operation, "result": result, "errno": error]
            if let expectedBytes { value["expectedBytes"] = expectedBytes }
            return value
        }
    }

    /// Report only the syscall that actually failed. Do not repeat the read to
    /// diagnose it: a subsequent successful lookup would hide a transient error.
    static func read(_ pid: pid_t, onFailure: ((ReadFailure) -> Void)?) -> Self? {
        var info = proc_bsdinfo()
        let expected = Int32(MemoryLayout<proc_bsdinfo>.size)
        let infoResult = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, expected)
        guard infoResult == expected else {
            let error = errno
            onFailure?(ReadFailure(operation: "proc_pidinfo", result: infoResult, error: error, expectedBytes: expected))
            return nil
        }
        var bytes = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        let pathResult = proc_pidpath(pid, &bytes, UInt32(bytes.count))
        guard pathResult > 0 else {
            let error = errno
            onFailure?(ReadFailure(operation: "proc_pidpath", result: pathResult, error: error, expectedBytes: nil))
            return nil
        }
        return Self(pid: pid, parent: pid_t(info.pbi_ppid), seconds: info.pbi_start_tvsec,
                    microseconds: info.pbi_start_tvusec, executable: String(cString: bytes))
    }

    static func isGone(_ pid: pid_t) -> Bool { kill(pid, 0) < 0 && errno == ESRCH }
    var incarnation: String { "\(seconds).\(microseconds)" }
    static func == (a: Self, b: Self) -> Bool {
        a.pid == b.pid && a.seconds == b.seconds && a.microseconds == b.microseconds && a.executable == b.executable
    }
    func hash(into hasher: inout Hasher) {
        hasher.combine(pid); hasher.combine(seconds); hasher.combine(microseconds); hasher.combine(executable)
    }
    func wasBorn(afterOrWith other: Self) -> Bool {
        seconds > other.seconds || (seconds == other.seconds && microseconds >= other.microseconds)
    }
}

/// Retain proven ancestry for the same two live process incarnations. Adoption
/// by launchd does not undo origin; matching a path alone never establishes it.
@MainActor final class NativeProcessOwnership {
    private struct Claim: Hashable {
        let member: NativeProcessIdentity
        let owner: NativeProcessIdentity
    }
    private var known = Set<Claim>()

    func belongs(_ process: NativeProcessIdentity, to owner: NativeProcessIdentity, bundle: URL,
                 read: (pid_t) -> NativeProcessIdentity? = NativeProcessIdentity.read) -> Bool {
        let bundle = bundle.standardizedFileURL.resolvingSymlinksInPath()
        guard process.pid != owner.pid, bundle.pathExtension.lowercased() == "app",
              URL(fileURLWithPath: process.executable).standardizedFileURL.resolvingSymlinksInPath().path.hasPrefix(bundle.path + "/"),
              let live = read(process.pid), live == process, read(owner.pid) == owner else { return false }
        var current = live, seen = Set<pid_t>()
        for depth in 0...16 {
            guard seen.insert(current.pid).inserted else { return false }
            if current == owner || known.contains(Claim(member: current, owner: owner)) {
                // Refuse replacement during discovery; no proof by PID alone.
                guard read(process.pid) == process, read(owner.pid) == owner else { return false }
                known.insert(Claim(member: process, owner: owner))
                return true
            }
            guard depth < 16, current.parent > 1, let parent = read(current.parent), current.wasBorn(afterOrWith: parent) else { return false }
            current = parent
        }
        return false
    }

    func prune(read: (pid_t) -> NativeProcessIdentity? = NativeProcessIdentity.read,
               isGone: (pid_t) -> Bool = NativeProcessIdentity.isGone) {
        func retained(_ identity: NativeProcessIdentity) -> Bool {
            if let current = read(identity.pid) { return current == identity }
            // An unreadable process is not necessarily dead. Operations still
            // require a fresh identity, but don't erase historical evidence.
            return !isGone(identity.pid)
        }
        known = known.filter { retained($0.member) && retained($0.owner) }
    }
}
