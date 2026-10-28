import Foundation

@main struct PairedInputTests {
    enum Failure: Error { case changed }
    @MainActor static func main() async {
        var events = [String](), generation = 1, window = 1, settlements = 0, skipped = 0
        func pair() -> PairedInput<String> {
            let original = generation
            return PairedInput(post: { events.append($0) }, canRelease: { generation == original },
                skipped: { skipped += 1 }, settle: { settlements += 1 })
        }
        // Release ignores changed window/focus preconditions but never targets
        // a replacement process.
        let changed = pair()
        do {
            try await changed.run {
                changed.press("down@1", release: "up@1")
                window = 2
                throw Failure.changed
            }
        } catch {}
        precondition(window == 2 && events == ["down@1", "up@1"] && settlements == 1)
        events = []
        let replaced = pair()
        do {
            try await replaced.run {
                replaced.press("down@1", release: "up@1")
                generation = 2
                throw Failure.changed
            }
        } catch {}
        precondition(events == ["down@1"] && skipped == 1 && settlements == 1)
        events = []
        let early = pair()
        let cancelled = Task { @MainActor in try? await early.run { early.press("down", release: "up") } }
        cancelled.cancel(); await cancelled.value
        precondition(events.isEmpty && settlements == 1)

        let double = pair()
        var started = false
        let interrupted = Task { @MainActor in
            try? await double.run {
                double.press("down1", release: "up1"); started = true
                try await Task.sleep(for: .seconds(60))
                double.release()
                double.press("down2", release: "up2")
            }
        }
        while !started { await Task.yield() }
        interrupted.cancel(); await interrupted.value
        double.release() // Completion/cleanup cannot release again.
        precondition(events == ["down1", "up1"] && settlements == 2)
        events = []
        let chord = pair()
        try! await chord.run {
            chord.press("shiftDown", release: "shiftUp")
            chord.press("keyDown", release: "keyUp")
            chord.release(); chord.release()
        }
        precondition(events == ["shiftDown", "keyDown", "keyUp", "shiftUp"] && settlements == 3)

        // A genuine cancelled Task still waits through asynchronous cleanup.
        events = []; started = false
        let timed = PairedInput<String>(post: { events.append($0) }, canRelease: { true })
        let timer = Task { @MainActor in
            try? await timed.run {
                timed.press("down", release: "up"); started = true
                try await Task.sleep(for: .seconds(60))
            }
        }
        while !started { await Task.yield() }
        let time = ContinuousClock.now
        timer.cancel(); await timer.value
        precondition(ContinuousClock.now - time >= .milliseconds(25))
        precondition(events == ["down", "up"])
        // A wheel event has no held release but still needs the same bounded
        // delivery settlement, including cancellation immediately after post.
        let wheel = PairedInput<String>(post: { _ in }, canRelease: { true }, settleUnpaired: true,
            settle: { settlements += 1 })
        do { try await wheel.run { throw Failure.changed } } catch {}
        precondition(settlements == 4)
        print("PASS input pairing: cancellation, window change, process replacement, double click, chord, asynchronous release settlement")
    }
}
