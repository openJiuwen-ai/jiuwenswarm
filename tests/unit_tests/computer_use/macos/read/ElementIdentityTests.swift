import Foundation

@main struct ElementIdentityTests {
    static func main() {
        let command = ElementIdentity(role: "AXButton", subrole: nil, identifier: "slot", url: nil, title: "Open Alice", description: nil)
        precondition(command.matches(["AXRole": "AXButton", "AXIdentifier": "slot", "AXTitle": "Open Alice"]))
        precondition(!command.matches(["AXRole": "AXButton", "AXIdentifier": "slot", "AXTitle": "Open Bob"]))
        precondition(!command.matches(["AXRole": "AXButton", "AXTitle": "Open Alice"]))
        let record = ElementIdentity(role: "AXTextField", subrole: nil, identifier: nil, url: "file:///one.txt", title: nil, description: nil)
        precondition(!record.matches(["AXRole": "AXTextField", "AXURL": "file:///two.txt"]))
        let editor = ElementIdentity(role: "AXTextField", subrole: nil, identifier: "input", url: nil, title: "Old text", description: "Old description")
        precondition(editor.matches(["AXRole": "AXTextField", "AXIdentifier": "input", "AXValue": "New text", "AXTitle": "New text", "AXDescription": "New description", "AXEnabled": "true"]))
        let document = ElementIdentity(role: "AXWindow", subrole: nil, identifier: nil, url: nil, title: "Unsaved title", description: nil, document: "file:///one.txt")
        precondition(document.matches(["AXRole": "AXWindow", "AXDocument": "file:///one.txt", "AXTitle": "New title"]))
        precondition(!document.matches(["AXRole": "AXWindow", "AXDocument": "file:///two.txt"]))
        let partial = ElementIdentity(facts: .init(values: ["AXRole": "AXButton"]))
        let preserved = command.refining(with: partial)!
        precondition(preserved == command)
        precondition(command.check(.init(values: ["AXRole": "AXButton"])) == .unavailable)
        precondition(command.check(.init(values: ["AXRole": "AXButton", "AXTitle": "Open Bob"])) == .changed,
                     "known contradiction takes precedence over other unavailable fields")
        precondition(command.check(.init(values: ["AXRole": "AXButton"], absent: ["AXIdentifier"])) == .changed)
        precondition(command.refining(with: .init(facts: .init(absent: ["AXIdentifier"]), roleHint: "AXButton")) == nil)
        let added = command.refining(with: .init(facts: .init(values: ["AXSubrole": "special"]), roleHint: "AXButton"))!
        precondition(added.attributes["AXTitle"] == "Open Alice" && added.attributes["AXSubrole"] == "special")
        precondition(added.refining(with: .init(facts: .init(values: ["AXSubrole": "changed"]))) == nil)
        let empty = ElementIdentity(facts: .init())
        precondition(empty.check(.init()) == .unavailable, "empty evidence must not verify an input identity")
        let missingLabel = ElementIdentity(facts: .init(values: ["AXRole": "AXButton"], absent: ["AXTitle"]))
        precondition(missingLabel.check(.init(values: ["AXRole": "AXButton"])) == .matched,
                     "a never-present optional attribute must not become a fresh input requirement")
        precondition(missingLabel.check(.init(values: ["AXRole": "AXButton", "AXTitle": ""])) == .matched)
        let enriched = missingLabel.refining(with: .init(facts: .init(values: ["AXRole": "AXButton", "AXTitle": "new command"])))!
        precondition(enriched.attributes["AXTitle"] == "new command" && !enriched.absent.contains("AXTitle"))
        precondition(enriched.check(.init(values: ["AXRole": "AXButton", "AXTitle": "different command"])) == .changed)
        let changingValue = ElementIdentity(facts: .init(values: ["AXRole": "AXTextField", "AXTitle": "text", "AXValue": "edited"]))
        precondition(changingValue.attributes == ["AXRole": "AXTextField"], "mutable text does not become identity evidence")
        let cell = ElementIdentity(facts: .init(values: ["AXRole": "AXTextField", "AXRowIndexRange": "1:1", "AXColumnIndexRange": "7:1"]))
        precondition(cell.check(.init(values: cell.attributes.merging(["AXValue": "formatted", "AXSelected": "true"]) { _, new in new })) == .matched)
        for (key, value) in [("AXRowIndexRange", "2:1"), ("AXColumnIndexRange", "8:1"), ("AXRowIndexRange", "1:2")] {
            let changed = ElementIdentity.Facts(values: cell.attributes.merging([key: value]) { _, new in new })
            precondition(cell.check(changed) == .changed, "position or span changes cannot reuse an old logical target")
            precondition(cell.refining(with: .init(facts: changed)) == nil)
        }
        let incompleteCell = ElementIdentity.Facts(values: ["AXRole": "AXTextField", "AXColumnIndexRange": "7:1"])
        precondition(cell.check(incompleteCell) == .unavailable)
        precondition(cell.refining(with: .init(facts: incompleteCell)) == cell, "temporary failures preserve the old logical constraint")
        precondition(cell.check(.init(values: ["AXRole": "AXTextField"], absent: ["AXRowIndexRange"])) == .changed)
        print("PASS: changed commands/record URLs expire; editable values and availability are not identity")
        print("PASS identity evidence: unknown, absence, refinement, contradictions and incomplete observations")
        print("PASS logical positions: recycled cells, merged ranges, unavailable facts and ordinary content edits")
    }
}
