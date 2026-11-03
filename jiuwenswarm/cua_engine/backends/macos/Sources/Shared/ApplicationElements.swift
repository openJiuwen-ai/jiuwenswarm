import AppKit
import ApplicationServices

@MainActor
final class ApplicationElements {
    struct Target {
        let application: NSRunningApplication
        let launched: Date?
        var element: AXUIElement
        let scope: String
        var role: String
        var identity: ElementIdentity
        // A nested view service can expose a public AX PID different from the
        // visible window's application. Retain that process incarnation; a
        // live native containment path, not PID equality, proves host ownership.
        var provider: NativeProcessIdentity? = nil
        var providerKnown = false
        // Native path survives presentation compression. Revalidate its edges
        // before using an ancestor as a control's interaction owner.
        var ancestors: [AXUIElement] = []
        var context: [Context] = []
    }
    struct Identity: Hashable {
        let pid: pid_t
        let launched: Date?
        let element: AXUIElement?
        var nativeWindow: UInt32? = nil
        var incarnation: String? = nil
        var scope: String? = nil

        static func == (lhs: Identity, rhs: Identity) -> Bool {
            lhs.pid == rhs.pid && lhs.launched == rhs.launched && lhs.nativeWindow == rhs.nativeWindow
                && lhs.incarnation == rhs.incarnation && lhs.scope == rhs.scope && {
                    if let a = lhs.element, let b = rhs.element { return CFEqual(a, b) }
                    return lhs.element == nil && rhs.element == nil
                }()
        }
        func hash(into hasher: inout Hasher) {
            hasher.combine(pid); hasher.combine(launched); hasher.combine(nativeWindow); hasher.combine(incarnation)
            hasher.combine(scope)
            if let element { hasher.combine(CFHash(element)) }
        }
    }
    struct Context: Equatable {
        let native: Identity
        let identity: ElementIdentity
    }
    struct Evidence: Equatable {
        let identity: ElementIdentity
        let provider: NativeProcessIdentity?
        let context: [Context]
        var providerKnown = false
    }
    // Tokens capture immutable text but resolve actions through confirmed native lifetimes.
    private let namespace = String(UInt64.random(in: .min ... .max), radix: 36)
    private lazy var elements = ObservedElements<Identity, Evidence, Target>(namespace: namespace, reconcile: Self.reconcile)
    private let references = ElementReferences<Identity>()
    // A physical window can appear through more than one logical application.
    // Keep its public window number, but never share action/observation state
    // between those owners. NSRunningApplication equality includes its lifetime.
    private struct ScopeIdentity: Hashable {
        let owner: NSRunningApplication
        let window: String?
    }
    private let scopeReferences = ElementReferences<ScopeIdentity>()

    /// Reconcile evidence for the same native key, never search for a substitute
    /// object. Missing attributes retain historical constraints, not a claim of
    /// current input readiness; each native unit still rechecks live evidence.
    nonisolated private static func reconcile(_ previous: Evidence, _: Target,
                                  _ next: Evidence, _ newTarget: Target) -> (Evidence, Target)? {
        guard let identity = previous.identity.refining(with: next.identity),
              previous.context.count == next.context.count else { return nil }
        var contexts = [Context]()
        for (old, new) in zip(previous.context, next.context) {
            guard old.native == new.native, let identity = old.identity.refining(with: new.identity) else { return nil }
            contexts.append(Context(native: new.native, identity: identity))
        }
        let oldKnown = previous.providerKnown || previous.provider != nil
        let newKnown = next.providerKnown || next.provider != nil
        guard !oldKnown || !newKnown || previous.provider == next.provider else { return nil }
        let provider = newKnown ? next.provider : previous.provider
        var target = newTarget
        target.identity = identity; target.role = identity.attributes["AXRole"] ?? newTarget.role
        target.provider = provider; target.providerKnown = oldKnown || newKnown
        target.context = contexts
        return (Evidence(identity: identity, provider: provider, context: contexts,
                         providerKnown: target.providerKnown), target)
    }

    func invalidate(pid: pid_t, scope: String? = nil) {
        elements.invalidate { $0.application.processIdentifier == pid && (scope == nil || $0.scope == scope) }
    }

    func observationScope(_ app: NSRunningApplication, window: AXUIElement?, reference: String? = nil) -> String {
        let windowRef = reference ?? window.map {
            references.reference(for: Identity(pid: app.processIdentifier, launched: app.launchDate, element: $0))
        }
        return "\(namespace):scope" + scopeReferences.reference(for: ScopeIdentity(owner: app, window: windowRef))
    }

    func windowReference(_ window: NativeWindows.Window, element: AXUIElement? = nil) -> String {
        let aliases = element.map { [Identity(pid: window.process.pid, launched: window.application.launchDate,
            element: $0, incarnation: window.process.incarnation)] } ?? []
        return references.reference(for: Identity(pid: window.process.pid, launched: nil,
            element: nil, nativeWindow: window.mapping.id, incarnation: window.process.incarnation), aliases: aliases)
    }

    func nativeScope(_ window: NativeWindows.Window, owner: NSRunningApplication) -> String {
        observationScope(owner, window: nil, reference: windowReference(window))
    }


    // The registry owns lifetimes and captured text. Collaborators
    // receive value copies or explicit operations, never mutable dictionaries.
    func target(_ token: String) -> Target? { elements.target(token) }
    func text(_ token: String) -> ObservedElements<Identity, Evidence, Target>.TextSnapshot? { elements.text(token) }
    func observe(_ key: Identity, evidence: Evidence, value: Target) -> String {
        var evidence = evidence
        evidence.providerKnown = evidence.providerKnown || value.providerKnown || value.provider != nil
        return elements.observe(key, evidence: evidence, value: value)
    }
    func observed(_ key: Identity) -> (id: String, value: Target)? { elements.observed(key) }
    func retain(_ ref: String) -> (() -> Void)? { elements.retain(ref) }
    static func childContext(_ target: Target) -> [Context] {
        let menu = target.role == kAXMenuBarRole || target.context.contains { $0.identity.attributes[kAXRoleAttribute] == kAXMenuBarRole }
        let boundary = [kAXWindowRole, kAXSheetRole, kAXRowRole].contains(target.role)
            || (menu && [kAXMenuBarRole, kAXMenuBarItemRole, kAXMenuRole, kAXMenuItemRole].contains(target.role))
            || target.identity.attributes.keys.contains { [kAXIdentifierAttribute, kAXURLAttribute, kAXDocumentAttribute].contains($0) }
            || target.identity.attributes.keys.contains(where: ElementIdentity.positionKeys.contains)
        return target.context + (boundary ? [Context(native: Identity(pid: target.application.processIdentifier,
            launched: target.launched, element: target.element), identity: target.identity)] : [])
    }
    func bind(_ lifetime: String, text: String?) -> String? { elements.bind(lifetime, text: text) }
    func invalidate(where predicate: (Target) -> Bool) { elements.invalidate(where: predicate) }
    func reference(for identity: Identity, aliases: [Identity] = []) -> String {
        references.reference(for: identity, aliases: aliases)
    }
}
