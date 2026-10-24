@main struct NativeReadinessTests {
    static func main() {
        precondition(NativeReadiness().ready)
        let input = [("receiver", "yes"), ("selection", "no")]
        let command = NativeReadiness(input)
        precondition(command.state == "no" && command.pending == ["selection"])
        let unknown = NativeReadiness([("receiver", "yes"), ("selection", "unknown")])
        precondition(unknown.state == "unknown" && unknown.pending == ["selection"])
        let changed = NativeReadiness([("receiver", "no"), ("selection", "unknown"), ("geometry", "yes")])
        precondition(changed.state == "no" && changed.pending == ["receiver", "selection"])
        let ready = NativeReadiness([("receiver", "yes"), ("selection", "yes"), ("geometry", "yes")])
        precondition(ready.ready && ready.pending.isEmpty)
        precondition(ready.requiringEnabled(false).pending == ["enabled"])
        precondition(unknown.requiringEnabled(false).pending == ["selection", "enabled"])
        precondition(unknown.requiringEnabled(false).state == "no")
        precondition(unknown.requiringEnabled(true).state == "unknown")
        precondition(ready.requiringEnabled(nil).ready)
        print("PASS per-unit readiness preserves known failures and unknown evidence")
    }
}
