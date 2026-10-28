import AppKit

@main struct PasteboardTransactionTests {
    @MainActor static func main() async throws {
        // No general clipboard, GUI activation or synthetic input is involved.
        let board = NSPasteboard(name: NSPasteboard.Name("computer-use-transaction-tests-" + UUID().uuidString))
        defer { board.releaseGlobally() }
        let custom = NSPasteboard.PasteboardType("local.computer-use.test.binary")
        func seed() -> [[NSPasteboard.PasteboardType: Data]] {
            board.clearContents()
            let first = NSPasteboardItem(), second = NSPasteboardItem()
            precondition(first.setString("original 中文", forType: .string))
            precondition(first.setData(Data([0, 1, 2, 255]), forType: custom))
            precondition(second.setString("second item", forType: .string))
            precondition(second.setData(Data("<b>second</b>".utf8), forType: .html))
            precondition(board.writeObjects([first, second]))
            return snapshot()
        }
        func snapshot() -> [[NSPasteboard.PasteboardType: Data]] {
            (board.pasteboardItems ?? []).map { item in
                Dictionary(uniqueKeysWithValues: item.types.map { ($0, item.data(forType: $0)!) })
            }
        }
        func expectInputFailure(_ action: () throws -> Void) {
            do { try action(); preconditionFailure("Expected refusal before using a changed clipboard") }
            catch let error as DriverError { precondition(error.code == .inputUnavailable) }
            catch { preconditionFailure("Unexpected error: \(error)") }
        }

        board.clearContents()
        let empty = try PasteboardTransaction(board)
        try empty.install("temporary 中文\nline 2")
        try empty.validateOwnership()
        precondition(board.string(forType: .string) == "temporary 中文\nline 2")
        precondition(empty.restore() == .restored)
        precondition((board.types ?? []).isEmpty && (board.pasteboardItems ?? []).isEmpty)
        precondition(empty.restore() == .ownershipLost, "A finished transaction cannot restore twice")

        let original = seed()
        let multiple = try PasteboardTransaction(board)
        try multiple.install("only this plain text")
        precondition(snapshot().count == 1 && Set(board.pasteboardItems![0].types) == [.string])
        precondition(multiple.restore() == .restored)
        precondition(snapshot() == original, "Item order and all binary/type representations must survive")

        _ = seed()
        let laterCopy = try PasteboardTransaction(board)
        try laterCopy.install("temporary")
        board.clearContents()
        precondition(board.setString("user copied later", forType: .string))
        expectInputFailure { try laterCopy.validateOwnership() }
        precondition(laterCopy.restore() == .ownershipLost)
        precondition(board.string(forType: .string) == "user copied later")

        _ = seed()
        let sameGeneration = try PasteboardTransaction(board)
        try sameGeneration.install("temporary")
        let installedGeneration = board.changeCount
        precondition(board.setString("same-generation copy", forType: .string))
        precondition(board.changeCount == installedGeneration, "Native setString must exercise the unchanged-generation case")
        expectInputFailure { try sameGeneration.validateOwnership() }
        precondition(sameGeneration.restore() == .ownershipLost)
        precondition(board.string(forType: .string) == "same-generation copy")

        _ = seed()
        let addedType = try PasteboardTransaction(board)
        try addedType.install("temporary")
        let typeGeneration = board.changeCount
        precondition(board.setData(Data([8, 9]), forType: custom))
        precondition(board.changeCount == typeGeneration)
        expectInputFailure { try addedType.validateOwnership() }
        precondition(addedType.restore() == .ownershipLost)
        precondition(board.data(forType: custom) == Data([8, 9]), "Keep newly added representations even when plain text is unchanged")

        _ = seed()
        let beforeInstall = try PasteboardTransaction(board)
        board.clearContents()
        precondition(board.setString("copy before install", forType: .string))
        let beforeRefusal = snapshot()
        expectInputFailure { try beforeInstall.install("must not replace") }
        precondition(snapshot() == beforeRefusal && beforeInstall.restore() == .ownershipLost)

        _ = seed()
        let beforeSameGeneration = try PasteboardTransaction(board)
        let snapshotGeneration = board.changeCount
        precondition(board.setString("changed after snapshot", forType: .string))
        precondition(board.changeCount == snapshotGeneration)
        let afterMutation = snapshot()
        expectInputFailure { try beforeSameGeneration.install("must not replace") }
        precondition(snapshot() == afterMutation)

        _ = seed()
        let failedRestore = try PasteboardTransaction(board)
        try failedRestore.install("temporary")
        var writes = 0
        let failed = failedRestore.restore(writeItems: { _ in writes += 1; return false })
        precondition(writes == 1 && failed == .failed, "Service write failure must differ from a preserved newer copy")
        precondition(failedRestore.restore() == .ownershipLost, "Do not replay a failed restoration over later activity")

        _ = seed()
        let duringRestore = try PasteboardTransaction(board)
        try duringRestore.install("temporary")
        let concurrent = duringRestore.restore(writeItems: { _ in
            board.clearContents()
            return board.setString("concurrent copy while restoring", forType: .string)
        })
        precondition(concurrent == .ownershipLost)
        precondition(board.string(forType: .string) == "concurrent copy while restoring")

        let cancellationOriginal = seed()
        let interrupted = try PasteboardTransaction(board)
        try interrupted.install("pending application paste")
        let start = ContinuousClock.now
        let finishing = Task { @MainActor in await interrupted.finish(afterInput: true) }
        finishing.cancel()
        try await Task.sleep(for: .milliseconds(100))
        precondition(board.string(forType: .string) == "pending application paste", "Cancellation must not erase an in-flight payload")
        let finished = await finishing.value
        precondition(finished == .restored && ContinuousClock.now - start >= .milliseconds(490))
        precondition(snapshot() == cancellationOriginal)

        _ = seed()
        let changedDuringHandoff = try PasteboardTransaction(board)
        try changedDuringHandoff.install("temporary handoff")
        let waiting = Task { @MainActor in await changedDuringHandoff.finish(afterInput: true) }
        try await Task.sleep(for: .milliseconds(100))
        board.clearContents()
        precondition(board.setString("new user copy during handoff", forType: .string))
        let preserved = await waiting.value
        precondition(preserved == .ownershipLost && board.string(forType: .string) == "new user copy during handoff")

        let beforeInput = seed()
        let noDispatch = try PasteboardTransaction(board)
        try noDispatch.install("not dispatched")
        let immediatelyRestored = await noDispatch.finish(afterInput: false)
        precondition(immediatelyRestored == .restored && snapshot() == beforeInput)
        print("PASS: named clipboard preserves every representation, later copies, cancellation handoff and restore failures")
    }
}
