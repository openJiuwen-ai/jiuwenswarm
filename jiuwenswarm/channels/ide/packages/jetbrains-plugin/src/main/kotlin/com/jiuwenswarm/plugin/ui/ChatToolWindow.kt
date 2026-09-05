package com.jiuwenswarm.plugin.ui

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.intellij.codeInsight.navigation.actions.GotoDeclarationAction
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ReadAction
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.editor.EditorFactory
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.util.TextRange
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.openapi.wm.WindowManager
import com.intellij.psi.PsiManager
import com.intellij.psi.search.GlobalSearchScope
import com.intellij.psi.search.PsiSearchHelper
import com.intellij.ui.content.ContentFactory
import com.intellij.ui.jcef.JBCefApp
import com.intellij.ui.jcef.JBCefBrowser
import com.intellij.ui.jcef.JBCefBrowserBase
import com.intellij.ui.jcef.JBCefJSQuery
import com.jiuwenswarm.plugin.JiuwenSwarmService
import com.jiuwenswarm.plugin.client.SessionInfo
import com.jiuwenswarm.plugin.client.WsStatus
import com.jiuwenswarm.plugin.context.ContextCollector
import com.jiuwenswarm.plugin.editor.DiffApplier
import com.jiuwenswarm.plugin.terminal.TerminalManager
import com.jiuwenswarm.plugin.settings.JiuwenSwarmSettings
import com.jiuwenswarm.plugin.swarm.SwarmStateManager
import org.cef.browser.CefBrowser
import org.cef.browser.CefFrame
import org.cef.handler.CefLoadHandlerAdapter
import java.io.File
import javax.swing.BorderFactory
import javax.swing.JComponent
import javax.swing.JLabel

private val LOG = logger<ChatToolWindowFactory>()
private val gson = Gson()

class ChatToolWindowFactory : ToolWindowFactory {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        if (!JBCefApp.isSupported()) {
            addFallback(toolWindow,
                "<html><body style='padding:8px'>" +
                "<b>JiuwenSwarm</b> requires JCEF (Chromium Embedded Framework).<br><br>" +
                "Enable it via:<br>" +
                "<code>Help → Find Action → Registry → ide.browser.jcef.enabled</code><br><br>" +
                "Then restart the IDE.</body></html>")
            return
        }
        try {
            val panel = ChatPanel(project, toolWindow)
            Disposer.register(toolWindow.disposable, panel)
            // Store the panel on its own component so SendSelectionAction can retrieve it
            panel.component.putClientProperty("jiuwenswarm.panel", panel)
            val content = ContentFactory.getInstance()
                .createContent(panel.component, "", false)
            toolWindow.contentManager.addContent(content)
        } catch (e: Exception) {
            LOG.error("Failed to initialise JiuwenSwarm chat panel", e)
            addFallback(toolWindow,
                "<html><body style='padding:8px'>" +
                "<b>JiuwenSwarm failed to load.</b><br>${e.message}</body></html>")
        }
    }

    override fun shouldBeAvailable(project: Project) = true

    private fun addFallback(toolWindow: ToolWindow, html: String) {
        val label = JLabel(html)
        label.border = BorderFactory.createEmptyBorder(12, 12, 12, 12)
        val content = ContentFactory.getInstance().createContent(label, "", false)
        toolWindow.contentManager.addContent(content)
    }
}

class ChatPanel(
    private val project: Project,
    private val toolWindow: ToolWindow,
) : Disposable {

    private val service = JiuwenSwarmService.instance()
    private val browser = JBCefBrowser()
    private val jsQuery: JBCefJSQuery

    // Tracks the last requestId sent to the server so we can match streaming
    // events that carry no request_id in their payload (gateway quirk).
    @Volatile private var lastRequestId: String? = null

    // Currently selected model (from the webview model selector); sent per-message.
    @Volatile private var activeModel: String? = null

    // Last session id we loaded history for, so history is fetched exactly once
    // per session even though sendCurrentStatus() may be called many times
    // (status changes, session changes, explicit switch, panel load).
    @Volatile private var historyLoadedForSession: String? = null

    // Debug logging is toggled from the webview; when true we log to IDEA log.
    @Volatile private var debugEnabled = false

    // Last user message text — used as commit message suggestion.
    @Volatile private var lastUserInput: String? = null

    // Snapshot tracking for checkpoint/rewind feature.
    // currentTurnSnapshots: file path → content before first edit this turn (null = file didn't exist).
    // lastTurnSnapshots: promoted from currentTurnSnapshots on chat.final; used for rewind.
    private val currentTurnSnapshots = mutableMapOf<String, String?>()
    @Volatile private var lastTurnSnapshots = mapOf<String, String?>()

    // Swarm state — updated on every team event; feeds the Swarm Map panel
    val swarmStateManager = SwarmStateManager()
    var swarmMapPanel: SwarmMapPanel? = null

    val component: JComponent get() = browser.component

    private var memoryTimer: java.util.Timer? = null

    init {
        jsQuery = JBCefJSQuery.create(browser as JBCefBrowserBase)
        jsQuery.addHandler { request ->
            handleWebviewMessage(request)
            JBCefJSQuery.Response("ok")
        }

        // Inject bridge function once page loads
        browser.jbCefClient.addLoadHandler(object : CefLoadHandlerAdapter() {
            override fun onLoadEnd(b: CefBrowser, frame: CefFrame, httpStatusCode: Int) {
                if (frame.isMain) {
                    // Reset the history guard on (re)load so a panel opened after an
                    // early (possibly dropped) connect still fetches the session history.
                    historyLoadedForSession = null
                    injectBridge()
                    sendCurrentStatus()
                }
            }
        }, browser.cefBrowser)

        // Listen for WS status, session, and message events
        service.ws.addStatusListener(::onStatusChange)
        service.session.addSessionListener(::onSessionChange)
        service.ws.addMessageListener(::onJiuwenMessage)

        // Poll server memory usage and stream it to the webview chip
        startMemoryPolling()

        // Load the chat HTML
        loadChatHtml()
    }

    private fun startMemoryPolling() {
        stopMemoryPolling()
        memoryTimer = java.util.Timer("jiuwenswarm-memory", true)
        memoryTimer?.schedule(object : java.util.TimerTask() {
            override fun run() {
                try {
                    val (rss, total, available) = service.session.getMemoryUsage()
                    dispatchToWebview(mapOf(
                        "type" to "memory",
                        "rssMb" to rss,
                        "totalMb" to total,
                        "availableMb" to available,
                    ))
                } catch (_: Exception) {
                    // memory.compute may be unavailable; silently ignore
                }
            }
        }, 0L, 10_000L)
    }

    private fun stopMemoryPolling() {
        memoryTimer?.cancel()
        memoryTimer = null
    }

    // ──────────────────────────────────────────
    // Load HTML
    // ──────────────────────────────────────────
    private fun loadChatHtml() {
        val html = readChatHtml()
        // Use http://localhost as the base URL so WebSocket to ws://localhost:... is allowed by JCEF.
        browser.loadHTML(html, "http://localhost")
    }

    private fun readChatHtml(): String {
        // 1. Packaged resource (production)
        javaClass.classLoader.getResource("webview/chat.html")?.let {
            return it.readText()
        }
        // 2. Development (monorepo sibling next to the plugin source tree)
        try {
            val jarDir = File(javaClass.protectionDomain.codeSource.location.toURI())
            val devHtml = generateSequence(jarDir) { it.parentFile }
                .take(6)
                .map { File(it, "packages/shared-webview/chat.html") }
                .firstOrNull { it.exists() }
            if (devHtml != null) return devHtml.readText()
        } catch (_: Exception) {}
        return fallbackHtml()
    }

    // ──────────────────────────────────────────
    // Bridge injection (JS ↔ Kotlin)
    // ──────────────────────────────────────────
    private fun injectBridge() {
        val inject = """
            window.__jb_send = function(jsonStr) {
                ${jsQuery.inject("jsonStr")}
            };
            // Notify app that bridge is ready
            if (window.__jb_dispatch) window.__jb_dispatch('{"type":"bridge_ready"}');
        """.trimIndent()
        browser.cefBrowser.executeJavaScript(inject, browser.cefBrowser.url, 0)
    }

    fun dispatchToWebview(msg: JsonObject) {
        val json = gson.toJson(msg)
            .replace("\\", "\\\\")  // must come first: escape backslashes before single-quotes
            .replace("'", "\\'")
        val js = "if(window.__jb_dispatch) window.__jb_dispatch('$json');"
        ApplicationManager.getApplication().invokeLater {
            browser.cefBrowser.executeJavaScript(js, browser.cefBrowser.url, 0)
        }
    }

    fun dispatchToWebview(msg: Map<String, Any?>) {
        dispatchToWebview(gson.toJsonTree(msg).asJsonObject)
    }

    /** Navigate to a symbol definition mentioned by the agent in chat. */
    private fun navigateToSymbol(symbol: String) {
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val scope = GlobalSearchScope.projectScope(project)
                val helper = PsiSearchHelper.getInstance(project)
                val files = helper.findFilesWithPlainTextWords(symbol)
                    .filter { scope.contains(it.virtualFile) }

                if (files.isEmpty()) {
                    debug("navigate_symbol: no files contain '$symbol'")
                    return@executeOnPooledThread
                }

                ApplicationManager.getApplication().invokeLater {
                    try {
                        val file = files.first()
                        val doc = com.intellij.openapi.editor.EditorFactory.getInstance()
                            .createDocument(file.text)
                        val text = doc.charsSequence
                        val idx = text.indexOf(symbol)
                        val offset = if (idx >= 0) idx else 0
                        OpenFileDescriptor(project, file.virtualFile, offset).navigate(true)
                    } catch (e: Exception) {
                        debug("navigate_symbol failed: ${e.message}")
                    }
                }
            } catch (e: Exception) {
                debug("navigate_symbol failed: ${e.message}")
            }
        }
    }

    // ──────────────────────────────────────────
    // Swarm Map → Plugin messages (open_lane etc.)
    // ──────────────────────────────────────────
    fun handleSwarmMessage(raw: String) {
        try {
            val msg = gson.fromJson(raw, com.google.gson.JsonObject::class.java)
            when (msg.get("type")?.asString) {
                "open_lane" -> {
                    val memberName = msg.get("memberName")?.asString ?: return
                    val filePath = swarmStateManager.snapshot().lanes
                        .firstOrNull { it.memberName == memberName }
                        ?.lastActivePath ?: return
                    ApplicationManager.getApplication().invokeLater {
                        val vf = LocalFileSystem.getInstance().findFileByPath(filePath)
                            ?: LocalFileSystem.getInstance().refreshAndFindFileByPath(filePath)
                        if (vf != null) OpenFileDescriptor(project, vf).navigate(true)
                    }
                }
            }
        } catch (_: Exception) {}
    }

    /** Feed a line into the Swarm Map's debug console (no-op until that panel exists). */
    private fun swarmDebug(line: String) {
        swarmMapPanel?.postDebug(line)
    }

    // ──────────────────────────────────────────
    // Webview → Plugin messages
    // ──────────────────────────────────────────
    private fun handleWebviewMessage(jsonStr: String) {
        try {
            val msg = gson.fromJson(jsonStr, JsonObject::class.java)
            when (msg.get("type")?.asString) {
                "ready" -> { sendCurrentStatus(); sendGitStatus() }
                "send" -> {
                    val content = msg.get("content")?.asString ?: return
                    val mode = msg.get("mode")?.asString ?: "code.plan"
                    val rid = msg.get("requestId")?.asString ?: return
                    val mediaItems = msg.getAsJsonArray("media_items")
                    val model = msg.get("model")?.asString ?: activeModel
                    val mentionedPaths = msg.getAsJsonArray("mentionedPaths")
                        ?.mapNotNull { it.asString } ?: emptyList()
                    lastRequestId = rid
                    // Clear snapshots from previous turn; rewind is no longer valid once user sends a new message
                    currentTurnSnapshots.clear()
                    lastTurnSnapshots = emptyMap()
                    dispatchToWebview(mapOf("type" to "rewindable", "enabled" to false))
                    lastUserInput = content
                    debug("SEND  → requestId=$rid mode=$mode content=${content.take(60)} media=${mediaItems?.size() ?: 0}")
                    val ideContext = ContextCollector.collect(project, mentionedPaths)
                    if (!service.session.sendChat(content, mode, rid, ideContext, mediaItems, model)) {
                        debug("SEND  → FAILED (no session or disconnected)")
                        dispatchToWebview(mapOf(
                            "type" to "error",
                            "message" to "Not connected or no active session",
                            "requestId" to rid
                        ))
                    } else {
                        debug("SEND  → OK")
                    }
                }
                "answer" -> {
                    val rid = msg.get("requestId")?.asString ?: return
                    val answers = msg.get("answers")?.asJsonArray ?: return
                    if (answers.size() == 0) return
                    val source = msg.get("source")?.asString ?: "confirm_interrupt"
                    val mode = msg.get("mode")?.asString ?: "code.plan"
                    debug("ANSWER→ requestId=$rid source=$source options=${answers.size()}")
                    if (!service.session.sendAnswer(rid, answers, source, mode)) {
                        dispatchToWebview(mapOf(
                            "type" to "error",
                            "message" to "Not connected or no active session",
                            "requestId" to rid
                        ))
                    }
                }
                "toggle_debug" -> {
                    debugEnabled = msg.get("enabled")?.asBoolean ?: false
                    debug("Debug mode toggled: $debugEnabled")
                }
                "new_session" -> ApplicationManager.getApplication().executeOnPooledThread {
                    try {
                        debug("ACTION→ new_session (reconnecting for fresh session)")
                        currentTurnSnapshots.clear()
                        lastTurnSnapshots = emptyMap()
                        dispatchToWebview(mapOf("type" to "rewindable", "enabled" to false))
                        service.ws.reconnect()
                    } catch (e: Exception) {
                        dispatchToWebview(mapOf("type" to "error", "message" to e.message))
                    }
                }
                "switch_session" -> {
                    val sid = msg.get("sessionId")?.asString ?: return
                    // session.switch is a team-mode server operation — the user's
                    // chat mode (code.plan / code.normal / code.team) is sent per-message,
                    // not per-session.
                    debug("ACTION→ switch_session $sid mode=code.team")
                    ApplicationManager.getApplication().executeOnPooledThread {
                        try {
                            service.session.switchSession(sid, "code.team")
                            // sendCurrentStatus() loads the new session's history exactly
                            // once (guarded by historyLoadedForSession) — no separate
                            // loadHistory call here, which used to triple-load history.
                            sendCurrentStatus()
                        } catch (e: Exception) {
                            dispatchToWebview(mapOf("type" to "error", "message" to e.message))
                        }
                    }
                }
                "list_sessions" -> ApplicationManager.getApplication().executeOnPooledThread {
                    try {
                        debug("ACTION→ list_sessions")
                        val sessions = service.session.listSessions()
                        debug("ACTION→ list_sessions returned ${sessions.size} sessions")
                        dispatchToWebview(mapOf("type" to "sessions", "sessions" to sessions.map { it.toMap() }))
                    } catch (e: Exception) {
                        LOG.warn("list_sessions failed", e)
                        dispatchToWebview(mapOf("type" to "sessions_error", "message" to (e.message ?: "Failed to load sessions")))
                    }
                }
                "list_skills" -> ApplicationManager.getApplication().executeOnPooledThread {
                    try {
                        debug("ACTION→ list_skills")
                        val skills = service.session.listSkills()
                        debug("ACTION→ list_skills returned ${skills.size} skills")
                        val skillMaps = skills.map { obj ->
                            mapOf(
                                "skill_id"    to (obj.get("skill_id")?.asString    ?: ""),
                                "name"        to (obj.get("name")?.asString         ?: obj.get("skill_id")?.asString ?: ""),
                                "description" to (obj.get("description")?.asString  ?: ""),
                                "enabled"     to (obj.get("enabled")?.asBoolean     ?: true),
                                "trigger"     to (obj.get("trigger")?.asString      ?: ""),
                            )
                        }
                        dispatchToWebview(mapOf("type" to "skills", "skills" to skillMaps))
                    } catch (e: Exception) {
                        LOG.warn("list_skills failed", e)
                        dispatchToWebview(mapOf("type" to "skills_error", "message" to (e.message ?: "Failed to load skills")))
                    }
                }
                "toggle_skill" -> {
                    val skillId = msg.get("skillId")?.asString ?: return
                    val enabled = msg.get("enabled")?.asBoolean ?: return
                    ApplicationManager.getApplication().executeOnPooledThread {
                        try {
                            debug("ACTION→ toggle_skill $skillId enabled=$enabled")
                            service.session.toggleSkill(skillId, enabled)
                            dispatchToWebview(mapOf("type" to "skill_toggled", "skillId" to skillId, "enabled" to enabled))
                        } catch (e: Exception) {
                            LOG.warn("toggle_skill failed", e)
                            dispatchToWebview(mapOf("type" to "skills_error", "message" to (e.message ?: "Failed to toggle skill")))
                        }
                    }
                }
                "switch_model" -> {
                    val model = msg.get("model")?.asString ?: ""
                    activeModel = model
                    debug("ACTION→ switch_model $model")
                    dispatchToWebview(mapOf("type" to "model_changed", "model" to model))
                }
                "rename_session" -> {
                    val title = msg.get("title")?.asString ?: return
                    ApplicationManager.getApplication().executeOnPooledThread {
                        try {
                            debug("ACTION→ session.rename title=$title")
                            service.session.renameSession(title)
                            dispatchToWebview(mapOf("type" to "session_renamed", "title" to title))
                        } catch (e: Exception) {
                            dispatchToWebview(mapOf("type" to "error", "message" to e.message))
                        }
                    }
                }
                "delete_session" -> {
                    val sid = msg.get("sessionId")?.asString ?: return
                    ApplicationManager.getApplication().executeOnPooledThread {
                        try {
                            debug("ACTION→ delete_session $sid")
                            service.session.deleteSession(sid)
                            dispatchToWebview(mapOf("type" to "session_deleted", "sessionId" to sid))
                        } catch (e: Exception) {
                            LOG.warn("delete_session failed", e)
                            dispatchToWebview(mapOf("type" to "sessions_error",
                                "message" to (e.message ?: "Failed to delete session")))
                        }
                    }
                }
                "export_session" -> {
                    val historyPath = msg.get("historyPath")?.asString ?: return
                    val sessionTitle = msg.get("sessionTitle")?.asString ?: "session"
                    ApplicationManager.getApplication().executeOnPooledThread {
                        try {
                            debug("ACTION→ export_session historyPath=$historyPath")
                            val jsonlFile = File(historyPath)
                            if (!jsonlFile.exists()) {
                                dispatchToWebview(mapOf("type" to "error", "message" to "History file not found: $historyPath"))
                                return@executeOnPooledThread
                            }
                            val md = buildMarkdownFromJsonl(jsonlFile, sessionTitle)
                            val safeName = sessionTitle.replace(Regex("[^\\w\\- ]"), "").trim()
                                .replace(' ', '-').ifEmpty { "session" }
                            val outFile = File(project.basePath ?: System.getProperty("user.home"),
                                "jiuwenswarm-export-$safeName.md")
                            outFile.writeText(md)
                            ApplicationManager.getApplication().invokeLater {
                                val vf = LocalFileSystem.getInstance().refreshAndFindFileByPath(outFile.absolutePath)
                                if (vf != null) OpenFileDescriptor(project, vf).navigate(true)
                            }
                            dispatchToWebview(mapOf("type" to "export_done", "path" to outFile.absolutePath))
                        } catch (e: Exception) {
                            LOG.warn("export_session failed", e)
                            dispatchToWebview(mapOf("type" to "error", "message" to "Export failed: ${e.message}"))
                        }
                    }
                }
                "rewind" -> {
                    val snapshots = lastTurnSnapshots
                    if (snapshots.isEmpty()) return
                    ApplicationManager.getApplication().executeOnPooledThread {
                        var restored = 0
                        var failed = 0
                        for ((path, originalContent) in snapshots) {
                            try {
                                WriteCommandAction.runWriteCommandAction(project, "Rewind agent changes", null, Runnable {
                                    val vf = LocalFileSystem.getInstance().refreshAndFindFileByPath(path)
                                    if (originalContent == null) {
                                        vf?.delete(this)
                                    } else if (vf != null) {
                                        vf.setBinaryContent(originalContent.toByteArray(vf.charset))
                                    }
                                })
                                restored++
                            } catch (e: Exception) {
                                debug("Rewind failed for $path: ${e.message}")
                                failed++
                            }
                        }
                        lastTurnSnapshots = emptyMap()
                        val resultMsg = if (failed == 0) "Rewound $restored file(s)"
                                        else "Rewound $restored file(s), $failed failed"
                        dispatchToWebview(mapOf("type" to "rewind_done", "message" to resultMsg,
                            "restored" to restored, "failed" to failed))
                    }
                }
                "open_file" -> {
                    val path = msg.get("path")?.asString ?: return
                    val line = msg.get("line")?.asInt ?: 0
                    ApplicationManager.getApplication().invokeLater {
                        val vf = LocalFileSystem.getInstance().findFileByPath(path)
                            ?: LocalFileSystem.getInstance().refreshAndFindFileByPath(path)
                        if (vf != null) {
                            OpenFileDescriptor(project, vf, maxOf(0, line - 1), 0).navigate(true)
                        } else {
                            debug("open_file: not found: $path")
                        }
                    }
                }
                "navigate_symbol" -> {
                    val symbol = msg.get("symbol")?.asString ?: return
                    ApplicationManager.getApplication().invokeLater {
                        navigateToSymbol(symbol)
                    }
                }
                "stop" -> {
                    debug("ACTION→ stop (chat.interrupt)")
                    service.session.interrupt()
                }
                "files_request" -> {
                    val files = ContextCollector.gatherWorkspaceFiles(project)
                    dispatchToWebview(mapOf("type" to "files", "files" to files))
                }
                "git_status_request" -> sendGitStatus()
                "git_commit_request" -> if (JiuwenSwarmSettings.instance().gitEnabled) {
                    ApplicationManager.getApplication().invokeLater { handleGitCommit() }
                }
                "git_push_request" -> if (JiuwenSwarmSettings.instance().gitEnabled) {
                    ApplicationManager.getApplication().executeOnPooledThread { handleGitPush() }
                }
            }
        } catch (e: Exception) {
            LOG.warn("Failed to parse webview message: $jsonStr", e)
        }
    }

    // ──────────────────────────────────────────
    // JiuwenSwarm events → webview
    // ──────────────────────────────────────────
    private fun debug(line: String) {
        if (!debugEnabled) return
        LOG.info("[JiuwenSwarmDebug] $line")
        dispatchToWebview(mapOf("type" to "debug_log", "line" to line))
    }

    private fun onStatusChange(@Suppress("UNUSED_PARAMETER") s: WsStatus) {
        debug("WS status → $s")
        sendCurrentStatus()
    }

    private fun onSessionChange(sid: String?) {
        debug("Session → $sid")
        swarmStateManager.reset(sid ?: "")
        swarmMapPanel?.postSnapshot(swarmStateManager.snapshot())
        sendCurrentStatus()
    }

    private fun onJiuwenMessage(msg: JsonObject) {
        // Skip request-response protocol messages (type:"res") — SessionManager handles
        // those synchronously via its CompletableFuture.  They are never chat events.
        if (msg.get("type")?.asString == "res") return

        debug("RAW ← ${gson.toJson(msg)}")

        // ── Swarm team event interception ──
        // Team events arrive either as E2A chat.delta with "team.event:" prefix,
        // or as old-format { type:"event", event:"team.*" } messages.
        // Consume them here and do not forward to the webview.
        val rawTeamEvent = extractTeamEventDelta(msg)
        if (rawTeamEvent != null) {
            swarmStateManager.applyTeamEvent(rawTeamEvent)
            val snap = swarmStateManager.snapshot()
            if (swarmMapPanel == null) {
                swarmMapPanel = SwarmMapToolWindowFactory.getPanel(project)
            }
            swarmDebug("team.event: $rawTeamEvent")
            swarmMapPanel?.postSnapshot(snap)
            // Auto-open the Swarm Map tool window when the first agent spawns
            if (snap.lanes.size == 1 && snap.lanes[0].status != "SHUTDOWN") {
                ApplicationManager.getApplication().invokeLater {
                    SwarmMapToolWindowFactory.openOrReveal(project)
                }
            }
            return
        }

        val converted = convertServerMessageToLegacyEvent(msg, lastRequestId)
        if (converted != null) {
            val et = converted.get("event_type")?.asString
            // ── Route file-edit tool calls to DiffApplier (show diff or auto-apply) ──
            // Server sends E2A-format messages (response_kind/body), so the old-format
            // {type:"event", event:"chat.tool_call"} gate never matches. Dispatch from
            // the converted legacy event instead, which covers both wire formats.
            // Run on the EDT: DiffApplier touches VFS / documents and opens the diff
            // dialog, none of which are safe from a pooled thread.
            if (et == "chat.tool_call") {
                ApplicationManager.getApplication().invokeLater {
                    DiffApplier.handle(project, converted)
                }
            }
            // ── Snapshot files before they are edited so rewind can restore them ──
            // Swarm tool attribution runs for both tool_call and tool_update so real
            // sub-agent activity stubs lanes even when team.member.spawned is not sent.
            if (et == "chat.tool_call" || et == "chat.tool_update") {
                val payload = converted.getAsJsonObject("payload") ?: JsonObject()
                val toolName = payload.get("tool_name")?.asString
                    ?: payload.getAsJsonObject("tool_call")?.get("name")?.asString
                    ?: ""
                // File-edit snapshots (only when rewind is enabled in settings)
                if (JiuwenSwarmSettings.instance().rewindEnabled &&
                    toolName in setOf("str_replace_editor", "write_file", "create_file", "edit_file")) {
                    val tcArgs = payload.getAsJsonObject("tool_call")?.get("arguments")
                    val args: JsonObject? = when {
                        tcArgs != null && tcArgs.isJsonObject -> tcArgs.asJsonObject
                        tcArgs != null && tcArgs.isJsonPrimitive -> runCatching {
                            JsonParser.parseString(tcArgs.asString).asJsonObject
                        }.getOrNull()
                        else -> null
                    } ?: payload.getAsJsonObject("tool_input") ?: payload.getAsJsonObject("input")
                    val path = args?.get("file_path")?.asString ?: args?.get("path")?.asString
                    if (path != null && !currentTurnSnapshots.containsKey(path)) {
                        val vf = LocalFileSystem.getInstance().findFileByPath(path)
                        currentTurnSnapshots[path] = vf?.let {
                            try {
                                ReadAction.compute<String, Throwable> {
                                    String(it.contentsToByteArray(), it.charset)
                                }
                            } catch (_: Exception) { null }
                        }
                        debug("SNAP  → snapshotted $path (existed=${vf != null})")
                    }
                }
                // Terminal integration for bash commands
                if (toolName == "bash" || toolName == "run_command") {
                    val cmd = TerminalManager.extractCommand(payload)
                    if (cmd != null) {
                        debug("TERM  → $cmd")
                        TerminalManager.runCommand(project, cmd)
                    }
                }
                // Swarm tool attribution — update the agent lane's current activity
                applySwarmToolAttribution(payload)
            }
            // ── Swarm model-call lifecycle — lane "thinking…" / "generating…" ──
            // llm_call_start fires before the first token of every model call, so the
            // lane shows "thinking…" instead of drifting into "idle". The first streamed
            // token flips it to "generating…", and llm_call_end clears it back.
            if (et == "chat.llm_call_start" || et == "chat.llm_call_end") {
                val payload = converted.getAsJsonObject("payload") ?: JsonObject()
                val memberName = payload.get("member_name")?.takeIf { it.isJsonPrimitive }?.asString
                if (et == "chat.llm_call_start") swarmStateManager.applyModelCallStart(memberName)
                else swarmStateManager.applyModelCallEnd(memberName)
                if (swarmMapPanel == null) swarmMapPanel = SwarmMapToolWindowFactory.getPanel(project)
                swarmMapPanel?.postSnapshot(swarmStateManager.snapshot())
            } else if (et == "chat.reasoning" || et == "chat.delta") {
                val payload = converted.getAsJsonObject("payload") ?: JsonObject()
                payload.get("member_name")?.takeIf { it.isJsonPrimitive }?.asString?.let {
                    swarmStateManager.applyModelTokenStart(it)
                }
            }
            // ── On turn end, promote snapshots and show rewind bar ──
            if (et == "chat.final") {
                if (currentTurnSnapshots.isNotEmpty()) {
                    lastTurnSnapshots = currentTurnSnapshots.toMap()
                    dispatchToWebview(mapOf("type" to "rewindable", "enabled" to true))
                    debug("SNAP  → turn complete, ${lastTurnSnapshots.size} file(s) snapshotted")
                }
                currentTurnSnapshots.clear()
                sendGitStatus()
            }
            debug("CONV  → event_type=$et request_id=${converted.get("request_id")?.asString}")
            dispatchToWebview(mapOf("type" to "jiuwen_event", "event" to converted))
            trackTokenUsage(converted)
        } else {
            debug("CONV  → dropped (not a recognised chat event)")
        }
    }

    /** Extract token counts from chat.usage_summary and update the service for the status bar.
     *  Token data is nested under payload.usage for this event type.
     *  Per-turn incremental display is handled in the webview via chat.usage_metadata.
     */
    private fun trackTokenUsage(event: JsonObject) {
        val et = event.get("event_type")?.asString ?: return
        if (et != "chat.usage_summary") return
        val payload = event.getAsJsonObject("payload") ?: return
        val usage   = payload.getAsJsonObject("usage") ?: return
        val input   = usage.get("input_tokens")?.asInt  ?: 0
        val output  = usage.get("output_tokens")?.asInt ?: 0
        service.lastTokenCount += input + output
        // Refresh the status bar widget so the new count appears immediately
        ApplicationManager.getApplication().invokeLater {
            WindowManager.getInstance().getStatusBar(project)?.updateWidget("JiuwenSwarmStatusWidget")
        }
    }

    /**
     * Extract the raw team-event JSON from a server message, or return null if
     * the message is not a team event.
     *
     * Two wire formats are supported:
     *  - E2A chunk: response_kind="e2a.chunk", body.event_type="chat.delta",
     *               body.delta starts with "team.event:" — the rest is the JSON payload.
     *  - Old format: type="event", event starts with "team." — wrap payload in envelope.
     *
     * The returned string is always in the { "event": { "type": ..., ... } } shape
     * that SwarmStateManager.applyTeamEvent() expects (or the flat fallback it tolerates).
     */
    private fun extractTeamEventDelta(msg: JsonObject): String? {
        val responseKind = msg.get("response_kind")?.asString
        // E2A chunk path
        if (responseKind == "e2a.chunk") {
            val body = msg.getAsJsonObject("body") ?: return null
            val eventType = body.get("event_type")?.asString ?: ""
            val delta = body.get("delta")
            // Legacy shape: chat.delta whose text is a "team.event:"-prefixed JSON string.
            if (eventType == "chat.delta" && delta?.isJsonPrimitive == true) {
                val text = delta.asString
                if (text.startsWith("team.event:")) {
                    return text.removePrefix("team.event:")
                }
                return null
            }
            // Object shape: event_type is a team.* category (e.g. "team.task",
            // "team.member") and delta is the full payload { event: { type, ... } }.
            // Normalize it into the { "event": { "type": ..., ... } } shape the
            // SwarmStateManager dispatches on.
            if ((eventType.startsWith("team.") || eventType == "workflow.updated") && delta?.isJsonObject == true) {
                val pl = delta.asJsonObject
                val inner = pl.getAsJsonObject("event")
                if (inner != null && inner.get("type")?.isJsonPrimitive == true) {
                    return gson.toJson(JsonObject().apply { add("event", inner.deepCopy()) })
                }
                // Flat fallback: delta itself carries the team payload fields.
                return gson.toJson(pl)
            }
            return null
        }
        // Old-format path
        if (msg.get("type")?.asString == "event") {
            val eventName = msg.get("event")?.asString ?: return null
            if (!eventName.startsWith("team.")) return null
            val payload = msg.getAsJsonObject("payload")
            // The gateway nests the real event under payload.event (e.g. event:"team.member"
            // with payload.event.type === "team.member.spawned"). Use the inner event so the
            // type SwarmStateManager dispatches on is the specific one, not the category.
            val inner = payload?.getAsJsonObject("event")
            if (inner != null && inner.get("type")?.asString != null) {
                return gson.toJson(JsonObject().apply { add("event", inner.deepCopy()) })
            }
            // Legacy fallback: flat payload with the category name as the event type.
            val flat = payload?.deepCopy() ?: JsonObject()
            flat.addProperty("type", eventName)
            return gson.toJson(JsonObject().apply { add("event", flat) })
        }
        return null
    }

    /**
     * Update the swarm lane for a tool call/update event. Parses the tool name from
     * either payload.tool_name (chat.tool_update) or tool_call.name (chat.tool_call),
     * and tolerates `arguments` being a JSON string or an object. No-ops when the
     * tool or member name is missing.
     */
    private fun applySwarmToolAttribution(payload: JsonObject) {
        val toolName = payload.get("tool_name")?.takeIf { it.isJsonPrimitive }?.asString
            ?: payload.getAsJsonObject("tool_call")?.get("name")?.takeIf { it.isJsonPrimitive }?.asString
            ?: return
        val memberName = payload.get("member_name")?.takeIf { it.isJsonPrimitive }?.asString ?: return
        val tcArgs = payload.getAsJsonObject("tool_call")?.get("arguments")
            ?: payload.get("arguments")
            ?: payload.get("tool_input")
            ?: payload.get("input")
        val args: JsonObject? = when {
            tcArgs != null && tcArgs.isJsonObject -> tcArgs.asJsonObject
            tcArgs != null && tcArgs.isJsonPrimitive -> runCatching {
                JsonParser.parseString(tcArgs.asString).asJsonObject
            }.getOrNull()
            else -> null
        }
        val filePath = args?.get("path")?.takeIf { it.isJsonPrimitive }?.asString
            ?: args?.get("file_path")?.takeIf { it.isJsonPrimitive }?.asString
        swarmStateManager.applyToolCall(toolName, filePath, memberName, args)
        if (swarmMapPanel == null) swarmMapPanel = SwarmMapToolWindowFactory.getPanel(project)
        swarmDebug("tool: $toolName" + (memberName?.let { " · $it" } ?: ""))
        swarmMapPanel?.postSnapshot(swarmStateManager.snapshot())
    }

    /** Convert server messages (E2A or old format) to the legacy event format the webview expects.
     *  Webview expects: { event_type, request_id, payload }
     *  Pure conversion — no side-effects (no dispatch, no DiffApplier calls).
     */
    private fun convertServerMessageToLegacyEvent(msg: JsonObject, fallbackRequestId: String? = null): JsonObject? {
        val responseKind = msg.get("response_kind")?.asString

        // ── E2A format ──
        if (responseKind != null) {
            val requestId = msg.get("request_id")?.asString ?: ""
            val body = msg.getAsJsonObject("body") ?: return null

            return when (responseKind) {
                "e2a.chunk" -> {
                    val eventType = body.get("event_type")?.asString ?: ""
                    val delta = body.get("delta")
                    JsonObject().apply {
                        addProperty("event_type", eventType)
                        addProperty("request_id", requestId)
                        val payloadObj = JsonObject()
                        when (eventType) {
                            "chat.delta" -> payloadObj.addProperty("text", delta?.asString ?: "")
                            "chat.reasoning" -> payloadObj.addProperty("text", delta?.asString ?: "")
                            else -> if (delta?.isJsonObject == true) {
                                for ((k, v) in delta.asJsonObject.entrySet()) payloadObj.add(k, v)
                            }
                        }
                        body.get("member_name")?.takeIf { it.isJsonPrimitive }?.let {
                            payloadObj.addProperty("member_name", it.asString)
                        }
                        add("payload", payloadObj)
                    }
                }
                "e2a.complete" -> {
                    val result = body.getAsJsonObject("result")
                    val eventType = result?.get("event_type")?.asString ?: "chat.final"
                    JsonObject().apply {
                        addProperty("event_type", eventType)
                        addProperty("request_id", requestId)
                        add("payload", result ?: JsonObject())
                    }
                }
                "e2a.error" -> {
                    val details = body.getAsJsonObject("details")
                    val errorMsg = body.get("message")?.asString ?: "Unknown error"
                    JsonObject().apply {
                        addProperty("event_type", "chat.error")
                        addProperty("request_id", requestId)
                        add("payload", JsonObject().apply {
                            addProperty("error", errorMsg)
                            if (details != null) add("details", details)
                        })
                    }
                }
                else -> null
            }
        }

        // ── Old format (used for connection.ack and direct events) ──
        if (msg.get("type")?.asString == "event") {
            val eventName = msg.get("event")?.asString ?: ""
            val payload = msg.getAsJsonObject("payload") ?: JsonObject()
            val mappedPayload = payload.deepCopy()
            // Webview expects "text" for delta events, gateway sends "content"
            if (eventName == "chat.delta" && mappedPayload.has("content") && !mappedPayload.has("text")) {
                mappedPayload.addProperty("text", mappedPayload.get("content").asString)
            }
            val requestId = mappedPayload.get("request_id")?.asString
                ?: msg.get("request_id")?.asString
                ?: fallbackRequestId
                ?: ""
            return JsonObject().apply {
                addProperty("event_type", eventName)
                addProperty("request_id", requestId)
                add("payload", mappedPayload)
            }
        }

        return null
    }

    // ──────────────────────────────────────────
    // Git quick actions
    // ──────────────────────────────────────────
    private fun execGitCommand(vararg args: String): String {
        val base = project.basePath ?: return ""
        return try {
            val proc = ProcessBuilder(*args)
                .directory(File(base))
                .redirectErrorStream(true)
                .start()
            val out = proc.inputStream.bufferedReader().readText().trim()
            proc.waitFor(10, java.util.concurrent.TimeUnit.SECONDS)
            out
        } catch (_: Exception) { "" }
    }

    private fun sendGitStatus() {
        if (!JiuwenSwarmSettings.instance().gitEnabled) return
        ApplicationManager.getApplication().executeOnPooledThread {
            val branch = execGitCommand("git", "rev-parse", "--abbrev-ref", "HEAD")
            if (branch.isEmpty() || branch == "HEAD") return@executeOnPooledThread
            val status = execGitCommand("git", "status", "--porcelain")
            val changedCount = if (status.isEmpty()) 0 else status.lines().count { it.isNotBlank() }
            dispatchToWebview(mapOf("type" to "git_status", "branch" to branch, "changedCount" to changedCount))
        }
    }

    /** Must be called on the EDT (invokeLater) because it shows a dialog. */
    private fun handleGitCommit() {
        val defaultMsg = lastUserInput?.take(72)?.let { "AI: $it" } ?: "AI: agent changes"
        val message = Messages.showInputDialog(
            project, "Commit message (git add -u && git commit):",
            "Git Commit", null, defaultMsg, null,
        ) ?: return
        ApplicationManager.getApplication().executeOnPooledThread {
            execGitCommand("git", "add", "-u")
            val result = execGitCommand("git", "commit", "-m", message)
            if (result.contains("nothing to commit") || result.contains("error") || result.contains("fatal")) {
                dispatchToWebview(mapOf("type" to "git_error", "message" to result.lines().firstOrNull().orEmpty()))
            } else {
                val hash = execGitCommand("git", "rev-parse", "--short", "HEAD")
                dispatchToWebview(mapOf("type" to "git_committed", "hash" to hash))
                sendGitStatus()
            }
        }
    }

    /** Runs on a pooled thread. */
    private fun handleGitPush() {
        val result = execGitCommand("git", "push")
        if (result.contains("error") || result.contains("fatal")) {
            dispatchToWebview(mapOf("type" to "git_error", "message" to result.lines().firstOrNull().orEmpty()))
        } else {
            dispatchToWebview(mapOf("type" to "git_pushed"))
            sendGitStatus()
        }
    }

    private fun sendCurrentStatus() {
        val s = service.ws.getStatus()
        val sid = service.session.sessionId
        debug("STATUS→ ws=$s session=$sid")
        when {
            s == WsStatus.CONNECTED && sid != null -> {
                // Send connected immediately so the webview stops showing "Connecting to JiuwenSwarm…"
                dispatchToWebview(mapOf(
                    "type" to "connected",
                    "sessionId" to sid,
                    "sessionTitle" to service.session.sessionTitle,
                    "defaultMode" to service.settings.defaultMode,
                ))
                // Load history exactly once per session (connect / reconnect / switch).
                if (service.settings.loadHistoryOnSwitch && sid != null && historyLoadedForSession != sid) {
                    historyLoadedForSession = sid
                    dispatchToWebview(mapOf("type" to "history_loading", "loading" to true))
                    ApplicationManager.getApplication().executeOnPooledThread {
                        service.session.loadHistory(sid)
                    }
                }
                // Then fetch models in background and send a second update with model list
                ApplicationManager.getApplication().executeOnPooledThread {
                    try {
                        val (models, activeModel) = service.session.listModels()
                        if (service.session.sessionId != sid) return@executeOnPooledThread
                        val modelList = models.map { m ->
                            mapOf(
                                "model_name" to (m.get("model_name")?.asString ?: ""),
                                "alias" to (m.get("alias")?.asString ?: ""),
                                "model_provider" to (m.get("model_provider")?.asString ?: ""),
                            )
                        }
                        // Look up the current session's history.jsonl path so the
                        // ☰ menu can offer "Copy session path".
                        var historyPath = ""
                        try {
                            historyPath = service.session.listSessions()
                                .firstOrNull { it.session_id == sid }
                                ?.history_path ?: ""
                        } catch (_: Exception) { }
                        dispatchToWebview(mapOf(
                            "type" to "connected",
                            "sessionId" to sid,
                            "sessionTitle" to service.session.sessionTitle,
                            "models" to modelList,
                            "activeModel" to activeModel,
                            "defaultMode" to service.settings.defaultMode,
                            "historyPath" to historyPath,
                        ))
                    } catch (_: Exception) {
                        debug("STATUS→ models.list failed, staying with basic connected state")
                    }
                }
            }
            s == WsStatus.CONNECTED ->
                dispatchToWebview(mapOf(
                    "type" to "connected",
                    "sessionId" to null,
                    "sessionTitle" to "JiuwenSwarm",
                    "needsSession" to true,
                    "defaultMode" to service.settings.defaultMode,
                ))
            s == WsStatus.RECONNECTING ->
                dispatchToWebview(mapOf("type" to "reconnecting"))
            else ->
                dispatchToWebview(mapOf("type" to "disconnected"))
        }
    }

    private fun buildMarkdownFromJsonl(file: File, title: String): String {
        val sb = StringBuilder()
        sb.append("# $title\n\n_Exported from JiuwenSwarm IDE_\n\n---\n\n")
        file.forEachLine { raw ->
            val line = raw.trim()
            if (line.isEmpty()) return@forEachLine
            try {
                val obj = JsonParser.parseString(line).asJsonObject
                val role = obj.get("role")?.asString ?: return@forEachLine
                if (role == "system") return@forEachLine
                val contentEl = obj.get("content") ?: return@forEachLine
                val text = when {
                    contentEl.isJsonPrimitive -> contentEl.asString.trim()
                    contentEl.isJsonArray -> contentEl.asJsonArray.mapNotNull { el ->
                        if (el.isJsonObject) {
                            val o = el.asJsonObject
                            if (o.get("type")?.asString == "text") o.get("text")?.asString else null
                        } else null
                    }.joinToString("\n").trim()
                    else -> return@forEachLine
                }
                if (text.isEmpty()) return@forEachLine
                val heading = if (role == "user") "## User" else "## Assistant"
                sb.append("$heading\n\n$text\n\n---\n\n")
            } catch (_: Exception) { /* skip malformed lines */ }
        }
        return sb.toString()
    }

    // ──────────────────────────────────────────
    override fun dispose() {
        stopMemoryPolling()
        service.ws.removeStatusListener(::onStatusChange)
        service.session.removeSessionListener(::onSessionChange)
        service.ws.removeMessageListener(::onJiuwenMessage)
        jsQuery.dispose()
        browser.dispose()
    }

    private fun fallbackHtml() = """
        <html><body style="background:#1e1e1e;color:#d4d4d4;font-family:sans-serif;padding:16px">
        <p>⚠ Could not load JiuwenSwarm chat UI.<br>
        Ensure <code>resources/webview/chat.html</code> is packaged with the plugin.</p>
        </body></html>
    """.trimIndent()
}

private fun SessionInfo.toMap() = mapOf(
    "session_id" to session_id,
    "title" to (title ?: session_id),
    "last_message_at" to last_message_at,
    "message_count" to message_count,
    "history_path" to (history_path ?: ""),
)
