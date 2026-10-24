import Foundation

@main struct RequestLedgerTests {
    static func main() {
        let ledger = RequestLedger(maxEntries: 2, maxBytes: 8)
        let request = Data("intent".utf8), response = Data("result".utf8)
        guard case .start = ledger.begin(id: 1, request: request) else { fatalError() }
        guard case .inProgress = ledger.begin(id: 1, request: request) else { fatalError() }
        guard case .conflict = ledger.begin(id: 1, request: Data("other".utf8)) else { fatalError() }
        ledger.finish(id: 1, response: response)
        guard case .replay(let saved) = ledger.begin(id: 1, request: request), saved == response else { fatalError() }
        guard case .start = ledger.begin(id: 2, request: request) else { fatalError() }
        ledger.finish(id: 2, response: response)
        guard case .expired = ledger.begin(id: 1, request: request) else { fatalError("eviction replayed a write") }
        guard case .replay = ledger.begin(id: 2, request: request) else { fatalError() }
        guard case .start = ledger.begin(id: 5, request: request) else { fatalError() }
        guard case .expired = ledger.begin(id: 3, request: request) else { fatalError("IDs must be monotonic") }
        ledger.finish(id: 5, response: Data(repeating: 0, count: 9))
        guard case .expired = ledger.begin(id: 5, request: request) else { fatalError("oversized result must not become a new request") }
        let countBound = RequestLedger(maxEntries: 1)
        guard case .start = countBound.begin(id: 1, request: request), case .start = countBound.begin(id: 2, request: request) else { fatalError() }
        countBound.finish(id: 2, response: response)
        guard case .inProgress = countBound.begin(id: 1, request: request), case .expired = countBound.begin(id: 2, request: request) else { fatalError() }
        print("PASS request ledger: in-flight dedup, identical replay, conflict, monotonic IDs, count/byte eviction and oversized replies")
    }
}
