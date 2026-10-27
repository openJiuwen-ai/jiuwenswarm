import Foundation
import ApplicationServices

/// The AX query result and its relationship to the retained target are separate
/// facts. A messaging failure is neither an empty point nor window occlusion.
struct NativeElementHitEvidence {
    enum Relation: String {
        case target, coarseAncestorWindow, otherWindow, otherControl, unknownOwner
        case noElement, unsupported, queryFailed
    }
    let status: AXError
    let relation: Relation

    static func result(_ status: AXError, relation: Relation?) -> Self {
        guard status == .success else {
            let value: Relation = status == .noValue ? .noElement
                : (status == .notImplemented || status == .attributeUnsupported ? .unsupported : .queryFailed)
            return .init(status: status, relation: value)
        }
        return .init(status: status, relation: relation ?? .noElement)
    }

    var permitsBackgroundGeometry: Bool {
        switch relation {
        case .otherWindow, .noElement, .unsupported, .queryFailed: return true
        case .target, .coarseAncestorWindow, .otherControl, .unknownOwner: return false
        }
    }
}

/// Bounded read-only candidates. The provider's activation point is a hint,
/// never evidence of a safe click by itself; only an exact AX hit admits it.
enum NativeElementHitSelection {
    struct Candidate: Equatable {
        enum Source: String { case activationPoint, center, edge }
        let point: CGPoint
        let source: Source
        var permitsGeometryFallback: Bool { source == .center }
        // The existing center route keeps the process's AX timeout, including
        // later frozen-point checks. Only additional candidate queries are short.
        var queryTimeout: Float? { source == .center ? nil : 0.05 }
    }
    enum Basis: String { case exactHit, coarseAncestorGeometry, backgroundGeometry, rejected }
    struct Observation<Hit> {
        let evidence: NativeElementHitEvidence
        let hit: Hit?
        let foreground: Bool
    }
    struct Selection<Hit> {
        let candidate: Candidate
        let observation: Observation<Hit>
        let basis: Basis
    }

    /// A fresh, strict parent path differs from an AXWindow link or cached
    /// context. Missing links, another surface, cycles and budget exhaustion
    /// never prove that the hit is the retained target's ancestor window.
    static func reachesWindow<Object>(_ target: Object, window: Object,
        same: (Object, Object) -> Bool, parent: (Object) throws -> Object?,
        isSurface: (Object) throws -> Bool, canContinue: () throws -> Bool) rethrows -> Bool {
        var current = target, seen = [target]
        for _ in 0..<32 {
            guard try canContinue(), let next = try parent(current),
                  !seen.contains(where: { same($0, next) }) else { return false }
            if same(next, window) { return true }
            if try isSurface(next) { return false }
            seen.append(next); current = next
        }
        return false
    }

    static func candidates(bounds: CGRect, activationPoint: CGPoint?, expand: Bool = true) -> [Candidate] {
        guard [bounds.minX, bounds.minY, bounds.width, bounds.height].allSatisfy(\.isFinite),
              !bounds.isNull, bounds.width > 0, bounds.height > 0 else { return [] }
        var result = [Candidate]()
        func append(_ point: CGPoint, _ source: Candidate.Source) {
            guard point.x.isFinite, point.y.isFinite, bounds.contains(point),
                  !result.contains(where: { $0.point == point }) else { return }
            result.append(Candidate(point: point, source: source))
        }
        // Keep the center even when the provider returns that same point: its
        // geometry fallback is an existing, independent window-directed route.
        let center = CGPoint(x: bounds.midX, y: bounds.midY)
        // Wheel input keeps the previously observed region center. Its public
        // resolver qualifies that exact hit; it does not request a control click.
        guard expand else { return [Candidate(point: center, source: .center)] }
        if let activationPoint, activationPoint != center { append(activationPoint, .activationPoint) }
        append(center, .center)
        let dx = min(4, bounds.width / 4), dy = min(4, bounds.height / 4)
        let left = bounds.minX + dx, right = bounds.maxX - dx
        let top = bounds.minY + dy, bottom = bounds.maxY - dy
        for point in [CGPoint(x: left, y: center.y), CGPoint(x: right, y: center.y),
                      CGPoint(x: center.x, y: top), CGPoint(x: center.x, y: bottom),
                      CGPoint(x: left, y: top), CGPoint(x: right, y: top),
                      CGPoint(x: left, y: bottom), CGPoint(x: right, y: bottom)] { append(point, .edge) }
        return result
    }

    static func select<Hit>(_ candidates: [Candidate], exclusive: Bool,
                           canContinue: (Candidate) throws -> Bool,
                           query: (Candidate) throws -> Observation<Hit>) rethrows -> Selection<Hit>? {
        var fallback: Selection<Hit>?, coarse: Selection<Hit>?, representative: Selection<Hit>?
        for candidate in candidates {
            guard try canContinue(candidate) else { continue }
            let observation = try query(candidate)
            let basis: Basis = observation.evidence.relation == .target ? .exactHit
                : (observation.evidence.relation == .coarseAncestorWindow && observation.foreground
                    && candidate.permitsGeometryFallback ? .coarseAncestorGeometry
                : (!exclusive && !observation.foreground && candidate.permitsGeometryFallback
                    && observation.evidence.permitsBackgroundGeometry ? .backgroundGeometry : .rejected))
            let result = Selection(candidate: candidate, observation: observation, basis: basis)
            if basis == .exactHit { return result }
            if basis == .coarseAncestorGeometry { coarse = result }
            if basis == .backgroundGeometry { fallback = result }
            if representative == nil || candidate.source == .center { representative = result }
            // Query failures apply to the application, not just this point. Do
            // not multiply slow failed AX calls across eight extra candidates.
            if candidate.source == .center,
               [.unsupported, .queryFailed].contains(observation.evidence.relation) { break }
        }
        return coarse ?? fallback ?? representative
    }
}
