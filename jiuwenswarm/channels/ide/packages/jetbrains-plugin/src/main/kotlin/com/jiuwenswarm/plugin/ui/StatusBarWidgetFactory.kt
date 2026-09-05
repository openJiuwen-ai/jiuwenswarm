package com.jiuwenswarm.plugin.ui

import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.wm.StatusBar
import com.intellij.openapi.wm.StatusBarWidget
import com.intellij.openapi.wm.StatusBarWidgetFactory
import com.jiuwenswarm.plugin.JiuwenSwarmService
import com.jiuwenswarm.plugin.client.WsStatus
import com.intellij.util.Consumer
import java.awt.Color
import java.awt.Component
import java.awt.Graphics
import java.awt.Graphics2D
import java.awt.RenderingHints
import java.awt.event.MouseEvent
import javax.swing.Icon
import javax.swing.SwingUtilities

class StatusBarWidgetFactory : StatusBarWidgetFactory {
    override fun getId() = "JiuwenSwarmStatusWidget"
    override fun getDisplayName() = "JiuwenSwarm"
    override fun isAvailable(project: Project) = true
    override fun createWidget(project: Project): StatusBarWidget = JiuwenStatusBarWidget()
    override fun disposeWidget(widget: StatusBarWidget) = Disposer.dispose(widget)
    override fun canBeEnabledOn(statusBar: StatusBar) = true
}

/**
 * Status bar widget using [StatusBarWidget.IconPresentation].
 *
 * We render a coloured text string as an [Icon] via Java2D rather than relying on
 * [StatusBarWidget.TextPresentation], which forces the IDE's default muted text colour.
 * This gives us per-state colours (teal = connected, yellow = reconnecting, grey = off).
 */
class JiuwenStatusBarWidget : StatusBarWidget, StatusBarWidget.IconPresentation {

    private val service = JiuwenSwarmService.instance()
    private var bar: StatusBar? = null
    private val statusListener: (WsStatus) -> Unit = { update() }

    override fun ID() = "JiuwenSwarmStatusWidget"
    override fun getPresentation(): StatusBarWidget.WidgetPresentation = this

    override fun install(statusBar: StatusBar) {
        bar = statusBar
        service.ws.addStatusListener(statusListener)
        update()
    }

    override fun dispose() {
        service.ws.removeStatusListener(statusListener)
        bar = null
    }

    // ── IconPresentation ──────────────────────────────────────────────────────

    override fun getTooltipText(): String {
        val tokens = service.lastTokenCount
        val tokenSuffix = if (tokens > 0) " | ${formatTokenCount(tokens)} tokens used" else ""
        return when (service.ws.getStatus()) {
            WsStatus.CONNECTED    -> "JiuwenSwarm: Connected — session ${service.session.sessionId?.take(8) ?: "none"}$tokenSuffix"
            WsStatus.CONNECTING   -> "JiuwenSwarm: Connecting…"
            WsStatus.RECONNECTING -> "JiuwenSwarm: Reconnecting…"
            WsStatus.DISCONNECTED -> "JiuwenSwarm: Disconnected — click to reconnect"
        }
    }

    private fun formatTokenCount(n: Int): String = when {
        n >= 1_000_000 -> "%.1fM".format(n / 1_000_000.0)
        n >= 1_000     -> "%.1fk".format(n / 1_000.0)
        else           -> n.toString()
    }

    override fun getClickConsumer(): Consumer<MouseEvent> = Consumer { e ->
        if (SwingUtilities.isLeftMouseButton(e) && service.ws.getStatus() == WsStatus.DISCONNECTED) {
            service.ws.connect()
        }
    }

    override fun getIcon(): Icon {
        val tokens = service.lastTokenCount
        val tokenLabel = if (tokens > 0) " · ${formatTokenCount(tokens)}" else ""
        val (label, color) = when (service.ws.getStatus()) {
            WsStatus.CONNECTED    -> "⬤ JiuwenSwarm$tokenLabel" to Color(0x2d, 0xa4, 0x4e)
            WsStatus.CONNECTING   -> "◌ JiuwenSwarm" to Color(0x2d, 0xa4, 0x4e)
            WsStatus.RECONNECTING -> "↻ JiuwenSwarm" to Color(0xdc, 0xdc, 0xaa)
            WsStatus.DISCONNECTED -> "○ JiuwenSwarm" to Color(0x6b, 0x6b, 0x6b)
        }
        return StatusTextIcon(label, color)
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    private fun update() {
        SwingUtilities.invokeLater { bar?.updateWidget(ID()) }
    }
}

/**
 * An [Icon] that paints a coloured text string using the host component's font.
 * Width is fixed to the widest possible label so the status bar layout stays stable.
 */
private class StatusTextIcon(private val text: String, private val color: Color) : Icon {

    override fun getIconWidth(): Int = maxOf(110, text.length * 8)  // scales with token label
    override fun getIconHeight(): Int = 16

    override fun paintIcon(c: Component?, g: Graphics, x: Int, y: Int) {
        val g2 = g.create() as Graphics2D
        try {
            g2.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON)
            g2.font = c?.font ?: g.font
            g2.color = color
            val fm = g2.fontMetrics
            // Vertically center within the icon height
            g2.drawString(text, x, y + fm.ascent + (getIconHeight() - fm.height) / 2)
        } finally {
            g2.dispose()
        }
    }
}
