package com.jiuwenswarm.plugin.ui

import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.wm.ToolWindowManager
import com.jiuwenswarm.plugin.JiuwenSwarmService

/**
 * Cmd+Shift+J / Ctrl+Shift+J — show the JiuwenSwarm panel and start a fresh session
 * by reconnecting the WebSocket (same as clicking the "New" button in the panel).
 */
class NewSessionAction : AnAction() {
    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        ApplicationManager.getApplication().invokeLater {
            val tw = ToolWindowManager.getInstance(project).getToolWindow("JiuwenSwarm")
                ?: return@invokeLater
            // show(Runnable) is called after the tool window is fully visible and
            // createToolWindowContent has completed, so the reconnect fires at the right time.
            tw.show {
                ApplicationManager.getApplication().executeOnPooledThread {
                    try {
                        JiuwenSwarmService.instance().ws.reconnect()
                    } catch (_: Exception) {}
                }
            }
        }
    }

    override fun update(e: AnActionEvent) {
        // Always enabled — show the panel even when not connected yet
        e.presentation.isEnabled = e.project != null
    }
}

/**
 * Cmd+Shift+E / Ctrl+Shift+E — send the current editor selection to the JiuwenSwarm chat.
 *
 * The selection is formatted as a fenced code block and pre-filled into the input field.
 * Uses [ToolWindow.show(Runnable)] to ensure [createToolWindowContent] has finished
 * (it is called lazily on first open) before attempting to dispatch to the panel.
 */
class SendSelectionAction : AnAction() {
    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val editor = e.getData(CommonDataKeys.EDITOR) ?: return
        val selection = editor.selectionModel.selectedText ?: return
        val fileName = e.getData(CommonDataKeys.VIRTUAL_FILE)?.name ?: "file"

        val prefillContent = "[File: $fileName]\n```\n$selection\n```\n\n"

        ApplicationManager.getApplication().invokeLater {
            val tw = ToolWindowManager.getInstance(project).getToolWindow("JiuwenSwarm")
                ?: return@invokeLater

            // show(Runnable): the runnable fires once the window is visible and
            // its content is initialised, so getContent(0) is guaranteed non-null.
            tw.show {
                val comp = tw.contentManager.getContent(0)?.component
                (comp?.getClientProperty("jiuwenswarm.panel") as? ChatPanel)
                    ?.dispatchToWebview(mapOf("type" to "prefill", "content" to prefillContent))
            }
        }
    }

    override fun update(e: AnActionEvent) {
        val editor = e.getData(CommonDataKeys.EDITOR)
        e.presentation.isEnabled = editor?.selectionModel?.hasSelection() == true
    }
}
