import Foundation

@main struct NativeCaptureRequestTests {
    static func accepted(_ request: Any) -> Bool {
        do { _ = try NativeCaptureRequest(request); return true }
        catch let error as DriverError {
            precondition(error.code == .invalidRequest)
            return false
        }
        catch { preconditionFailure("unexpected decoder failure: \(error)") }
    }

    static func main() throws {
        if CommandLine.arguments.dropFirst().first == "--stdio" {
            while let line = readLine() {
                let value = try JSONSerialization.jsonObject(with: Data(line.utf8), options: [.fragmentsAllowed])
                print(accepted(value) ? "true" : "false")
            }
            return
        }
        let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
        for test in cases {
            precondition(accepted(test["request"]!) == test["valid"] as! Bool, test["name"] as! String)
        }
        print("PASS \(cases.count) shared native capture request contracts")
    }
}
