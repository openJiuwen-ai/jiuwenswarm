import AppKit
import ApplicationServices

/// One session's native objects, raw facts, window catalogue and addressed units.
/// This facade does not find editors, project observations or choose input routes.
@MainActor
final class NativeApplicationAccess {
    typealias ReadRoot = NativeApplicationReader.ReadRoot
    typealias Window = NativeApplicationReader.Window
    private let registry: ApplicationElements
    private let reader: NativeApplicationReader
    private let control: NativeElementControl
    private let facts: NativeFactCollector
    init() {
        let registry = ApplicationElements()
        let inspection = NativeElementInspection(registry: registry)
        self.registry = registry
        self.reader = NativeApplicationReader(registry: registry, inspection: inspection)
        self.control = NativeElementControl(registry: registry, inspection: inspection)
        self.facts = NativeFactCollector(registry: registry, inspection: inspection)
    }

    func retainNode(_ ref: String) -> (() -> Void)? { registry.retain(ref) }

    /// Decoration reads retained identities only. Geometry is resolved on the
    /// pointer's separate queue, never by adding AX work to the input path.
    func visualElement(_ ref: String) -> AXUIElement? { registry.target(ref)?.element }

    func observationScope(_ app: NSRunningApplication, window: AXUIElement?, reference: String? = nil) -> String {
        return registry.observationScope(app, window: window, reference: reference)
    }

    func windowReference(_ window: NativeWindows.Window, element: AXUIElement? = nil) -> String {
        return registry.windowReference(window, element: element)
    }

    func nativeScope(_ window: NativeWindows.Window, owner: NSRunningApplication) -> String {
        return registry.nativeScope(window, owner: owner)
    }

    func readRoot(_ ref: String) throws -> ReadRoot {
        return try reader.readRoot(ref)
    }

    func nativeFacts(_ ref: String, session: String, fields: [String], maxNodes: Int, maxDepth: Int,
                     childrenPage: NativeInspectQuery.ChildrenPage? = nil, relationPage: NativeInspectQuery.RelationPage? = nil, advertisedFields: [String] = [],
                     followRelations: [String] = []) throws -> [String: Any] {
        try facts.collect(ref, session: session, fields: fields, maxNodes: maxNodes, maxDepth: maxDepth,
                          childrenPage: childrenPage, relationPage: relationPage, advertisedFields: advertisedFields, followRelations: followRelations)
    }

    func nativeReadTarget(_ ref: String) throws -> ApplicationElements.Target { try facts.readTarget(ref) }

    func nativeRelation(_ ref: String, session: String, property: String, cursor: String?, limit: Int) throws -> [String: Any] {
        try facts.relation(ref, session: session, property: property, cursor: cursor, limit: limit)
    }

    func registerNativeRoot(_ element: AXUIElement, app: NSRunningApplication, scope: String) throws -> String {
        try facts.registerRoot(element, app: app, scope: scope)
    }

    func readText(_ ref: String, offset: Int, length: Int) throws -> [String: Any] {
        return try reader.readText(ref, offset: offset, length: length)
    }

    func windows(_ app: NSRunningApplication, nativeWindows: [NativeWindows.Window] = [], reportIssue: ((String) -> Void)? = nil) throws -> [Window] {
        return try reader.windows(app, nativeWindows: nativeWindows, reportIssue: reportIssue)
    }

    func nativeActionFacts(_ ref: String) throws -> NativeElementControl.ActionFacts {
        try control.nativeActionFacts(ref)
    }

    func executeNativeAction(_ ref: String, action: String, description: String?, onDispatch: @MainActor () throws -> Void) throws {
        try control.executeNativeAction(ref, action: action, description: description, onDispatch: onDispatch)
    }

    func nativeTextValueFacts(_ ref: String) throws -> NativeElementControl.TextValueFacts {
        try control.nativeTextValueFacts(ref)
    }

    func executeNativeTextValue(_ ref: String, contract: NativeElementControl.TextValueContract, text: String,
                                onDispatch: @MainActor () throws -> Void) throws -> NativeElementControl.ActionResult {
        try control.executeNativeTextValue(ref, contract: contract, text: text, onDispatch: onDispatch)
    }

}
