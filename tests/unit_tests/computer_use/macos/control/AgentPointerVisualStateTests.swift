@main struct AgentPointerVisualStateTests {
    static func main() {
        var state = AgentPointerVisualState()
        state.observeNewWindow(at: 10)
        precondition(state.opacity(at: 14.99) == 1)
        precondition(state.opacity(at: 15) == 0.4)
        // Repeated rendering/observations don't mutate the idle clock.
        for time in 15...50 { precondition(state.opacity(at: Double(time)) == 0.4) }
        state.begin()
        precondition(state.opacity(at: 51) == 1)
        precondition(state.opacity(at: 500) == 1, "a long input/drag stays active without periodic new events")
        state.finish(at: 501)
        precondition(state.opacity(at: 505.99) == 1)
        precondition(state.opacity(at: 506) == 0.4, "idle counts from completion, not request start")
        state.observeNewWindow(at: 600)
        precondition(state.opacity(at: 600) == 1)
        precondition(state.opacity(at: 606) == 0.4)
        print("PASS pointer idle: deadline, read-only stability, immediate wake, long operation, completion, new window")
    }
}
