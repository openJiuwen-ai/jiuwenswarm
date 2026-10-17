import Foundation

@main struct NativeMetricsTests {
    @MainActor static func main() {
        var time: TimeInterval = 1
        let metrics = NativeMetrics(clock: { time })
        let preparation = metrics.start(.windowPreparation)
        time = 2
        let validation = metrics.start(.targetValidation)
        time = 3
        validation?.end()
        metrics.count(.cachedWindowPreparation)
        enum Failure: Error { case injected }
        func failedCheck() throws {
            let check = metrics.start(.targetValidation)
            defer { check?.end() }
            time = 4
            throw Failure.injected
        }
        do { try failedCheck(); preconditionFailure("failure expected") } catch {}
        preparation?.end()
        let preview = metrics.start(.windowCapture)
        let report = metrics.finish(method: "typeText", status: "error")!
        let phases = report["phases"] as! [String: [String: Any]]
        precondition(report["durationMs"] as? Double == 3000)
        precondition(report["status"] as? String == "error")
        precondition(phases["windowPreparation"]?["durationMs"] as? Double == 3000)
        precondition(phases["targetValidation"]?["durationMs"] as? Double == 2000)
        precondition(phases["targetValidation"]?["count"] as? Int == 2)
        precondition(phases["windowCapture"] == nil, "unfinished asynchronous work is excluded")
        precondition((report["counters"] as? [String: Int]) == ["cachedWindowPreparation": 1])
        time = 10
        preview?.end()
        metrics.count(.activationNotification)
        precondition(metrics.start(.targetValidation) == nil)
        precondition(metrics.finish(method: "preview", status: "ok") == nil, "completed requests emit once; late preview work cannot append another result")
        let next = NativeMetrics(clock: { time })
        let second = next.finish(method: "listApps", status: "ok")!
        precondition((second["phases"] as? [String: [String: Any]])?.isEmpty == true)
        precondition((second["counters"] as? [String: Int])?.isEmpty == true)
        print("PASS: inclusive metrics retain failed checks, close once, exclude late previews and isolate requests")
    }
}
