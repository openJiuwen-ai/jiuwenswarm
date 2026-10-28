import CoreGraphics

@main struct AgentPointerGeometryTests {
    static func main() {
        let frame = CGRect(x: -500, y: -200, width: 800, height: 600)
        let flipped = AgentPointerGeometry.appKit(frame, primaryHeight: 900)
        precondition(flipped == CGRect(x: -500, y: 500, width: 800, height: 600))
        precondition(!AgentPointerGeometry.valid(CGRect(x: 0, y: 0, width: CGFloat.infinity, height: 2)))
        precondition(AgentPointerAppearance.duration(from: .zero, to: .zero) == 0.12)
        precondition(AgentPointerAppearance.duration(from: .zero, to: CGPoint(x: 900, y: 0)) == 0.28)
        precondition(AgentPointerAppearance.duration(from: .zero, to: CGPoint(x: 900, y: 0), dragging: true) == 0.08)
        let shape = AgentPointerAssets.path()
        let strokeBounds = shape.copy(strokingWithWidth: AgentPointerAssets.outlineWidth, lineCap: .round, lineJoin: .round, miterLimit: 1).boundingBoxOfPath
        let size = AgentPointerAssets.size, hotspot = AgentPointerAssets.hotspot
        let canvas = CGRect(x: -hotspot.x, y: hotspot.y - size.height, width: size.width, height: size.height)
        precondition(canvas.insetBy(dx: -0.01, dy: -0.01).contains(strokeBounds), "the outline must fit its SVG viewport")
        precondition(strokeBounds.width >= size.width * 0.8 && strokeBounds.height >= size.height * 0.8,
            "the pointer must remain legible at its declared size, with room for optical margins")
        precondition(shape.contains(CGPoint(x: 10, y: -12)))
        precondition(!shape.contains(CGPoint(x: 21, y: -25)), "inner notch must not become a solid triangle")
        let offset = CGPoint(x: 165, y: 111)
        precondition(AgentPointerGeometry.restingPoint(offset: offset, in: frame) == CGPoint(x: -335, y: -89))
        precondition(AgentPointerGeometry.restingPoint(offset: offset, in: frame.offsetBy(dx: 100, dy: 75)) == CGPoint(x: -235, y: -14))
        let tiny = CGRect(x: -50, y: 100, width: 20, height: 20)
        precondition(AgentPointerGeometry.restingPoint(offset: offset, in: tiny) == CGPoint(x: 115, y: 211),
            "a smaller window clips the pointer, never rewrites its original anchor")
        let edge = CGPoint(x: frame.width - 2, y: frame.height - 2)
        precondition(AgentPointerGeometry.restingPoint(offset: edge, in: frame) == CGPoint(x: frame.maxX - 2, y: frame.maxY - 2),
            "cursor artwork must not push the hotspot away from an edge control")
        print("PASS pointer geometry: negative display origins, invalid bounds, bounded animation, idle window movement/resizing")
    }
}
