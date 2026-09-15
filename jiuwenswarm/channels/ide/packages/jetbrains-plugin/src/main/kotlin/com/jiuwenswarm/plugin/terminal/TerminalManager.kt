package com.jiuwenswarm.plugin.terminal

import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindowManager
import com.jiuwenswarm.plugin.settings.JiuwenSwarmSettings

private val LOG = logger<TerminalManager>()

/**
 * Routes bash/run_command tool calls into a JetBrains IDE terminal window.
 * Uses reflection so the plugin still compiles even if the Terminal plugin
 * is not available at build time (it is bundled at runtime).
 */
object TerminalManager {

    private var terminalViewClass: Class<*>? = null
    private var getInstanceMethod: java.lang.reflect.Method? = null
    private var getWidgetsMethod: java.lang.reflect.Method? = null
    private var createWidgetMethod: java.lang.reflect.Method? = null
    private var executeCommandMethod: java.lang.reflect.Method? = null

    init {
        try {
            terminalViewClass = Class.forName("org.jetbrains.plugins.terminal.TerminalView")
            getInstanceMethod = terminalViewClass?.getMethod("getInstance", Project::class.java)
            getWidgetsMethod = terminalViewClass?.getMethod("getWidgets")
            createWidgetMethod = terminalViewClass?.getMethod(
                "createLocalShellWidget",
                String::class.java,
                String::class.java,
            )
            executeCommandMethod = Class.forName("org.jetbrains.plugins.terminal.ShellTerminalWidget")
                ?.getMethod("executeCommand", String::class.java)
        } catch (_: Exception) {
            LOG.info("Terminal plugin reflection init failed — terminal integration disabled")
        }
    }

    fun runCommand(project: Project, command: String) {
        try {
            if (!JiuwenSwarmSettings.instance().runCommandsInTerminal) return
            val tv = terminalViewClass ?: return
            val gi = getInstanceMethod ?: return
            val gw = getWidgetsMethod ?: return
            val cw = createWidgetMethod ?: return
            val ec = executeCommandMethod ?: return

            val terminalView = gi.invoke(null, project)
            val widgets = gw.invoke(terminalView) as? List<*>
            val existing = widgets?.find { w ->
                try {
                    val title = w?.javaClass?.getMethod("getTerminalTitle")?.invoke(w)
                    title?.javaClass?.getMethod("getTitle")?.invoke(title) == "JiuwenSwarm"
                } catch (_: Exception) { false }
            }
            val widget = existing ?: cw.invoke(terminalView, project.basePath, "JiuwenSwarm")
            ec.invoke(widget, command)

            // Bring Terminal tool window to front
            ToolWindowManager.getInstance(project).getToolWindow("Terminal")?.show {}
        } catch (e: Exception) {
            LOG.warn("Failed to run command in terminal: $command", e)
        }
    }

    /** Extract a shell command from a bash / run_command tool-call payload. */
    fun extractCommand(payload: com.google.gson.JsonObject): String? {
        val args = payload.getAsJsonObject("tool_call")?.getAsJsonObject("arguments")
            ?: payload.getAsJsonObject("tool_input")
            ?: payload.getAsJsonObject("input")
            ?: payload
        val cmd = args.get("command")?.asString
            ?: args.get("cmd")?.asString
            ?: args.get("shell_cmd")?.asString
            ?: args.get("bash_command")?.asString
        return cmd?.trim()
    }
}
