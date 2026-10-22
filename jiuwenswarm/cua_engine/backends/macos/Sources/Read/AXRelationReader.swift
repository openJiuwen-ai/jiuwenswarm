import ApplicationServices

/// Native relation acquisition shared by tree reads and explicit continuation.
/// It preserves ordered identities; it does not infer labels, owners or editors.
@MainActor final class AXRelationReader {
    static let tableArrays = ["AXRows", "AXVisibleRows", "AXSelectedRows", "AXColumns", "AXVisibleColumns",
        "AXSelectedColumns", "AXVisibleCells", "AXSelectedCells", "AXRowHeaderUIElements", "AXColumnHeaderUIElements",
        "AXDisclosedRows"]
    static let arrayProperties = Set(["AXChildren", "AXServesAsTitleForUIElements", "AXOwns", "AXLinkedUIElements", "AXSelectedChildren"] + tableArrays)
    struct Page {
        let source: String
        let property: String
        let offset: Int
        let total: Int
        let prefix: [AXUIElement]
        var targets: ArraySlice<AXUIElement> { prefix.dropFirst(offset) }
    }
    enum Result {
        case page(Page)
        case failure([String: Any])
    }
    private let pages = NativeRelationPages<AXUIElement>(equal: { CFEqual($0, $1) })

    func read(_ element: AXUIElement, source: String, property: String, limit: Int,
              cursor: String? = nil, withinBudget: () throws -> Bool = { true }) throws -> Result {
        let selected = try pages.select(cursor, source: source, property: property)
        let isArray = Self.arrayProperties.contains(property)
        var prefix = [AXUIElement]()
        let total: Int
        func failure(_ error: AXError) -> Result {
            .failure(AXFactEncoding.relationFailure(source: source, property: property, error: error))
        }
        func timedOut() -> Result {
            .failure(AXFactEncoding.relationFailure(source: source, property: property))
        }
        guard try withinBudget() else { return timedOut() }
        if isArray {
            var count: CFIndex = 0
            let status = AXUIElementGetAttributeValueCount(element, property as CFString, &count)
            guard status == .success, count >= 0 else { return failure(status == .success ? .failure : status) }
            total = count
            try pages.validate(selected, total: total, prefix: selected.prefix)
            let length = min(total, selected.offset + max(0, limit), pages.maxPrefix)
            if length > 0 {
                guard try withinBudget() else { return timedOut() }
                var raw: CFArray?
                // The prefix and the next page are fetched together. A bare
                // numeric offset would silently skip inserted/reordered items.
                let status = AXUIElementCopyAttributeValues(element, property as CFString, 0, length, &raw)
                guard status == .success, let values = raw as? [AXUIElement], values.count == length else {
                    return failure(status == .success ? .failure : status)
                }
                prefix = values
            }
            guard try withinBudget() else { return timedOut() }
            var after: CFIndex = 0
            let checked = AXUIElementGetAttributeValueCount(element, property as CFString, &after)
            guard checked == .success else { return failure(checked) }
            guard after == total else {
                throw DriverError("native relation changed while reading its page; read its source again", code: .snapshotExpired)
            }
        } else {
            var raw: CFTypeRef?
            let status = AXUIElementCopyAttributeValue(element, property as CFString, &raw)
            guard status == .success else { return failure(status) }
            guard let raw, CFGetTypeID(raw) == AXUIElementGetTypeID() else { return failure(.failure) }
            total = 1
            if limit > 0 { prefix = [unsafeDowncast(raw, to: AXUIElement.self)] }
        }
        try pages.validate(selected, total: total, prefix: prefix)
        return .page(Page(source: source, property: property, offset: selected.offset, total: total, prefix: prefix))
    }

    /// Only registered/returned targets are consumed. If a collection budget
    /// ends during registration, a continuation starts at the first omitted one.
    func encode(_ page: Page, targets: [String]) -> [String: Any] {
        precondition(targets.count <= page.targets.count)
        let end = page.offset + targets.count
        var value: [String: Any] = ["source": page.source, "property": page.property,
            "targets": targets, "total": page.total, "offset": page.offset,
            "certainty": "confirmed", "state": end == page.total && page.offset == 0 ? "complete" : "partial"]
        if end < page.total {
            if let cursor = pages.retain(source: page.source, property: page.property, total: page.total,
                                         prefix: Array(page.prefix.prefix(end))) {
                value["cursor"] = cursor
                value["detail"] = "native relation has an unread continuation"
            } else {
                value["detail"] = "native relation continuation retention budget; unread targets are not captured"
            }
        } else if page.offset > 0 { value["detail"] = "continuation page; earlier targets belong to the preceding observation" }
        return value
    }

    func retainAvailableCursors(_ relations: inout [[String: Any]]) {
        for index in relations.indices {
            if let cursor = relations[index]["cursor"] as? String, !pages.contains(cursor) {
                relations[index].removeValue(forKey: "cursor")
                relations[index]["detail"] = "native relation continuation retention budget; read its source again"
            }
        }
    }
}
