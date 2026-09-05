package com.jiuwenswarm.plugin.settings

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage

@State(
    name = "JiuwenSwarmSettings",
    storages = [Storage("jiuwenswarm.xml")]
)
class JiuwenSwarmSettings : PersistentStateComponent<JiuwenSwarmSettings.State> {

    data class State(
        var host: String = "127.0.0.1",
        var port: Int = 19000,
        var channelId: String = "ide",
        var defaultMode: String = "code.plan",
        var autoConnect: Boolean = true,
        /** When true, file edits from the agent are applied directly without a diff dialog. */
        var autoApplyEdits: Boolean = false,
        /** When true, show an approval dialog before applying any agent file edit. */
        var approveEdits: Boolean = false,
        /** When true, bash/run_command tool calls are shown in an IDE terminal. */
        var runCommandsInTerminal: Boolean = true,
        /** When true, send periodic WebSocket ping frames to keep the connection alive. */
        var keepAliveEnabled: Boolean = true,
        /** Seconds between keep-alive ping frames (5–300). */
        var keepAliveInterval: Int = 30,
        /** When true, inject the project directory tree into each chat message. */
        var projectTreeEnabled: Boolean = true,
        /** Maximum number of files listed in the injected project tree (10–2000). */
        var projectTreeMaxFiles: Int = 200,
        /** When true, load and display message history after switching to an existing session. */
        var loadHistoryOnSwitch: Boolean = true,
        /** When true, snapshot files before agent edits so the rewind feature can restore them. */
        var rewindEnabled: Boolean = true,
        /** When true, show the Commit / Push quick actions in the chat panel. */
        var gitEnabled: Boolean = false,
    )

    private var state = State()

    override fun getState(): State = state

    override fun loadState(state: State) {
        this.state = state
    }

    var host: String
        get() = state.host
        set(v) { state = state.copy(host = v) }

    var port: Int
        get() = state.port
        set(v) { state = state.copy(port = v) }

    var channelId: String
        get() = state.channelId
        set(v) { state = state.copy(channelId = v) }

    var defaultMode: String
        get() = state.defaultMode
        set(v) { state = state.copy(defaultMode = v) }

    var autoConnect: Boolean
        get() = state.autoConnect
        set(v) { state = state.copy(autoConnect = v) }

    var autoApplyEdits: Boolean
        get() = state.autoApplyEdits
        set(v) { state = state.copy(autoApplyEdits = v) }

    var approveEdits: Boolean
        get() = state.approveEdits
        set(v) { state = state.copy(approveEdits = v) }

    var runCommandsInTerminal: Boolean
        get() = state.runCommandsInTerminal
        set(v) { state = state.copy(runCommandsInTerminal = v) }

    var keepAliveEnabled: Boolean
        get() = state.keepAliveEnabled
        set(v) { state = state.copy(keepAliveEnabled = v) }

    var keepAliveInterval: Int
        get() = state.keepAliveInterval
        set(v) { state = state.copy(keepAliveInterval = v.coerceIn(5, 300)) }

    var projectTreeEnabled: Boolean
        get() = state.projectTreeEnabled
        set(v) { state = state.copy(projectTreeEnabled = v) }

    var projectTreeMaxFiles: Int
        get() = state.projectTreeMaxFiles
        set(v) { state = state.copy(projectTreeMaxFiles = v.coerceIn(10, 2000)) }

    var loadHistoryOnSwitch: Boolean
        get() = state.loadHistoryOnSwitch
        set(v) { state = state.copy(loadHistoryOnSwitch = v) }

    var rewindEnabled: Boolean
        get() = state.rewindEnabled
        set(v) { state = state.copy(rewindEnabled = v) }

    var gitEnabled: Boolean
        get() = state.gitEnabled
        set(v) { state = state.copy(gitEnabled = v) }

    val wsUrl: String get() = "ws://$host:$port/ws"

    companion object {
        fun instance(): JiuwenSwarmSettings =
            ApplicationManager.getApplication().getService(JiuwenSwarmSettings::class.java)
    }
}
