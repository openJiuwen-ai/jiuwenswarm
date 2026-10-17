import Foundation
import CoreGraphics

@main struct DesktopGeometryTests {
    static func main() throws {
        func display(_ id: UInt32, _ x: Double, _ y: Double, _ w: Double, _ h: Double,
                     pixels: Int = 2000, rotation: Double = 0) -> DesktopGeometry.Display {
            .init(id: id, frame: CGRect(x: x, y: y, width: w, height: h), pixelsWide: pixels, pixelsHigh: pixels, rotation: rotation)
        }
        func fails(_ operation: () throws -> Void) { do { try operation(); fatalError("expected rejection") } catch {} }
        let main = display(1, 0, 0, 1440, 900), left = display(2, -1920, -200, 1920, 1080)
        let layout = try DesktopGeometry([main, left])
        precondition(layout.width == 3360 && layout.height == 1100)
        precondition(layout.rect(main.frame) == CGRect(x: 1920, y: 200, width: 1440, height: 900))
        let origin = try layout.point(x: 1920, y: 200, captured: [1, 2])
        precondition(origin == .zero, "desktop PNG origin differs from Quartz origin")
        let negative = try layout.point(x: 0, y: 0, captured: [1, 2])
        precondition(negative == CGPoint(x: -1920, y: -200))
        fails { _ = try layout.point(x: 1920, y: 0, captured: [1, 2]) } // hole above main
        fails { _ = try layout.point(x: 100, y: 1090, captured: [1, 2]) } // hole below left
        fails { _ = try layout.point(x: 10, y: 10, captured: [1]) } // missing display image
        for pair in [(-1.0, 0.0), (3360, 0), (0, 1100), (.nan, 0), (0, .infinity)] {
            fails { _ = try layout.point(x: pair.0, y: pair.1, captured: [1, 2]) }
        }
        let large = try DesktopGeometry([display(1, -5000, -1000, 10001, 3003)])
        precondition(large.width == 4096 && large.height <= 4096)
        let thumbnail = try DesktopGeometry([main, left], maxDimension: 320)
        precondition(thumbnail.width == 320 && thumbnail.height <= 320 && thumbnail != layout)
        let thumbRect = thumbnail.rect(main.frame)
        let thumbOrigin = try thumbnail.point(x: thumbRect.minX, y: thumbRect.minY, captured: [1, 2])
        precondition(abs(thumbOrigin.x) < 1e-9 && abs(thumbOrigin.y) < 1e-9)
        let singlePixel = try DesktopGeometry([main, left], maxDimension: 1)
        precondition(singlePixel.width == 1 && singlePixel.height == 1)
        for budget in [0, -1, 4097] { fails { _ = try DesktopGeometry([main], maxDimension: budget) } }
        let native = try large.point(x: Double(large.width) / 2, y: Double(large.height) / 2, captured: [1])
        precondition(abs(native.x - 0.5) < 1e-9 && abs(native.y - 501.5) < 1e-9, "both axes use actual PNG dimensions")
        let overlap = try DesktopGeometry([main, display(2, 0, 0, 1440, 900)])
        fails { _ = try overlap.point(x: 10, y: 10, captured: [1, 2]) }
        let reordered = try DesktopGeometry([left, main])
        let retina = try DesktopGeometry([main, display(2, -1920, -200, 1920, 1080, pixels: 4000)])
        let rotated = try DesktopGeometry([main, display(2, -1920, -200, 1920, 1080, rotation: 90)])
        precondition(reordered == layout, "enumeration order cannot change mapping identity")
        precondition(retina != layout && rotated != layout)
        try layout.validatePath(from: CGPoint(x: -100, y: 300), to: CGPoint(x: 300, y: 300), captured: [1, 2])
        fails { try layout.validatePath(from: CGPoint(x: -100, y: -100), to: CGPoint(x: 300, y: -100), captured: [1, 2]) }
        fails { try layout.validatePath(from: CGPoint(x: -100, y: 300), to: CGPoint(x: 300, y: 300), captured: [1]) }
        fails { try overlap.validatePath(from: .zero, to: CGPoint(x: 300, y: 300), captured: [1, 2]) }
        let gap = try DesktopGeometry([display(1, 0, 0, 100, 100), display(2, 101, 0, 100, 100)])
        fails { try gap.validatePath(from: CGPoint(x: 1, y: 50), to: CGPoint(x: 200, y: 50), captured: [1, 2]) }
        fails { _ = try DesktopGeometry([]) }
        fails { _ = try DesktopGeometry([main, main]) }
        fails { _ = try DesktopGeometry([display(1, 0, 0, 0, 3)]) }
        print("PASS desktop geometry: negative origins, display gaps, partial capture, independent pixel scales, bounds, overlap, mode/rotation changes")
    }
}
