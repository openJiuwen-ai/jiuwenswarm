import Foundation

@main struct NativeUnitCommandTests {
    static func accepted(_ raw: Any, begin: Bool = false) -> Bool {
        do {
            if begin { _ = try NativeBeginRequest(raw) }
            else { _ = try NativeUnitCommand(raw) }
            return true
        }
        catch let error as DriverError {
            precondition(error.code == .invalidRequest)
            return false
        }
        catch { preconditionFailure("unexpected envelope error: \(error)") }
    }

    static func main() throws {
        if ["--stdio", "--begin-stdio"].contains(CommandLine.arguments.dropFirst().first ?? "") {
            while let line = readLine() {
                let value = try JSONSerialization.jsonObject(with: Data(line.utf8), options: [.fragmentsAllowed])
                print(accepted(value, begin: CommandLine.arguments[1] == "--begin-stdio") ? "true" : "false")
            }
            return
        }
        for file in CommandLine.arguments.dropFirst() {
            let source = URL(fileURLWithPath: file)
            let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: source)) as! [[String: Any]]
            for test in cases {
                let begin = test["begin"] != nil
                precondition(accepted(test[begin ? "begin" : "command"]!, begin: begin) == test["valid"] as! Bool, test["name"] as! String)
            }
            print("PASS \(cases.count) shared native wire envelopes")
        }
    }
}
