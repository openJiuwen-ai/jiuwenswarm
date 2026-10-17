import Foundation

@main struct RequestLifetimeTests {
    @MainActor static func main() async {
        // The first command owns its lifetime, and normal completion permits
        // a later command. Overlap never replaces the running operation.
        let normal = RequestLifetime()
        var completed = 0
        precondition(normal.start { completed += 1 })
        precondition(!normal.start { completed += 100 })
        while completed == 0 { await Task.yield() }
        precondition(normal.start { completed += 1 })
        while completed == 1 { await Task.yield() }
        precondition(completed == 2)

        // Cancellation before the operation starts must dispatch no action.
        let queued = RequestLifetime()
        var dispatched = false
        precondition(queued.start { dispatched = true })
        let queuedTask = queued.cancel()
        precondition(queued.stopping)
        precondition(!queued.start { dispatched = true })
        await queuedTask?.value
        precondition(!dispatched)

        // A cancelled operation gets to unwind. Shutdown waits for completion,
        // including async cleanup, before its caller can terminate the process.
        let held = RequestLifetime()
        var entered = false, released = false, finished = false, extraActions = 0
        precondition(held.start {
            defer { released = true }
            entered = true
            do {
                try await Task.sleep(for: .seconds(60))
                extraActions += 1
            } catch {
                precondition(error is CancellationError)
                // Exercise suspension during cleanup without ignoring cancellation.
                await Task.yield()
                finished = true
            }
        })
        while !entered { await Task.yield() }
        let running = held.cancel()
        let same = held.cancel()
        precondition(!released && !finished)
        precondition(!held.start { extraActions += 1 })
        await running?.value
        await same?.value
        precondition(released && finished && extraActions == 0)
        precondition(held.cancel() == nil)

        // A request deadline cancels just that operation and keeps the session
        // alive, but new work cannot enter while cleanup is still running.
        let timed = RequestLifetime()
        var timedOut = false, unwound = false
        precondition(timed.start(timeoutMs: 20, onTimeout: { timedOut = true }) {
            do { try await Task.sleep(for: .seconds(60)); preconditionFailure("deadline ignored") }
            catch {
                precondition(error is CancellationError)
                precondition(!timed.start { preconditionFailure("overlapped cleanup") })
                await Task.yield()
                unwound = true
            }
        })
        while !unwound { await Task.yield() }
        precondition(timedOut && !timed.stopping)
        var nextCompleted = false
        precondition(timed.start { nextCompleted = true })
        while !nextCompleted { await Task.yield() }

        // A control-frame cancellation arriving before execution must still
        // permit the driver to produce a terminal "no action" error reply.
        let early = RequestLifetime()
        var reported = false
        precondition(early.start { reported = Task.isCancelled })
        early.cancelActive()
        while !reported { await Task.yield() }
        print("PASS request lifetime: completion, overlap, pre-start cancellation, unwind before shutdown, no new work or replay")
    }
}
