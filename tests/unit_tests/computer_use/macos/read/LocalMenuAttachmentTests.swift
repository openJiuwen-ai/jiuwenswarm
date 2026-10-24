@main struct LocalMenuAttachmentTests {
    static func main() throws {
        var roles = ["window":"AXWindow", "wrapper":"AXGroup", "menu":"AXMenu", "item":"AXMenuItem"]
        var edges = ["window":["wrapper"], "wrapper":["menu"], "menu":["item"]]
        func check(_ ancestors: [String] = ["window", "wrapper", "menu"]) -> Bool {
            LocalMenuAttachment.confirms(leaf:"item", ancestors:ancestors, window:"window", same: { $0 == $1 }, role:{ roles[$0] }, children:{ edges[$0] ?? [] })
        }
        precondition(check())
        edges["wrapper"] = []; precondition(!check()); edges["wrapper"] = ["menu"]
        roles["wrapper"] = "AXWindow"; precondition(!check())
        roles["wrapper"] = "AXMenuBar"; precondition(!check())
        roles["wrapper"] = "AXGroup"; precondition(!check(["other", "menu"]))
        roles["item"] = "AXButton"; precondition(!check())
        print("PASS: local menu attachment, detached/reparented menus and unrelated window boundaries")
    }
}
