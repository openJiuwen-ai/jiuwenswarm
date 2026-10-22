import AppKit
import QuartzCore
import ImageIO
import UniformTypeIdentifiers

/// Render the production component offscreen; no desktop windows or input.
@main struct AgentPointerAppearanceTests {
    @MainActor static func main() throws {
        func render(scale: CGFloat, idle: Bool = false) -> CGContext {
            let size = AgentPointerAssets.size
            let context = CGContext(data: nil, width: Int(size.width * scale), height: Int(size.height * scale),
                bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)!
            context.scaleBy(x: scale, y: scale)
            context.translateBy(x: AgentPointerAssets.hotspot.x, y: size.height - AgentPointerAssets.hotspot.y)
            let pointer = AgentPointerAppearance()
            pointer.moveTo(.zero, animated: false, scale: scale)
            pointer.setState(idle ? .idle : .active)
            func prepare(_ layer: CALayer) {
                layer.removeAllAnimations(); layer.displayIfNeeded()
                for child in layer.sublayers ?? [] { prepare(child) }
            }
            prepare(pointer.layer)
            pointer.layer.render(in: context)
            pointer.stop()
            return context
        }
        func metrics(_ context: CGContext) -> (alpha: Int, visible: Int, colors: Set<UInt32>) {
            let data = context.data!.assumingMemoryBound(to: UInt8.self)
            var alpha = 0, visible = 0, colors = Set<UInt32>()
            for y in 0..<context.height {
                for x in 0..<context.width {
                    let offset = y * context.bytesPerRow + x * 4
                    let a = Int(data[offset + 3]); alpha += a
                    if a > 100 {
                        visible += 1
                        colors.insert(UInt32(data[offset]) << 16 | UInt32(data[offset + 1]) << 8 | UInt32(data[offset + 2]))
                    }
                }
            }
            return (alpha, visible, colors)
        }
        for scale in [CGFloat(1), 2, 6] {
            let active = render(scale: scale), idle = render(scale: scale, idle: true)
            let a = metrics(active), b = metrics(idle)
            precondition(a.visible > Int(120 * scale * scale), "artwork is visible at each screen scale")
            precondition(a.visible < Int(550 * scale * scale), "transparent exterior is retained")
            precondition(a.colors.count > 40, "gradient/highlight colors survive native drawing")
            let ratio = Double(b.alpha) / Double(a.alpha)
            precondition(ratio > 0.38 && ratio < 0.42, "idle opacity applies to the complete glass artwork")
            let file = URL(fileURLWithPath: ".artifacts/native-tests/pointer-glass-\(Int(scale))x.png")
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            let destination = CGImageDestinationCreateWithURL(file as CFURL, UTType.png.identifier as CFString, 1, nil)!
            CGImageDestinationAddImage(destination, active.makeImage()!, nil)
            precondition(CGImageDestinationFinalize(destination))
        }
        print("PASS pointer artwork: production pixels at 1x/2x/6x, gradients, transparency and idle opacity")
    }
}
