package com.jiuwenswarm.plugin

import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.logger
import com.jiuwenswarm.plugin.client.SessionManager
import com.jiuwenswarm.plugin.client.WsClient
import com.jiuwenswarm.plugin.client.WsStatus
import com.jiuwenswarm.plugin.settings.JiuwenSwarmSettings
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

private val LOG = logger<JiuwenSwarmService>()

/**
 * Application-level singleton that owns the WebSocket connection and session manager.
 * Registered as applicationService in plugin.xml.
 */
class JiuwenSwarmService : Disposable {

    val settings = JiuwenSwarmSettings.instance()
    val ws = WsClient(
        settings.wsUrl,
        if (settings.keepAliveEnabled) settings.keepAliveInterval.toLong() else 0,
    )
    val session = SessionManager(ws, settings.channelId)

    /** Cumulative token count for the current session, updated by ChatToolWindow. */
    @Volatile var lastTokenCount: Int = 0

    // If the server isn't ready at connection time it skips sending connection.ack.
    // We schedule a one-time reconnect after 15s so the IDE retries once the agent
    // prewarm finishes (typically ~8s after startup).
    private val ackRetryExecutor = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "jiuwenswarm-ack-retry").also { it.isDaemon = true }
    }
    @Volatile private var ackRetryPending = false

    init {
        if (settings.autoConnect) {
            ws.connect()
        }
        ws.addStatusListener { status ->
            when (status) {
                WsStatus.CONNECTED -> scheduleAckRetry()
                WsStatus.DISCONNECTED, WsStatus.RECONNECTING -> ackRetryPending = false
                else -> {}
            }
        }
    }

    private fun scheduleAckRetry() {
        if (ackRetryPending) return
        ackRetryPending = true
        ackRetryExecutor.schedule({
            ackRetryPending = false
            if (ws.isConnected() && session.sessionId == null) {
                LOG.info("[JiuwenSwarm] No session.ack after 15s — server may have been initialising. Reconnecting once.")
                ws.reconnect()
            }
        }, 15L, TimeUnit.SECONDS)
    }

    override fun dispose() {
        ackRetryExecutor.shutdownNow()
        session.dispose()
        ws.dispose()
    }

    companion object {
        fun instance(): JiuwenSwarmService =
            ApplicationManager.getApplication().getService(JiuwenSwarmService::class.java)
    }
}
