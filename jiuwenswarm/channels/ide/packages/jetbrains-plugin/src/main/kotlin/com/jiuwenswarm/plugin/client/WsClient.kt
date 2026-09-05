package com.jiuwenswarm.plugin.client

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.intellij.openapi.Disposable
import com.intellij.openapi.diagnostic.logger
import okhttp3.*
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

private val LOG = logger<WsClient>()

enum class WsStatus { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING }

typealias StatusListener = (WsStatus) -> Unit
typealias MessageListener = (JsonObject) -> Unit

private val BACKOFF_SECONDS = longArrayOf(1, 2, 4, 8, 16, 30)
private val gson = Gson()

class WsClient(
    url: String,
    pingIntervalSeconds: Long = 15,
) : Disposable {

    // On macOS, "localhost" resolves to ::1 (IPv6) before 127.0.0.1 (IPv4).
    // If the server only listens on IPv4, OkHttp gets "Connection Refused" on ::1
    // and stays in reconnect loop. Force IPv4 to avoid this.
    private val url = url.replace("://localhost:", "://127.0.0.1:")

    @Volatile private var pingIntervalSec: Long = pingIntervalSeconds.coerceIn(0, 300)

    private var client: OkHttpClient = buildClient()

    private fun buildClient(): OkHttpClient {
        val builder = OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
        if (pingIntervalSec > 0) {
            builder.pingInterval(pingIntervalSec, TimeUnit.SECONDS)
        }
        return builder.build()
    }

    /** Rebuild the underlying OkHttp client with a new keep-alive interval. */
    fun setPingInterval(seconds: Long) {
        val clamped = seconds.coerceIn(0, 300)
        if (pingIntervalSec == clamped) return
        pingIntervalSec = clamped
        client = buildClient()
    }

    private val scheduler = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "jiuwenswarm-ws-scheduler").also { it.isDaemon = true }
    }

    @Volatile private var ws: WebSocket? = null
    @Volatile private var status = WsStatus.DISCONNECTED
    private val retryCount = AtomicInteger(0)
    private var retryFuture: ScheduledFuture<*>? = null
    @Volatile private var destroyed = false

    private val statusListeners = CopyOnWriteArrayList<StatusListener>()
    private val messageListeners = CopyOnWriteArrayList<MessageListener>()

    fun addStatusListener(l: StatusListener) = statusListeners.add(l)
    fun removeStatusListener(l: StatusListener) = statusListeners.remove(l)
    fun addMessageListener(l: MessageListener) = messageListeners.add(l)
    fun removeMessageListener(l: MessageListener) = messageListeners.remove(l)

    fun connect() {
        if (destroyed) return
        retryFuture?.cancel(false)
        setStatus(WsStatus.CONNECTING)
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, Listener())
    }

    /** Force a fresh reconnect (used for "New session"). */
    fun reconnect() {
        if (destroyed) return
        retryFuture?.cancel(false)
        retryCount.set(0)
        ws?.close(1000, "User requested reconnect")
        ws = null
        connect()
    }

    fun send(json: JsonObject): Boolean {
        val sock = ws ?: return false
        return sock.send(gson.toJson(json))
    }

    fun isConnected() = status == WsStatus.CONNECTED

    fun getStatus() = status

    override fun dispose() {
        destroyed = true
        retryFuture?.cancel(false)
        ws?.close(1000, "Plugin disposed")
        ws = null
        scheduler.shutdownNow()
        client.dispatcher.executorService.shutdown()
        setStatus(WsStatus.DISCONNECTED)
    }

    private fun setStatus(s: WsStatus) {
        if (status == s) return
        status = s
        statusListeners.forEach { it(s) }
    }

    private fun scheduleReconnect() {
        if (destroyed) return
        val delay = BACKOFF_SECONDS[minOf(retryCount.getAndIncrement(), BACKOFF_SECONDS.size - 1)]
        setStatus(WsStatus.RECONNECTING)
        LOG.info("Reconnecting in ${delay}s (attempt ${retryCount.get()})")
        retryFuture = scheduler.schedule({ if (!destroyed) connect() }, delay, TimeUnit.SECONDS)
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            LOG.info("Connected to JiuwenSwarm at $url")
            retryCount.set(0)
            setStatus(WsStatus.CONNECTED)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val obj = gson.fromJson(text, JsonObject::class.java)
                messageListeners.forEach { it(obj) }
            } catch (e: Exception) {
                LOG.warn("Failed to parse message: ${text.take(100)}", e)
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            LOG.warn("WebSocket error", t)
            ws = null
            scheduleReconnect()
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            ws = null
            if (!destroyed) scheduleReconnect()
        }
    }
}
