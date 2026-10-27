import AppKit
import Foundation
import Darwin

if let code = ForegroundActivation.runIfRequested(CommandLine.arguments) { exit(code) }

let app = NSApplication.shared
// AppKit may activate an accessory app while finishing launch if its first
// request already created a window. Keep launch prohibited, then enable the
// nonactivating preview before accepting any requests.
app.setActivationPolicy(.prohibited)
// Closed output pipes must not terminate us before input cleanup can unwind.
signal(SIGPIPE, SIG_IGN)

if CommandLine.arguments.contains("--check-permissions") || CommandLine.arguments.contains("--request-permissions") {
    do { try writeJSON(Driver.permissionStatus(request: CommandLine.arguments.contains("--request-permissions"))) }
    catch { exit(1) }
    exit(0)
}

if let metadata = try? JSONSerialization.data(withJSONObject: [
    "kind": "backend", "version": "0.0.1",
    "desktopMode": ProcessInfo.processInfo.environment["COMPUTER_USE_DESKTOP"] ?? "shared",
    "executable": Bundle.main.executablePath ?? CommandLine.arguments[0]
], options: [.sortedKeys]), let line = String(data: metadata, encoding: .utf8) { diagnose(line) }

let driver = Driver()

// Terminal/session hangup must drain input cleanup just like cancellation or
// EOF. Its default disposition would exit before releasing held input and cleaning up the pointer.
let terminationSignals = [SIGTERM, SIGINT, SIGHUP].map { number in
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { MainActor.assumeIsolated { driver.shutdown(code: 0) } }
    source.resume()
    return source
}

@MainActor
func startReading() {
    // Blocking stdin reads stay off the AppKit main run loop.
    DispatchQueue.global(qos: .userInitiated).async {
        var pending = Data()
        do {
            var buffer = [UInt8](repeating: 0, count: 65536)
            while true {
                // POSIX read returns available pipe bytes; FileHandle may wait to fill its buffer.
                let count = buffer.withUnsafeMutableBytes { Darwin.read(STDIN_FILENO, $0.baseAddress, $0.count) }
                if count == 0 { break }
                if count < 0 {
                    if errno == EINTR { continue }
                    throw DriverError("stdin read failed: \(errno)")
                }
                pending.append(contentsOf: buffer.prefix(count))
                guard pending.count <= 64 * 1024 * 1024 else { throw DriverError("request frame too large") }
                while let end = pending.firstIndex(of: 10) {
                    var line = Data(pending[..<end])
                    pending.removeSubrange(...end)
                    if line.last == 13 { line.removeLast() }
                    let message = line
                    // Keep arrival order, including a subsequent EOF. receive
                    // starts a tracked task without blocking the AppKit loop.
                    DispatchQueue.main.async { driver.receive(message) }
                }
            }
            if !pending.isEmpty { diagnose("protocol: incomplete final frame") }
        } catch { diagnose("stdin: \(error)") }
        DispatchQueue.main.async { driver.shutdown(code: 0) }
    }
}

@MainActor
final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.async {
            NSApplication.shared.setActivationPolicy(.accessory)
            startReading()
        }
    }
}

let delegate = ApplicationDelegate()
app.delegate = delegate
app.run()
