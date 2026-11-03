/// Display time only. No input timing, application state or desktop policy.
struct AgentPointerVisualState {
    private(set) var active = false
    private var idleSince: Double?

    mutating func begin() { active = true }
    mutating func finish(at time: Double) { active = false; idleSince = time }
    mutating func observeNewWindow(at time: Double) { idleSince = time }

    func opacity(at time: Double) -> Float {
        guard !active, let idleSince, time - idleSince >= AgentPointerAssets.idleDelay else { return 1 }
        return Float(AgentPointerAssets.idleOpacity)
    }
}
