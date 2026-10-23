import Foundation
import CoreFoundation

func object(_ value: Any?, keys: Set<String>) throws -> [String: Any] {
    guard let value = value as? [String: Any], Set(value.keys) == keys else {
        throw DriverError("invalid object fields; expected \(keys.sorted())", code: .invalidRequest)
    }
    return value
}

func string(_ value: Any?, label: String, nonempty: Bool = true) throws -> String {
    guard let value = value as? String,
          !nonempty || !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        throw DriverError("\(label) must be \(nonempty ? "a nonempty" : "a") string", code: .invalidRequest)
    }
    return value
}

func number(_ value: Any?, label: String) throws -> Double {
    guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else {
        throw DriverError("\(label) must be a finite number", code: .invalidRequest)
    }
    return n.doubleValue
}

func boolean(_ value: Any?, label: String) throws -> Bool {
    guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else {
        throw DriverError("\(label) must be a boolean", code: .invalidRequest)
    }
    return n.boolValue
}

func validateText(_ text: String) throws {
    for scalar in text.unicodeScalars {
        let v = scalar.value
        if v <= 31 || (127...159).contains(v) || v == 0x2028 || v == 0x2029 {
            throw DriverError("typeText: use pressKey for control keys; text must be one line", code: .invalidRequest)
        }
    }
}

func validatePasteText(_ text: String) throws {
    for scalar in text.unicodeScalars {
        let v = scalar.value
        if (v <= 31 && ![9, 10, 13].contains(v)) || (127...159).contains(v) {
            throw DriverError("paste: text contains unsupported control characters", code: .invalidRequest)
        }
    }
}

func writeJSON(_ value: [String: Any]) throws {
    var bytes = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed])
    bytes.append(10)
    try FileHandle.standardOutput.write(contentsOf: bytes)
}

enum RequestDiagnostics {
    @TaskLocal static var id: Int64?
    @TaskLocal static var metrics: NativeMetrics?
    @TaskLocal static var deadline: TimeInterval?
    @TaskLocal static var observationBudget: ObservationBudget?

    static func checkCancellation() throws {
        try Task.checkCancellation()
        if let deadline, ProcessInfo.processInfo.systemUptime >= deadline { throw RequestDeadlineExceeded() }
    }

    /// Only read-only collectors opt in. Input validation never uses Dc.
    static func checkObservationBudget() throws {
        try checkCancellation()
        if let observationBudget, !observationBudget.canCollect() { throw ObservationBudgetExceeded() }
    }

    static func rethrowCancellation(_ error: Error) throws {
        if error is CancellationError || error is RequestDeadlineExceeded { throw error }
        try checkCancellation()
    }
}

struct RequestDeadlineExceeded: Error, CustomStringConvertible { let description = "request deadline exceeded" }

func diagnose(_ message: String) {
    writeDiagnostic(message)
}

func diagnose(_ fields: [String: Any]) {
    writeDiagnostic(fields)
}

private func writeDiagnostic(_ message: Any) {
    if let id = RequestDiagnostics.id,
       var data = try? JSONSerialization.data(withJSONObject: ["requestId": id, "message": message]) {
        data.append(10)
        try? FileHandle.standardError.write(contentsOf: data)
    } else {
        if let text = message as? String {
            try? FileHandle.standardError.write(contentsOf: Data((text + "\n").utf8))
        } else if var data = try? JSONSerialization.data(withJSONObject: message) {
            data.append(10)
            try? FileHandle.standardError.write(contentsOf: data)
        }
    }
}
