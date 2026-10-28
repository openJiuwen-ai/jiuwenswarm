import AppKit

/// A single explicit paste temporarily owns the clipboard. Materialize every
/// existing representation before changing it; preserve detected later copies.
@MainActor
final class PasteboardTransaction {
    enum RestoreResult {
        case restored, ownershipLost, failed
    }

    private typealias Contents = [[NSPasteboard.PasteboardType: Data]]
    private let pasteboard: NSPasteboard
    private let original: Contents
    private let initialChange: Int
    private var ownedChange: Int?
    private var ownedContents: Contents?

    init(_ pasteboard: NSPasteboard = .general) throws {
        self.pasteboard = pasteboard
        initialChange = pasteboard.changeCount
        original = try Self.contents(of: pasteboard)
        guard pasteboard.changeCount == initialChange else {
            throw DriverError("clipboard changed or could not be read before paste; no paste sent", code: .inputUnavailable)
        }
    }

    private static func contents(of pasteboard: NSPasteboard) throws -> Contents {
        var items = Contents()
        for item in pasteboard.pasteboardItems ?? [] {
            var data = [NSPasteboard.PasteboardType: Data]()
            for type in item.types {
                guard let bytes = item.data(forType: type) else {
                    throw DriverError("current clipboard cannot be fully preserved; no paste sent", code: .inputUnavailable)
                }
                data[type] = bytes
            }
            items.append(data)
        }
        guard !items.isEmpty || (pasteboard.types ?? []).isEmpty else {
            throw DriverError("clipboard changed or could not be read before paste; no paste sent", code: .inputUnavailable)
        }
        return items
    }

    /// changeCount is an ownership generation, not a content revision. Another
    /// writer can replace a representation without calling clearContents(), so
    /// retaining the generation alone does not authorize overwriting its copy.
    private func matches(_ contents: Contents, generation: Int) -> Bool {
        guard pasteboard.changeCount == generation,
              let current = try? Self.contents(of: pasteboard), current == contents else { return false }
        return pasteboard.changeCount == generation
    }

    func install(_ text: String) throws {
        let item = NSPasteboardItem()
        guard item.setString(text, forType: .string), let data = item.data(forType: .string) else {
            throw DriverError("could not prepare clipboard text; no paste sent", code: .inputUnavailable)
        }
        let temporary: Contents = [[.string: data]]
        guard matches(original, generation: initialChange) else {
            throw DriverError("clipboard changed before paste; no paste sent", code: .inputUnavailable)
        }
        // NSPasteboard has no compare-and-swap operation. Check on both sides
        // of our mutations and never adopt a later generation. A simultaneous
        // external write between the check and clear cannot be made atomic.
        ownedChange = pasteboard.clearContents()
        ownedContents = []
        try validateOwnership()
        let written = pasteboard.writeObjects([item])
        if let ownedChange, matches(temporary, generation: ownedChange) {
            ownedContents = temporary
        }
        guard written, ownedContents == temporary else {
            throw DriverError("could not place text on the clipboard; no paste sent", code: .inputUnavailable)
        }
        try validateOwnership()
    }

    func validateOwnership() throws {
        guard let ownedChange, let ownedContents, matches(ownedContents, generation: ownedChange) else {
            throw DriverError("clipboard changed before the paste command; no further input sent", code: .inputUnavailable,
                next: "inspect the target before deciding whether to paste again")
        }
    }

    /// Once any paste-chord input was posted, cancellation must not remove the
    /// payload before the application has had the ordinary asynchronous handoff
    /// interval. The timer ignores task cancellation but is always bounded.
    /// It does not prove that the application actually consumed the clipboard.
    func finish(afterInput: Bool) async -> RestoreResult {
        if afterInput {
            await withCheckedContinuation { continuation in
                DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(500)) { continuation.resume() }
            }
        }
        return restore()
    }

    /// The optional writer allows a named-board test to reproduce service write
    /// failure; ordinary paste operations always use NSPasteboard.writeObjects.
    @discardableResult func restore(writeItems: (([NSPasteboardItem]) -> Bool)? = nil) -> RestoreResult {
        guard let ownedChange, let ownedContents, matches(ownedContents, generation: ownedChange) else {
            return .ownershipLost
        }
        var items = [NSPasteboardItem]()
        for representations in original {
            let item = NSPasteboardItem()
            for (type, data) in representations {
                guard item.setData(data, forType: type) else { return .failed }
            }
            items.append(item)
        }
        // Preparing every representation first avoids clearing the temporary
        // clipboard and only then discovering an unrepresentable original item.
        guard matches(ownedContents, generation: ownedChange) else { return .ownershipLost }
        self.ownedChange = nil
        self.ownedContents = nil
        let restoreChange = pasteboard.clearContents()
        guard matches([], generation: restoreChange) else { return .ownershipLost }
        if items.isEmpty { return .restored }
        let written = writeItems?(items) ?? pasteboard.writeObjects(items)
        guard pasteboard.changeCount == restoreChange else { return .ownershipLost }
        guard written else { return .failed }
        guard matches(original, generation: restoreChange) else {
            return .ownershipLost
        }
        return .restored
    }
}
