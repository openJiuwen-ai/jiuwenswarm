@main struct KeyboardEventRouteTests {
    static func main() {
        // Web renderers often expose no native window at all. Their AXWindow
        // still identifies the host (checked separately by the native adapter).
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: nil,
            webAreaWindow: nil, hasHostedWebArea: true) == .host)
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: 100,
            webAreaWindow: 100, hasHostedWebArea: true) == .host)
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: nil,
            webAreaWindow: 100, hasHostedWebArea: true) == .host)

        // A missing service window never proves that its keyboard endpoint is
        // the host. Non-web remote sheets must keep the service responder route.
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: nil,
            webAreaWindow: nil, hasHostedWebArea: false) == .service)
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: 200,
            webAreaWindow: nil, hasHostedWebArea: false) == .service)

        // A native view service can itself contain a WebView. An independently
        // identified service window still receives the event, never the host.
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: 200,
            webAreaWindow: nil, hasHostedWebArea: true) == .service)
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: nil,
            webAreaWindow: 200, hasHostedWebArea: true) == .service)
        precondition(KeyboardEventRoute.resolve(hostWindow: 100, focusWindow: 200,
            webAreaWindow: 200, hasHostedWebArea: true) == .service)
        print("PASS: hosted web renderers use host events; native view-service windows retain their own routes")
    }
}
