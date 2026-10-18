import Foundation

/// Decode once, before native target access. This is wire shape validation;
/// platform field support and live scope checks stay in the selected collector.
enum NativeInspectQuery {
    struct ChildrenPage {
        let cursor: String?
        let limit: Int

        init(_ raw: Any?) throws {
            let p = try object(raw, keys: ["cursor", "limit"])
            cursor = p["cursor"] is NSNull ? nil : try string(p["cursor"], label: "childrenPage.cursor")
            limit = try NativeInspectQuery.integer(p["limit"], label: "childrenPage.limit", range: 1...1999)
        }
    }
    struct RelationPage {
        let property: String
        let page: ChildrenPage

        init(_ raw: Any?) throws {
            let p = try object(raw, keys: ["property", "cursor", "limit"])
            property = try string(p["property"], label: "relationPage.property")
            page = try ChildrenPage(["cursor": p["cursor"]!, "limit": p["limit"]!])
        }
    }
    struct Actuator: Equatable {
        let node: String
        let relation: String
        let region: String?
        let bar: String?
        var references: [String] { [node] + (region.map { [$0] } ?? []) + (bar.map { [$0] } ?? []) }
        var wire: [String: Any] {
            var value: [String: Any] = ["node": node, "relation": relation]
            if let region { value["region"] = region }
            if let bar { value["bar"] = bar }
            return value
        }

        init(_ raw: Any?) throws {
            relation = try string((raw as? [String: Any])?["relation"], label: "actuator.relation")
            guard ["containedEditor", "scrollPage", "scrollPointer", "controlPart"].contains(relation) else {
                throw DriverError("unsupported actuator relation", code: .invalidRequest)
            }
            let keys: Set<String> = relation == "scrollPage" ? ["node", "relation", "region", "bar"]
                : relation == "scrollPointer" ? ["node", "relation", "region"] : ["node", "relation"]
            let p = try object(raw, keys: keys)
            node = try string(p["node"], label: "actuator.node")
            region = ["scrollPage", "scrollPointer"].contains(relation) ? try string(p["region"], label: "actuator.region") : nil
            bar = relation == "scrollPage" ? try string(p["bar"], label: "actuator.bar") : nil
            if relation == "scrollPage", node == region || node == bar {
                throw DriverError("a page button must be distinct from its scroll area and bar", code: .invalidRequest)
            }
        }
    }

    enum Receiver: Equatable {
        case virtualFocus(popup: String, via: String)
        case containedFocus(node: String)
        var reference: String {
            switch self { case .virtualFocus(let popup, _): return popup; case .containedFocus(let node): return node }
        }
        var wire: [String: Any] {
            switch self {
            case .virtualFocus(let popup, let via): return ["relation": "virtualFocus", "popup": popup, "via": via]
            case .containedFocus(let node): return ["relation": "containedFocus", "node": node]
            }
        }
        init(_ raw: Any?) throws {
            switch (raw as? [String: Any])?["relation"] as? String {
            case "virtualFocus":
                let p = try object(raw, keys: ["relation", "popup", "via"])
                guard let via = p["via"] as? String, ["owns", "linked"].contains(via) else {
                    throw DriverError("unsupported virtual receiver relationship", code: .invalidRequest)
                }
                self = .virtualFocus(popup: try string(p["popup"], label: "receiver.popup"), via: via)
            case "containedFocus":
                let p = try object(raw, keys: ["relation", "node"])
                self = .containedFocus(node: try string(p["node"], label: "receiver.node"))
            default:
                throw DriverError("unsupported receiver relationship", code: .invalidRequest)
            }
        }
    }

    struct Dependency: Equatable {
        let observed: String
        let node: String
        let fields: [String]
        let actions: Bool
        let relations: [String]
        var wire: [String: Any] { ["observed": observed, "node": node, "fields": fields,
                                  "actions": actions, "relations": relations] }

        init(_ raw: Any?) throws {
            let p = try object(raw, keys: ["observed", "node", "fields", "actions", "relations"])
            observed = try string(p["observed"], label: "basis.observed")
            node = try string(p["node"], label: "basis.node")
            fields = try NativeInspectQuery.names(p["fields"], label: "basis.fields", min: 0, max: 32)
            actions = try boolean(p["actions"], label: "basis.actions")
            relations = try NativeInspectQuery.names(p["relations"], label: "basis.relations", min: 0, max: 16)
            guard !fields.isEmpty || actions || !relations.isEmpty else {
                throw DriverError("empty selection dependency", code: .invalidRequest)
            }
        }
    }

    case applications
    case launch
    case surfaces(application: String)
    case roots(application: String)
    case inputContext(within: [String], actuator: Actuator?, receiver: Receiver?, basis: [Dependency])
    case nodes(fields: [String], maxNodes: Int, maxDepth: Int, childrenPage: ChildrenPage?, relationPage: RelationPage?, advertisedFields: [String], followRelations: [String])
    case relation(property: String, cursor: String?, limit: Int)
    case capturedText(ref: String, offset: Int, length: Int)

    var observesNodes: Bool {
        switch self {
        case .nodes, .relation: return true
        default: return false
        }
    }

    init(_ raw: Any?) throws {
        let kind = try string((raw as? [String: Any])?["kind"], label: "query.kind")
        switch kind {
        case "launch":
            _ = try object(raw, keys: ["kind"])
            self = .launch
        case "applications":
            _ = try object(raw, keys: ["kind"])
            self = .applications
        case "surfaces":
            let p = try object(raw, keys: ["kind", "application"])
            self = .surfaces(application: try string(p["application"], label: "application"))
        case "roots":
            let p = try object(raw, keys: ["kind", "application"])
            self = .roots(application: try string(p["application"], label: "application"))
        case "inputContext":
            let hasActuator = (raw as? [String: Any])?.keys.contains("actuator") == true
            let hasReceiver = (raw as? [String: Any])?.keys.contains("receiver") == true
            let hasBasis = (raw as? [String: Any])?.keys.contains("basis") == true
            let p = try object(raw, keys: Set(["kind", "within"] + (hasActuator ? ["actuator"] : [])
                + (hasReceiver ? ["receiver"] : []) + (hasBasis ? ["basis"] : [])))
            var basis = [Dependency]()
            if hasBasis {
                guard hasActuator || hasReceiver, let entries = p["basis"] as? [Any], (1...512).contains(entries.count) else {
                    throw DriverError("selection basis requires an actuator or receiver and 1...512 entries", code: .invalidRequest)
                }
                basis = try entries.map(Dependency.init)
                guard Set(basis.map { [$0.observed, $0.node] }).count == basis.count else {
                    throw DriverError("duplicate selection dependency", code: .invalidRequest)
                }
            }
            guard !hasReceiver || hasBasis else {
                throw DriverError("a receiver relation requires selection dependencies", code: .invalidRequest)
            }
            let actuator = hasActuator ? try Actuator(p["actuator"]) : nil
            guard actuator?.relation != "scrollPointer" || !basis.isEmpty else {
                throw DriverError("element scrolling requires semantic selection dependencies", code: .invalidRequest)
            }
            guard !hasReceiver || actuator == nil || actuator?.relation == "containedEditor" else {
                throw DriverError("receiver relation requires an editor actuator", code: .invalidRequest)
            }
            self = .inputContext(within: try Self.names(p["within"], label: "within", min: 0, max: 16),
                                 actuator: actuator,
                                 receiver: hasReceiver ? try Receiver(p["receiver"]) : nil, basis: basis)
        case "nodes":
            let hasPage = (raw as? [String: Any])?.keys.contains("childrenPage") == true
            let hasRelationPage = (raw as? [String: Any])?.keys.contains("relationPage") == true
            let hasAdvertised = (raw as? [String: Any])?.keys.contains("advertisedFields") == true
            let hasFollow = (raw as? [String: Any])?.keys.contains("followRelations") == true
            let p = try object(raw, keys: Set(["kind", "fields", "maxNodes", "maxDepth"]
                + (hasPage ? ["childrenPage"] : []) + (hasAdvertised ? ["advertisedFields"] : [])
                + (hasRelationPage ? ["relationPage"] : [])
                + (hasFollow ? ["followRelations"] : [])))
            let fields = try Self.names(p["fields"], label: "fields", min: 1, max: 128)
            let advertised = hasAdvertised ? try Self.names(p["advertisedFields"], label: "advertisedFields", min: 1, max: 128) : []
            guard Set(advertised).isSubset(of: Set(fields)) else {
                throw DriverError("advertised fields must be a subset of the requested fields", code: .invalidRequest)
            }
            let maxNodes = try Self.integer(p["maxNodes"], label: "maxNodes", range: 1...2000)
            let maxDepth = try Self.integer(p["maxDepth"], label: "maxDepth", range: 0...2000)
            let page = hasPage ? try ChildrenPage(p["childrenPage"]) : nil
            let relationPage = hasRelationPage ? try RelationPage(p["relationPage"]) : nil
            guard !(hasPage && hasRelationPage) else {
                throw DriverError("select one root relation page", code: .invalidRequest)
            }
            if let selected = relationPage?.page ?? page, maxDepth < 1 || selected.limit >= maxNodes {
                throw DriverError("a relation page needs depth and space for its root and targets", code: .invalidRequest)
            }
            self = .nodes(fields: fields, maxNodes: maxNodes, maxDepth: maxDepth,
                childrenPage: page, relationPage: relationPage, advertisedFields: advertised,
                followRelations: hasFollow ? try Self.names(p["followRelations"], label: "followRelations", min: 1, max: 16) : [])
        case "relation":
            let p = try object(raw, keys: ["kind", "property", "cursor", "limit"])
            self = .relation(property: try string(p["property"], label: "property"),
                cursor: p["cursor"] is NSNull ? nil : try string(p["cursor"], label: "cursor"),
                limit: try Self.integer(p["limit"], label: "limit", range: 1...2000))
        case "capturedText":
            let p = try object(raw, keys: ["kind", "textRef", "offset", "length"])
            self = .capturedText(ref: try string(p["textRef"], label: "textRef"),
                offset: try Self.integer(p["offset"], label: "offset", range: 0...9_007_199_254_740_991),
                length: try Self.integer(p["length"], label: "length", range: 2...16_384))
        default:
            throw DriverError("unknown native inspection query", code: .invalidRequest)
        }
    }

    private static func names(_ value: Any?, label: String, min: Int, max: Int) throws -> [String] {
        guard let names = value as? [String], (min...max).contains(names.count),
              Set(names).count == names.count else {
            throw DriverError("invalid native query \(label)", code: .invalidRequest)
        }
        return try names.map { try string($0, label: label) }
    }

    private static func integer(_ value: Any?, label: String, range: ClosedRange<Int>) throws -> Int {
        let value = try number(value, label: label)
        guard value.rounded() == value, (Double(range.lowerBound)...Double(range.upperBound)).contains(value) else {
            throw DriverError("invalid native query \(label)", code: .invalidRequest)
        }
        return Int(value)
    }
}
