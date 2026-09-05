package com.jiuwenswarm.plugin.ui

import com.google.gson.Gson
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.wm.ToolWindow
import com.intellij.ui.jcef.JBCefBrowser
import com.intellij.ui.jcef.JBCefBrowserBase
import com.intellij.ui.jcef.JBCefJSQuery
import com.jiuwenswarm.plugin.swarm.SwarmSnapshot
import org.cef.browser.CefBrowser
import org.cef.browser.CefFrame
import org.cef.handler.CefLoadHandlerAdapter
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import javax.swing.JComponent

private val LOG = logger<SwarmMapPanel>()
private val gsonSM = Gson()

class SwarmMapPanel(toolWindow: ToolWindow) : Disposable {

    private val browser = JBCefBrowser()
    private val jsQuery: JBCefJSQuery
    private val ready = AtomicBoolean(false)
    private val pending = AtomicReference<SwarmSnapshot?>(null)
    private val pendingDebug = ArrayDeque<String>()

    /** Called by SwarmMapToolWindowFactory to wire up JS→Kotlin messages. */
    var onMessage: ((String) -> Unit)? = null

    val component: JComponent get() = browser.component

    init {
        jsQuery = JBCefJSQuery.create(browser as JBCefBrowserBase)
        jsQuery.addHandler { raw ->
            onMessage?.invoke(raw)
            JBCefJSQuery.Response("ok")
        }

        browser.jbCefClient.addLoadHandler(object : CefLoadHandlerAdapter() {
            override fun onLoadEnd(b: CefBrowser, frame: CefFrame, httpStatusCode: Int) {
                if (!frame.isMain) return
                injectBridge()
                ready.set(true)
                pending.getAndSet(null)?.let { postSnapshot(it) }
                // Flush debug lines buffered while the webview was loading.
                val buffered = ArrayList(pendingDebug)
                pendingDebug.clear()
                for (line in buffered) postDebug(line)
            }
        }, browser.cefBrowser)

        browser.loadHTML(readSwarmMapHtml(), "http://localhost")
        Disposer.register(toolWindow.disposable, this)
    }

    // ──────────────────────────────────────────
    // Public API
    // ──────────────────────────────────────────

    /** Thread-safe: buffers if the panel is not yet ready. */
    fun postSnapshot(snapshot: SwarmSnapshot) {
        if (!ready.get()) {
            pending.set(snapshot)
            return
        }
        val json = gsonSM.toJson(snapshot)
            .replace("\\", "\\\\")
            .replace("`", "\\`")
        val js = "if(window.__swarmReceive) window.__swarmReceive(JSON.parse(`$json`));"
        ApplicationManager.getApplication().invokeLater {
            if (!browser.isDisposed) {
                browser.cefBrowser.executeJavaScript(js, browser.cefBrowser.url, 0)
            }
        }
    }

    /** Send a debug-log line to the webview; buffered until the webview is ready. */
    fun postDebug(line: String) {
        if (!ready.get()) {
            pendingDebug.addLast(line)
            while (pendingDebug.size > 500) pendingDebug.removeFirst()
            return
        }
        val escaped = line.replace("\\", "\\\\").replace("`", "\\`")
        val js = "if(window.__swarmDebug) window.__swarmDebug(`$escaped`);"
        ApplicationManager.getApplication().invokeLater {
            if (!browser.isDisposed) {
                browser.cefBrowser.executeJavaScript(js, browser.cefBrowser.url, 0)
            }
        }
    }

    // ──────────────────────────────────────────
    // Bridge injection
    // ──────────────────────────────────────────

    private fun injectBridge() {
        val inject = """
            window.__swarmSend = function(jsonStr) {
                ${jsQuery.inject("jsonStr")}
            };
        """.trimIndent()
        browser.cefBrowser.executeJavaScript(inject, browser.cefBrowser.url, 0)
    }

    // ──────────────────────────────────────────
    // HTML loading (same resolution chain as chat.html)
    // ──────────────────────────────────────────

    private fun readSwarmMapHtml(): String {
        // 1. Packaged resource (production)
        javaClass.classLoader.getResource("webview/swarm_map.html")?.let {
            return it.readText()
        }
        // 2. Development — walk up from the jar to the monorepo shared-webview
        try {
            val jarDir = File(javaClass.protectionDomain.codeSource.location.toURI())
            val devHtml = generateSequence(jarDir) { it.parentFile }
                .take(6)
                .map { File(it, "packages/shared-webview/swarm_map.html") }
                .firstOrNull { it.exists() }
            if (devHtml != null) return devHtml.readText()
        } catch (_: Exception) {}
        return fallbackHtml()
    }

    private fun fallbackHtml() =
        "<html><body style='padding:12px;color:#ccc;background:#1e1e1e'>" +
        "<b>Swarm Map</b><br><br>swarm_map.html not found in plugin resources." +
        "</body></html>"

    override fun dispose() {
        browser.dispose()
    }
}
