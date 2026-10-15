import Foundation

@main struct NativeRelationPagesTests {
    @MainActor static func main() throws {
        let pages = NativeRelationPages<Int>(maxEntries: 3, maxPrefix: 4, maxElements: 6, equal: ==)
        let first = try pages.select(nil, source: "node", property: "children")
        assert(first.offset == 0 && first.total == nil)
        let cursor = pages.retain(source: "node", property: "children", total: 5, prefix: [1, 2])!
        let next = try pages.select(cursor, source: "node", property: "children")
        assert(next.offset == 2)
        try pages.validate(next, total: 5, prefix: [1, 2, 3, 4])
        for (total, prefix) in [(6, [1, 2, 3]), (5, [0, 1, 2]), (5, [2, 1, 3]), (5, [1])] {
            do { try pages.validate(next, total: total, prefix: prefix); fatalError("changed relation accepted") }
            catch let error as DriverError { assert(error.code == .snapshotExpired) }
        }
        for (source, property) in [("other", "children"), ("node", "labels")] {
            do { _ = try pages.select(cursor, source: source, property: property); fatalError("cross-bound continuation") }
            catch let error as DriverError { assert(error.code == .invalidRequest) }
        }
        // Reading the same cursor twice does not advance it or discard evidence.
        let repeated = try pages.select(cursor, source: "node", property: "children")
        assert(repeated.prefix == [1, 2])
        assert(pages.retain(source: "node", property: "children", total: 5, prefix: [1, 2, 3, 4]) == nil)
        assert(pages.retain(source: "node", property: "children", total: 2, prefix: [1, 2]) == nil)
        _ = pages.retain(source: "a", property: "children", total: 9, prefix: [1, 2, 3])
        _ = pages.retain(source: "b", property: "children", total: 9, prefix: [1, 2, 3])
        assert(!pages.contains(cursor), "retained native handles must have a shared bound")
        do { _ = try pages.select(cursor, source: "node", property: "children"); fatalError("expired cursor accepted") }
        catch let error as DriverError { assert(error.code == .snapshotExpired) }
        print("PASS native relation pages: exact source, stable prefix, mutation, repeatability and bounded retention")
    }
}
