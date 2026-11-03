import AppKit
import QuartzCore

/// Rasterized by Core Animation at the current screen scale from generated SVG
/// drawing commands. The artwork remains independent of motion and input.
private final class AgentPointerArtworkLayer: CALayer {
    override func draw(in context: CGContext) { AgentPointerAssets.draw(in: context) }
}

/// Reusable visual component. Needs only AppKit/QuartzCore and generated assets;
/// knows nothing about apps, windows, accessibility, input or desktop policy.
@MainActor final class AgentPointerAppearance {
    enum State { case active, idle, dragging }
    private typealias Style = AgentPointerAssets
    let layer = CALayer()
    private let entrance = CALayer()
    private let glyph = CALayer()
    private let body = AgentPointerArtworkLayer()
    private let flash = CAShapeLayer()
    private let ring = CAShapeLayer()
    private var movementEndsAt: CFTimeInterval = 0
    private var dragging = false
    private var reducedMotion = false

    init() {
        // Motion layers keep their origin at the hotspot. Only the artwork has
        // finite bounds, positioned so its SVG origin shares the same hotspot.
        for value in [layer, entrance, glyph, body, flash, ring] { value.anchorPoint = .zero }
        body.bounds = CGRect(origin: .zero, size: Style.size)
        body.position = CGPoint(x: -Style.hotspot.x, y: Style.hotspot.y - Style.size.height)
        body.needsDisplayOnBoundsChange = true; body.setNeedsDisplay()
        flash.path = Style.path(); flash.fillColor = Style.reducedMotionPressedColor; flash.opacity = 0
        ring.path = Self.circle(Style.rippleToRadius); ring.strokeColor = Style.rippleColor
        ring.fillColor = nil; ring.lineWidth = Style.rippleWidth; ring.opacity = 0
        glyph.addSublayer(body); glyph.addSublayer(flash)
        entrance.addSublayer(ring); entrance.addSublayer(glyph); layer.addSublayer(entrance)
    }

    func setReducedMotion(_ value: Bool) {
        guard reducedMotion != value else { return }
        reducedMotion = value
        cancelFeedback()
        layer.removeAnimation(forKey: "move"); movementEndsAt = 0
        setDragging(dragging)
    }

    func setState(_ state: State) {
        setDragging(state == .dragging)
        setOpacity(state == .idle ? Float(Style.idleOpacity) : 1)
    }

    private func setOpacity(_ value: Float) {
        guard layer.opacity != value else { return }
        let from = layer.presentation()?.opacity ?? layer.opacity
        CATransaction.begin(); CATransaction.setDisableActions(true)
        layer.opacity = value
        let animation = CABasicAnimation(keyPath: "opacity")
        animation.fromValue = from; animation.toValue = value
        animation.duration = value < from ? Style.idleFade : Style.idleWake
        animation.timingFunction = Self.easing()
        layer.add(animation, forKey: "idle")
        CATransaction.commit()
    }

    /// Host chooses whether a move should animate and whether to fade in.
    func moveTo(_ position: CGPoint, animated: Bool = true, appear: Bool = false, scale: CGFloat = 1) {
        let now = CACurrentMediaTime()
        let from = layer.presentation()?.position ?? layer.position
        let changed = layer.position != position
        CATransaction.begin(); CATransaction.setDisableActions(true)
        if body.contentsScale != scale { body.setNeedsDisplay() }
        for value in [layer, entrance, glyph, body, flash, ring] { value.contentsScale = scale }
        layer.isHidden = false
        layer.position = position
        if !animated || appear || reducedMotion {
            layer.removeAnimation(forKey: "move"); movementEndsAt = now
        } else if changed {
            // Replace movement rather than queuing old path segments. Feedback
            // is cancelled at operation boundaries, not at every drag point.
            let animation = CABasicAnimation(keyPath: "position")
            animation.fromValue = NSValue(point: from); animation.toValue = NSValue(point: position)
            animation.duration = Self.duration(from: from, to: position, dragging: dragging)
            animation.timingFunction = Self.easing()
            layer.add(animation, forKey: "move"); movementEndsAt = now + animation.duration
        }
        if appear {
            cancelFeedback()
            let appearance = CABasicAnimation(keyPath: "opacity")
            appearance.fromValue = 0; appearance.toValue = 1; appearance.duration = Style.appear
            entrance.add(appearance, forKey: "appear")
        }
        CATransaction.commit()
    }

    private func setDragging(_ value: Bool) {
        dragging = value
        let scale: CGFloat = value && !reducedMotion ? Style.dragScale : 1
        let current = glyph.value(forKeyPath: "transform.scale") as? CGFloat ?? 1
        guard current != scale else { return }
        let from = glyph.presentation()?.value(forKeyPath: "transform.scale") ?? current
        CATransaction.begin(); CATransaction.setDisableActions(true)
        glyph.setValue(scale, forKeyPath: "transform.scale")
        let animation = CABasicAnimation(keyPath: "transform.scale")
        animation.fromValue = from; animation.toValue = scale; animation.duration = reducedMotion ? 0 : Style.dragTransition
        glyph.add(animation, forKey: "hold")
        CATransaction.commit()
    }

    func click(count: Int = 1) {
        cancelFeedback()
        let delay = max(0, movementEndsAt - CACurrentMediaTime())
        if reducedMotion {
            let animation = CAKeyframeAnimation(keyPath: "opacity")
            animation.values = [1, 1, 0]
            animation.keyTimes = [0, NSNumber(value: Style.reducedMotionHoldUntil), 1]
            animation.duration = Style.reducedMotionFlash; flash.add(animation, forKey: "feedback")
            return
        }
        let ripple = CAAnimationGroup()
        let expansion = CABasicAnimation(keyPath: "path")
        expansion.fromValue = Self.circle(Style.rippleFromRadius); expansion.toValue = Self.circle(Style.rippleToRadius)
        expansion.duration = Style.rippleDuration
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = Style.rippleFromOpacity; fade.toValue = 0; fade.duration = Style.rippleDuration
        ripple.animations = [expansion, fade]; ripple.duration = Style.rippleDuration
        ripple.beginTime = CACurrentMediaTime() + delay
        ring.add(ripple, forKey: "feedback")
        if !dragging {
            let press = CAKeyframeAnimation(keyPath: "transform.scale")
            press.values = count > 1 ? Style.pressDoubleScales : Style.pressScales
            press.keyTimes = (count > 1 ? Style.pressDoubleTimes : Style.pressTimes).map { NSNumber(value: $0) }
            press.duration = count > 1 ? Style.pressDoubleDuration : Style.pressDuration; press.beginTime = ripple.beginTime
            glyph.add(press, forKey: "feedback")
        }
    }

    func cancelFeedback() {
        ring.removeAllAnimations(); glyph.removeAnimation(forKey: "feedback"); flash.removeAllAnimations()
    }

    func hide() {
        layer.isHidden = true; cancelFeedback()
        layer.removeAnimation(forKey: "move"); movementEndsAt = 0
    }

    func stop() {
        for value in [layer, entrance, glyph, body, flash, ring] { value.removeAllAnimations() }
        layer.removeFromSuperlayer()
    }

    private static func circle(_ radius: CGFloat) -> CGPath {
        CGPath(ellipseIn: CGRect(x: -radius, y: -radius, width: radius * 2, height: radius * 2), transform: nil)
    }

    nonisolated static func duration(from: CGPoint, to: CGPoint, dragging: Bool = false) -> Double {
        let distance = hypot(to.x - from.x, to.y - from.y)
        return dragging ? min(Style.dragMax, max(Style.dragMin, distance / Style.dragPointsPerSecond))
            : min(Style.moveMax, max(Style.moveMin, distance / Style.movePointsPerSecond))
    }

    private static func easing() -> CAMediaTimingFunction {
        let e = Style.easing
        return CAMediaTimingFunction(controlPoints: e[0], e[1], e[2], e[3])
    }
}
