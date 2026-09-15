package com.jiuwenswarm.plugin.ui

import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.ui.content.ContentFactory
import java.util.concurrent.ConcurrentHashMap

private val LOG = logger<SwarmMapToolWindowFactory>()

class SwarmMapToolWindowFactory : ToolWindowFactory {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        try {
            val panel = SwarmMapPanel(toolWindow)
            panel.onMessage = { raw -> findChatPanel(project)?.handleSwarmMessage(raw) }

            val content = ContentFactory.getInstance().createContent(panel.component, "", false)
            toolWindow.contentManager.addContent(content)

            // Register the panel so ChatPanel can push snapshots to it
            panelByProject[project] = panel

            // Wire the panel to the existing ChatPanel and push current state immediately
            findChatPanel(project)?.let { chatPanel ->
                chatPanel.swarmMapPanel = panel
                val snap = chatPanel.swarmStateManager.snapshot()
                if (!chatPanel.swarmStateManager.isEmpty()) {
                    panel.postSnapshot(snap)
                }
            }
        } catch (e: Exception) {
            LOG.error("Failed to initialise JiuwenSwarm Swarm Map panel", e)
        }
    }

    override fun shouldBeAvailable(project: Project) = true

    companion object {
        // Holds the live panel per project so ChatPanel can reach it before
        // the tool window has been wired via the factory lifecycle.
        private val panelByProject = ConcurrentHashMap<Project, SwarmMapPanel>()

        fun getPanel(project: Project): SwarmMapPanel? = panelByProject[project]

        fun removePanel(project: Project) { panelByProject.remove(project) }

        /**
         * Open (or bring to front) the Swarm Map tool window.
         * Called by ChatPanel when the first team.member.spawned event arrives.
         * createToolWindowContent runs on the EDT via the platform's lazy init —
         * panelByProject will be populated before the next swarm snapshot is pushed.
         */
        fun openOrReveal(project: Project) {
            val tw = ToolWindowManager.getInstance(project)
                .getToolWindow("JiuwenSwarm Swarm") ?: return
            if (!tw.isVisible) tw.show()
        }

        private fun findChatPanel(project: Project): ChatPanel? {
            val chatTw = ToolWindowManager.getInstance(project)
                .getToolWindow("JiuwenSwarm") ?: return null
            return chatTw.contentManager.contents
                .mapNotNull {
                    it.component.getClientProperty("jiuwenswarm.panel") as? ChatPanel
                }
                .firstOrNull()
        }
    }
}
