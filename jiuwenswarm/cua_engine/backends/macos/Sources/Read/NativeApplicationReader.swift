import AppKit
import ApplicationServices

/// Native window catalogue, validated handles and immutable text pages.
/// Semantic composition, tree expansion policy and rendering belong to Python.
@MainActor
final class NativeApplicationReader {
    private typealias Identity = ApplicationElements.Identity
    private let registry: ApplicationElements
    private let inspection: NativeElementInspection
    init(registry: ApplicationElements, inspection: NativeElementInspection) {
        self.registry = registry; self.inspection = inspection
    }

    struct ReadRoot {
        let element: AXUIElement
        let application: NSRunningApplication
        let scope: String
        let ancestors: [AXUIElement]
        let validate: () throws -> Void
    }

    func readRoot(_ ref: String) throws -> ReadRoot {
        guard let target = registry.target(ref) else { throw DriverError("element reference expired; read its window again", code: .staleTarget) }
        // Inspection is allowed for disabled controls and blocked backgrounds.
        // Keep identity checks separate from input preparation and permission to act.
        let validate = {
            try BackgroundTarget(target.application).validate()
            try self.inspection.validateTargetIdentity(target)
        }
        try validate()
        return ReadRoot(element: target.element, application: target.application, scope: target.scope,
            ancestors: target.ancestors, validate: validate)
    }

    func readText(_ ref: String, offset: Int, length: Int) throws -> [String: Any] {
        guard let snapshot = registry.text(ref) else { throw DriverError("snapshot expired; read the element again to capture a new Value", code: .snapshotExpired) }
        guard let value = snapshot.value else {
            throw DriverError("the observed element has no readable string Value", next: "read the app or use its screenshot; an unavailable value is not empty text")
        }
        let text = value as NSString
        guard offset <= text.length else { throw DriverError("text offset exceeds observed Value length \(text.length)", code: .invalidRequest) }
        func insidePair(_ index: Int) -> Bool {
            index > 0 && index < text.length && (0xD800...0xDBFF).contains(text.character(at: index - 1))
                && (0xDC00...0xDFFF).contains(text.character(at: index))
        }
        guard !insidePair(offset) else { throw DriverError("text offset splits a Unicode surrogate pair; use the previous page's next offset", code: .invalidRequest) }
        var end = offset + min(length, text.length - offset)
        if insidePair(end) { end -= 1 }
        return ["text": text.substring(with: NSRange(location: offset, length: end - offset)),
                "offset": offset, "total": text.length, "next": end < text.length ? end as Any : NSNull()]
    }

    struct Window {
        let ref: String
        let title: String
        let element: AXUIElement
        var kind: String = "window"
        var parent: String? = nil
        var modal: Bool? = nil
        var document: String? = nil
    }

    func windows(_ app: NSRunningApplication, nativeWindows: [NativeWindows.Window] = [], reportIssue: ((String) -> Void)? = nil) throws -> [Window] {
        try RequestDiagnostics.checkCancellation()
        guard AXIsProcessTrusted() else { return [] }
        let incarnation = nativeWindows.first { $0.process.pid == app.processIdentifier }?.process.incarnation
            ?? NativeProcessIdentity.read(app.processIdentifier)?.incarnation
        let root = AXUIElementCreateApplication(app.processIdentifier)
        AXMessaging.configure(root)
        let rootRead = AXAccess.read(root, kAXWindowsAttribute)
        let declaredRoots = rootRead.value as? [AXUIElement] ?? []
        if rootRead.value as? [AXUIElement] == nil { reportIssue?("AXWindows catalogue unavailable or malformed") }
        var candidates = declaredRoots
        for key in [kAXFocusedWindowAttribute, kAXMainWindowAttribute] {
            if let raw = inspection.attribute(root, key), CFGetTypeID(raw) == AXUIElementGetTypeID() {
                candidates.append(unsafeDowncast(raw, to: AXUIElement.self))
            }
        }
        if let focused = WindowGeometry.inputWindow(pid: app.processIdentifier) { candidates.append(focused) }
        var result = [Window]()
        var owners = [(child: AXUIElement, parent: AXUIElement)]()
        let surfaceRoles = [kAXWindowRole, kAXSheetRole, "AXPopover"]
        var focus = AXAccess.element(root, kAXFocusedUIElementAttribute), focusPath = [AXUIElement]()
        var focusedSurface: AXUIElement?
        let focusDeadline = ProcessInfo.processInfo.systemUptime + 0.1
        while let node = focus, focusPath.count < 32, ProcessInfo.processInfo.systemUptime < focusDeadline,
              !focusPath.contains(where: { CFEqual($0, node) }) {
            try RequestDiagnostics.checkCancellation()
            focusPath.append(node)
            if surfaceRoles.contains(inspection.attribute(node, kAXRoleAttribute) as? String ?? "") {
                candidates.append(node)
                if let child = focusedSurface { owners.append((child, node)) }
                focusedSurface = node
            }
            focus = AXAccess.element(node, kAXParentAttribute)
        }
        if focus != nil { reportIssue?("focused surface ancestry incomplete") }
        // AppKit attaches popovers to their anchor control, not necessarily to
        // AXWindows or a window's immediate children. Discover surface roots
        // through actual child edges; never infer ownership from screen overlap.
        let surfaceDeadline = ProcessInfo.processInfo.systemUptime + 0.4
        var scanned = [AXUIElement]()
        func nestedSurfaces(_ parent: AXUIElement) throws -> [AXUIElement] {
            var pending = [parent], found = [AXUIElement](), index = 0
            while index < pending.count, scanned.count < 512,
                  ProcessInfo.processInfo.systemUptime < surfaceDeadline {
                let node = pending[index]; index += 1
                try RequestDiagnostics.checkCancellation()
                if scanned.contains(where: { CFEqual($0, node) }) { continue }
                scanned.append(node)
                for child in AXAccess.elements(node, kAXChildrenAttribute, limit: 128) ?? [] {
                    if ProcessInfo.processInfo.systemUptime >= surfaceDeadline { break }
                    if surfaceRoles.contains(inspection.attribute(child, kAXRoleAttribute) as? String ?? "") {
                        found.append(child)
                    } else { pending.append(child) }
                }
            }
            return found
        }
        var cursor = 0
        while cursor < candidates.count {
            try RequestDiagnostics.checkCancellation()
            let element = WindowGeometry.presentedWindow(candidates[cursor], pid: app.processIdentifier); cursor += 1
            var pid: pid_t = 0
            guard AXUIElementGetPid(element, &pid) == .success, pid == app.processIdentifier,
                  let role = inspection.attribute(element, kAXRoleAttribute) as? String,
                  ([kAXWindowRole, kAXSheetRole, "AXPopover"].contains(role) || declaredRoots.contains { CFEqual($0, element) }),
                  !result.contains(where: { CFEqual($0.element, element) }) else { continue }
            let subrole = inspection.attribute(element, kAXSubroleAttribute) as? String ?? ""
            let kind = role == "AXPopover" ? "popover" : role == kAXSheetRole ? "sheet" : ["AXDialog", "AXSystemDialog"].contains(subrole) ? "dialog"
                : ["AXFloatingWindow", "AXSystemFloatingWindow"].contains(subrole) ? "panel" : "window"
            result.append(Window(ref: registry.reference(for: Identity(pid: pid, launched: app.launchDate, element: element, incarnation: incarnation)),
                title: inspection.attribute(element, kAXTitleAttribute) as? String ?? "", element: element,
                kind: kind, modal: inspection.attribute(element, kAXModalAttribute) as? Bool))
            // Sheets are often absent from AXWindows. Explicit native ownership
            // is retained; title, screen position and current focus prove no owner.
            for child in inspection.attribute(element, "AXSheets") as? [AXUIElement] ?? [] {
                owners.append((child, element))
                candidates.append(child)
            }
            for child in try nestedSurfaces(element) {
                owners.append((child, element)); candidates.append(child)
            }
        }
        for index in result.indices {
            try RequestDiagnostics.checkCancellation()
            let item = result[index]
            var parent = owners.first { CFEqual($0.child, item.element) }?.parent
            if parent == nil, let raw = inspection.attribute(item.element, kAXParentAttribute), CFGetTypeID(raw) == AXUIElementGetTypeID() {
                parent = unsafeDowncast(raw, to: AXUIElement.self)
            }
            if let parent, let owner = result.first(where: { !CFEqual($0.element, item.element) && CFEqual($0.element, parent) }) {
                result[index].parent = owner.ref
                // AXSheet is the native document-modal role; an explicit false
                // or unavailable owner is never changed into application-wide modality.
                if item.kind == "sheet" && item.modal == nil { result[index].modal = true }
            }
        }
        // Merge only one-to-one native matches. Same-title/overlapping windows
        // must not collapse, nor may a sheet borrow its parent's identity.
        var remapped = [String: String]()
        // Read each AX hint once, not once per pair of AX/native windows.
        let matchesByWindow = try result.map { item -> [NativeWindows.Window] in
            try RequestDiagnostics.checkCancellation()
            guard let hint = WindowGeometry.hint(item.element) else { return [] }
            return nativeWindows.filter {
                $0.process.pid == app.processIdentifier && WindowSelection.identifies(
                    .init(id: $0.mapping.id, frame: $0.mapping.frame, title: $0.mapping.title ?? "", visible: $0.onScreen),
                    hint: hint)
            }
        }
        for (index, item) in result.enumerated() {
            let matches = matchesByWindow[index]
            if matches.count == 1, let native = matches.first,
               matchesByWindow.filter({ $0.contains { $0.mapping.id == native.mapping.id } }).count == 1 {
                remapped[item.ref] = registry.windowReference(native, element: item.element)
            }
        }
        try RequestDiagnostics.checkCancellation()
        var mapped = result.map { item in
            Window(ref: remapped[item.ref] ?? item.ref, title: item.title, element: item.element,
                kind: item.kind, parent: item.parent.map { remapped[$0] ?? $0 }, modal: item.modal)
        }
        // Identical titles are insufficient for choosing a newly opened file.
        // Preserve each exact window's own native URI, without inspecting its
        // contents, guessing from a parent or querying unrelated unique titles.
        let titleCounts = Dictionary(grouping: mapped, by: \.title).mapValues(\.count)
        let documentDeadline = ProcessInfo.processInfo.systemUptime + 0.2
        for index in mapped.indices where (titleCounts[mapped[index].title] ?? 0) > 1 {
            try RequestDiagnostics.checkCancellation()
            guard ProcessInfo.processInfo.systemUptime < documentDeadline else { break }
            if let raw = inspection.attribute(mapped[index].element, kAXDocumentAttribute),
               let value = (raw as? String) ?? (raw as? URL)?.absoluteString, !value.isEmpty {
                mapped[index].document = value
            }
        }
        return mapped
    }
}
