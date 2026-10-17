import ApplicationServices

@main struct AXRelationReaderTests {
    @MainActor static func main() {
        let reader = AXRelationReader()
        // Opaque identities only; these tests do not query or operate the desktop.
        let a = AXUIElementCreateApplication(100), b = AXUIElementCreateApplication(101)
        let partial = reader.encode(.init(source: "source", property: "AXChildren", offset: 0, total: 3, prefix: [a, b]), targets: ["a"])
        assert(partial["offset"] as? Int == 0 && partial["state"] as? String == "partial")
        assert(partial["cursor"] is String)
        assert(partial["targets"] as? [String] == ["a"], "unregistered targets must not be consumed")
        let empty = reader.encode(.init(source: "source", property: "AXChildren", offset: 0, total: 0, prefix: []), targets: [])
        assert(empty["state"] as? String == "complete" && empty["cursor"] == nil)
        let last = reader.encode(.init(source: "source", property: "AXChildren", offset: 1, total: 2, prefix: [a, b]), targets: ["b"])
        assert(last["offset"] as? Int == 1 && last["state"] as? String == "partial" && last["cursor"] == nil)
        var links = [partial]
        for index in 0..<300 {
            _ = reader.encode(.init(source: "node-\(index)", property: "AXChildren", offset: 0, total: 2, prefix: [a]), targets: ["a"])
        }
        reader.retainAvailableCursors(&links)
        assert(links[0]["cursor"] == nil, "evicted continuations must not be advertised at publication")
        assert(links[0]["targets"] as? [String] == ["a"])
        print("PASS AX relation pages: registered prefix, partial suffix, empty relation and honest cursor eviction")
    }
}
