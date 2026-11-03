import AppKit
import ApplicationServices

/// Operation-local native observations used by Python's semantic selection.
/// This stores facts, not classifications or a second editor search algorithm.
@MainActor final class NativeSelectionBasis {
    typealias Dependency = NativeInspectQuery.Dependency
    struct Changed: Error, ClassifiedFailure, CustomStringConvertible {
        let description = "the structural facts used to select this input receiver changed"
        var code: FailureCode { .inputUnavailable }
    }
    struct Fact: Equatable {
        let data: Data
        var objects: [AXUIElement] = []
        var unavailable = false
        static func == (lhs: Self, rhs: Self) -> Bool {
            lhs.data == rhs.data && lhs.objects.count == rhs.objects.count
                && zip(lhs.objects, rhs.objects).allSatisfy { CFEqual($0, $1) }
        }
    }
    private struct Snapshot {
        let target: ApplicationElements.Target
        let scope: String
        var fields = [String: Fact]()
        var actions: Fact?
        var relations = [String: Fact]()
    }
    private struct Check {
        let key: [String]
        let target: ApplicationElements.Target
        let fact: Fact
    }
    private let access: NativeApplicationAccess
    private var snapshots = [[String]: Snapshot]()
    private var bytes = 0
    // No text, focus, selection or layout values belong to this dependency set.
    static let fields: Set<String> = ["AXRole", "AXSubrole", "AXEditable", "AXEnabled", "AXOrientation", "AXHasPopup",
        "settable.AXFocused", "settable.AXValue", "settable.AXSelectedText"]

    init(access: NativeApplicationAccess) { self.access = access }
    func clear() { snapshots.removeAll(); bytes = 0 }

    private static func data(_ value: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes])
    }
    static func scalar(value: Any?, status: [String: Any]?) throws -> Fact {
        if let state = status?["state"] as? String {
            return try Fact(data: data(["state": state]), unavailable: !["absent", "unsupported"].contains(state))
        }
        guard let value else { throw DriverError("uncollected selection field", code: .invalidRequest) }
        return try Fact(data: data(["value": value]))
    }
    static func actions(_ value: [String: Any]) throws -> Fact {
        guard let state = value["state"] as? String, let items = value["items"] as? [[String: Any]] else {
            throw DriverError("invalid action dependency", code: .invalidRequest)
        }
        let names = items.compactMap { $0["name"] as? String }.sorted()
        return try Fact(data: data(["state": state, "names": names, "total": value["total"] ?? NSNull()]),
                        unavailable: state == "unavailable")
    }
    private static func relation(_ value: [String: Any], objects: [AXUIElement]) throws -> Fact {
        let state = value["state"] as? String ?? "error"
        return try Fact(data: data(["state": state, "certainty": value["certainty"] ?? "unknown",
            "offset": value["offset"] ?? 0, "total": value["total"] ?? NSNull()]), objects: objects,
            unavailable: !["complete", "partial", "absent", "unsupported"].contains(state))
    }

    /// Retain the exact returned batch, never a later resampling of its values.
    /// Only write operations call this. Every reference stays within that lease.
    func retain(_ batch: [String: Any]) throws {
        guard let observed = (batch["observed"] as? [String: Any])?["id"] as? String,
              let scope = batch["surface"] as? String else {
            throw DriverError("selection source has no collection identity", code: .invalidRequest)
        }
        var pending = [[String]: Snapshot]()
        func snapshot(_ node: String) throws -> Snapshot {
            if let value = pending[[observed, node]] { return value }
            let target = try access.nativeReadTarget(node)
            guard target.scope == scope else { throw DriverError("selection source scope changed", code: .staleTarget) }
            return Snapshot(target: target, scope: scope)
        }
        for node in batch["nodes"] as? [[String: Any]] ?? [] {
            guard let ref = node["node"] as? String, let values = node["values"] as? [String: Any],
                  let missing = node["fieldStatus"] as? [String: Any], let actions = node["actions"] as? [String: Any] else {
                throw DriverError("invalid native selection source", code: .invalidRequest)
            }
            var value = try snapshot(ref)
            for key in node["requestedFields"] as? [String] ?? [] where Self.fields.contains(key) {
                value.fields[key] = try Self.scalar(value: values[key], status: missing[key] as? [String: Any])
            }
            value.actions = try Self.actions(actions)
            pending[[observed, ref]] = value
        }
        let relations = batch["relations"] as? [[String: Any]]
            ?? (batch["relation"] as? [String: Any]).map { [$0] } ?? []
        for edge in relations {
            guard let ref = edge["source"] as? String, let property = edge["property"] as? String,
                  let targets = edge["targets"] as? [String] else {
                throw DriverError("invalid native relation dependency", code: .invalidRequest)
            }
            var value = try snapshot(ref)
            let objects = try targets.map { try access.nativeReadTarget($0).element }
            value.relations[property] = try Self.relation(edge, objects: objects)
            pending[[observed, ref]] = value
        }
        let addedBytes = pending.values.reduce(0) { total, value in
            let facts = Array(value.fields.values) + Array(value.relations.values) + (value.actions.map { [$0] } ?? [])
            return total + 512 + facts.reduce(0) { $0 + $1.data.count + $1.objects.count * 64 }
        }
        guard snapshots.count + pending.count <= 8192, bytes + addedBytes <= 8 * 1024 * 1024 else {
            throw DriverError("operation selection evidence budget exhausted", code: .observationIncomplete)
        }
        guard pending.keys.allSatisfy({ snapshots[$0] == nil }) else {
            throw DriverError("native collection identity was reused", code: .invalidRequest)
        }
        snapshots.merge(pending) { _, new in new }; bytes += addedBytes
    }

    private func selected(_ basis: [Dependency], scope: String) throws -> [Check] {
        var checks = [Check]()
        for entry in basis {
            guard let value = snapshots[[entry.observed, entry.node]], value.scope == scope else {
                throw DriverError("selection dependency is not from this operation and surface", code: .invalidRequest)
            }
            for field in entry.fields {
                guard let fact = value.fields[field] else {
                    throw DriverError("selection field was not retained: \(field)", code: .invalidRequest)
                }
                checks.append(Check(key: [entry.node, "field", field], target: value.target, fact: fact))
            }
            if entry.actions {
                guard let fact = value.actions else { throw DriverError("action dependency was not collected", code: .invalidRequest) }
                checks.append(Check(key: [entry.node, "actions"], target: value.target, fact: fact))
            }
            for property in entry.relations {
                guard let fact = value.relations[property] else { throw DriverError("relation dependency was not collected", code: .invalidRequest) }
                let meta = try JSONSerialization.jsonObject(with: fact.data) as! [String: Any]
                checks.append(Check(key: [entry.node, "relation", property, String(meta["offset"] as! Int),
                    String(fact.objects.count)], target: value.target, fact: fact))
            }
        }
        return checks.sorted { $0.key.lexicographicallyPrecedes($1.key) }
    }

    func validate(_ basis: [Dependency], scope: String) throws { _ = try selected(basis, scope: scope) }

    /// New observation IDs with identical facts retain the preparation contract.
    /// A changed semantic fact cannot inherit a prior contract's ready marker.
    func equivalent(_ left: [Dependency], _ right: [Dependency], scope: String) -> Bool {
        guard let a = try? selected(left, scope: scope), let b = try? selected(right, scope: scope), a.count == b.count else { return false }
        return zip(a, b).allSatisfy { $0.key == $1.key && $0.fact == $1.fact }
    }

    static func compare(_ expected: Fact, _ actual: Fact) throws {
        if expected == actual { return }
        if actual.unavailable {
            throw DriverError("a structural selection dependency cannot currently be read", code: .observationIncomplete)
        }
        throw Changed()
    }

    func check(_ basis: [Dependency], scope: String) throws {
        let checks = try selected(basis, scope: scope)
        var validated = Set<String>()
        for item in checks {
            try RequestDiagnostics.checkCancellation()
            if validated.insert(item.key[0]).inserted { try NativeFactCollector.validateRetainedTarget(item.target) }
            let element = item.target.element
            let actual: Fact
            switch item.key[1] {
            case "field":
                let property = item.key[2]
                if let key = NativeFactCollector.writableFields[property] {
                    var writable = DarwinBoolean(false)
                    let status = AXUIElementIsAttributeSettable(element, key as CFString, &writable)
                    actual = try Self.scalar(value: status == .success ? writable.boolValue : nil,
                                             status: status == .success ? nil : AXFactEncoding.status(status))
                } else {
                    var raw: CFTypeRef?
                    let status = AXUIElementCopyAttributeValue(element, property as CFString, &raw)
                    let decoded = status == .success ? AXFactEncoding.scalar(raw) : (value: nil, failure: AXFactEncoding.status(status))
                    actual = try Self.scalar(value: decoded.value, status: decoded.failure)
                }
            case "actions":
                var raw: CFArray?
                let status = AXUIElementCopyActionNames(element, &raw)
                actual = try Self.actions(AXFactEncoding.actions(status, raw))
            default:
                let meta = try JSONSerialization.jsonObject(with: item.fact.data) as! [String: Any]
                let offset = meta["offset"] as! Int
                let reader = AXRelationReader()
                let result: AXRelationReader.Result
                do { result = try reader.read(element, source: item.key[0], property: item.key[2], limit: offset + item.fact.objects.count) }
                catch let error as DriverError where error.code == .snapshotExpired { throw Changed() }
                switch result {
                case .failure(let value): actual = try Self.relation(value, objects: [])
                case .page(let page):
                    let objects = Array(page.prefix.dropFirst(offset))
                    actual = try Self.relation(["state": offset == 0 && objects.count == page.total ? "complete" : "partial",
                        "certainty": "confirmed", "offset": offset, "total": page.total], objects: objects)
                }
            }
            try Self.compare(item.fact, actual)
        }
    }
}
