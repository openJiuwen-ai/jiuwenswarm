/// The accessibility service and the process receiving AppKit key events are
/// different identities. Web content is exposed by a renderer, but its host
/// WebView forwards keyboard events from the host window to that renderer.
/// Native view services with their own window retain their own event route.
enum KeyboardEventRoute: Equatable {
    case host
    case service

    /// The caller must already establish the focused AX object's presenter and
    /// owning window. A WebArea is evidence only through actual AX containment,
    /// never through application names, titles or process-name heuristics.
    static func resolve(hostWindow: UInt32, focusWindow: UInt32?,
                        webAreaWindow: UInt32?, hasHostedWebArea: Bool) -> Self {
        guard hasHostedWebArea,
              focusWindow == nil || focusWindow == hostWindow,
              webAreaWindow == nil || webAreaWindow == hostWindow else { return .service }
        return .host
    }
}
