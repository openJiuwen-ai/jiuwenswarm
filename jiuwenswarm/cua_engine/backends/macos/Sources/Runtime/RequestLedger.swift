import Foundation
import CryptoKit

/// A bounded ledger for one live session. The high-water mark survives cache
/// eviction: forgotten requests are rejected, never admitted as new work.
final class RequestLedger {
    enum Admission { case start, replay(Data), inProgress, expired, conflict }
    private struct Entry { let fingerprint: Data; var response: Data? }
    private var entries = [Int64: Entry]()
    private var order = [Int64]()
    private var highest: Int64 = 0
    private var bytes = 0
    private let maxEntries: Int
    private let maxBytes: Int

    init(maxEntries: Int = 64, maxBytes: Int = 8 * 1024 * 1024) {
        self.maxEntries = maxEntries; self.maxBytes = maxBytes
    }

    func begin(id: Int64, request: Data) -> Admission {
        let fingerprint = Data(SHA256.hash(data: request))
        if let entry = entries[id] {
            guard entry.fingerprint == fingerprint else { return .conflict }
            return entry.response.map(Admission.replay) ?? .inProgress
        }
        guard id > highest else { return .expired }
        highest = id
        entries[id] = Entry(fingerprint: fingerprint)
        order.append(id)
        trim()
        return .start
    }

    func finish(id: Int64, response: Data) {
        guard var entry = entries[id], entry.response == nil else { return }
        entry.response = response; entries[id] = entry; bytes += response.count
        trim()
    }

    private func trim() {
        while entries.count > maxEntries || bytes > maxBytes {
            // An executing request cannot be evicted. Driver serializes actual
            // execution; rejected overlapping requests complete immediately.
            guard let index = order.firstIndex(where: { entries[$0]?.response != nil }) else { return }
            let id = order.remove(at: index)
            bytes -= entries.removeValue(forKey: id)?.response?.count ?? 0
        }
    }
}
