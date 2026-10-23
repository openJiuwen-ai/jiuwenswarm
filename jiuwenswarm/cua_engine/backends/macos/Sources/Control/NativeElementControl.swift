import AppKit
import ApplicationServices

/// Addressed native units and their final target checks. Python selects routes.
@MainActor
final class NativeElementControl {
    typealias Target = ApplicationElements.Target
    let registry: ApplicationElements
    let inspection: NativeElementInspection
    init(registry: ApplicationElements, inspection: NativeElementInspection) {
        self.registry = registry; self.inspection = inspection
    }
    struct ActionResult {
        let verified: Bool
        let detail: String
    }

    func inputTarget(_ ref: String) throws -> Target {
        guard let target = registry.target(ref) else { throw DriverError("element reference expired; read the app again", code: .staleTarget) }
        guard target.application.launchDate == target.launched else { throw DriverError("target process changed; read the app again", code: .staleTarget) }
        try BackgroundTarget(target.application).validate(input: true)
        guard AXIsProcessTrusted() else { throw DriverError("accessibility permission denied", code: .desktopUnavailable) }
        try inspection.validateTargetIdentity(target)
        // Read the current native role/subrole, not an old capability flag.
        // Reject before window preparation, AX focus, mouse or key dispatch.
        try validateSharedElementInput(target)
        return target
    }

    func validateSharedElementInput(_ target: Target) throws {
        guard !BackgroundTarget.isExclusive else { return }
        try SharedDesktopPolicy.validateElementInput(role: target.role,
            subrole: target.identity.attributes[kAXSubroleAttribute],
            currentRole: inspection.attribute(target.element, kAXRoleAttribute) as? String,
            currentSubrole: inspection.attribute(target.element, kAXSubroleAttribute) as? String, exclusive: false)
    }

    func declaredActions(_ element: AXUIElement) throws -> [NativeAction.Item]? {
        try RequestDiagnostics.checkCancellation()
        var raw: CFArray?
        guard AXUIElementCopyActionNames(element, &raw) == .success, let rawNames = raw as? [String],
              let names = NativeAction.boundedNames(rawNames) else { return nil }
        return try names.map { name in
            try RequestDiagnostics.checkCancellation()
            var description: CFString?
            AXUIElementCopyActionDescription(element, name as CFString, &description)
            return NativeAction.Item(name: name, description: description as String?)
        }
    }
}
