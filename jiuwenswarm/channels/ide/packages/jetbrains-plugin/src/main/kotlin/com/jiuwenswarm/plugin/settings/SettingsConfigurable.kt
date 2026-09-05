package com.jiuwenswarm.plugin.settings

import com.intellij.openapi.options.Configurable
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import javax.swing.JComboBox
import javax.swing.JComponent
import javax.swing.JPanel
import javax.swing.JSpinner
import javax.swing.SpinnerNumberModel

class SettingsConfigurable : Configurable {

    private val settings = JiuwenSwarmSettings.instance()

    // ── Connection ──
    private val hostField = JBTextField(settings.host, 20)
    private val portSpinner = JSpinner(SpinnerNumberModel(settings.port, 1, 65535, 1))
    private val channelIdField = JBTextField(settings.channelId, 10)
    private val autoConnectBox = JBCheckBox("Connect automatically on IDE startup", settings.autoConnect)
    private val keepAliveBox = JBCheckBox("Send keep-alive pings to server", settings.keepAliveEnabled)
    private val keepAliveIntervalSpinner = JSpinner(SpinnerNumberModel(settings.keepAliveInterval, 5, 300, 5))

    // ── Chat behaviour ──
    private val defaultModeCombo = JComboBox(arrayOf("Plan & Execute", "Execute", "Team Coding")).also {
        it.selectedItem = modeLabel(settings.defaultMode)
    }
    private val loadHistoryOnSwitchBox = JBCheckBox(
        "Load message history when switching to an existing session",
        settings.loadHistoryOnSwitch,
    )

    // ── File edits ──
    private val approveEditsBox = JBCheckBox(
        "Require approval before applying agent file edits",
        settings.approveEdits,
    )
    private val autoApplyEditsBox = JBCheckBox(
        "Auto-apply file edits (skip diff dialog)",
        settings.autoApplyEdits,
    )
    private val runInTerminalBox = JBCheckBox(
        "Run bash / shell commands in IDE terminal",
        settings.runCommandsInTerminal,
    )
    private val rewindEnabledBox = JBCheckBox(
        "Enable checkpoint / rewind (snapshot files before agent edits)",
        settings.rewindEnabled,
    )
    private val gitEnabledBox = JBCheckBox(
        "Enable Git Commit / Push quick actions",
        settings.gitEnabled,
    )

    // ── Context injection ──
    private val projectTreeEnabledBox = JBCheckBox(
        "Inject project directory tree into each message",
        settings.projectTreeEnabled,
    )
    private val projectTreeMaxFilesSpinner = JSpinner(
        SpinnerNumberModel(settings.projectTreeMaxFiles, 10, 2000, 50)
    )

    private var panel: JPanel? = null

    private fun modeLabel(code: String): String = when (code) {
        "code.normal" -> "Execute"
        "code.team" -> "Team Coding"
        else -> "Plan & Execute"
    }

    private fun modeCode(label: String): String = when (label) {
        "Execute" -> "code.normal"
        "Team Coding" -> "code.team"
        else -> "code.plan"
    }

    override fun getDisplayName() = "JiuwenSwarm"

    override fun createComponent(): JComponent {
        panel = FormBuilder.createFormBuilder()
            // Connection
            .addSeparator()
            .addLabeledComponent(JBLabel("Server host:"), hostField, 1, false)
            .addLabeledComponent(JBLabel("Server port:"), portSpinner, 1, false)
            .addLabeledComponent(JBLabel("Channel ID:"), channelIdField, 1, false)
            .addComponent(autoConnectBox, 1)
            .addComponent(keepAliveBox, 1)
            .addLabeledComponent(JBLabel("Keep-alive interval (seconds):"), keepAliveIntervalSpinner, 1, false)
            // Chat behaviour
            .addSeparator()
            .addLabeledComponent(JBLabel("Default agent mode:"), defaultModeCombo, 1, false)
            .addComponent(loadHistoryOnSwitchBox, 1)
            // File edits
            .addSeparator()
            .addComponent(approveEditsBox, 1)
            .addComponent(autoApplyEditsBox, 1)
            .addComponent(runInTerminalBox, 1)
            .addComponent(rewindEnabledBox, 1)
            .addComponent(gitEnabledBox, 1)
            // Context injection
            .addSeparator()
            .addComponent(projectTreeEnabledBox, 1)
            .addLabeledComponent(JBLabel("Project tree max files:"), projectTreeMaxFilesSpinner, 1, false)
            .addComponentFillVertically(JPanel(), 0)
            .panel
        return panel!!
    }

    override fun isModified(): Boolean =
        hostField.text != settings.host ||
        (portSpinner.value as Int) != settings.port ||
        channelIdField.text != settings.channelId ||
        autoConnectBox.isSelected != settings.autoConnect ||
        keepAliveBox.isSelected != settings.keepAliveEnabled ||
        (keepAliveIntervalSpinner.value as Int) != settings.keepAliveInterval ||
        modeCode(defaultModeCombo.selectedItem as String) != settings.defaultMode ||
        loadHistoryOnSwitchBox.isSelected != settings.loadHistoryOnSwitch ||
        approveEditsBox.isSelected != settings.approveEdits ||
        autoApplyEditsBox.isSelected != settings.autoApplyEdits ||
        runInTerminalBox.isSelected != settings.runCommandsInTerminal ||
        rewindEnabledBox.isSelected != settings.rewindEnabled ||
        gitEnabledBox.isSelected != settings.gitEnabled ||
        projectTreeEnabledBox.isSelected != settings.projectTreeEnabled ||
        (projectTreeMaxFilesSpinner.value as Int) != settings.projectTreeMaxFiles

    override fun apply() {
        settings.host = hostField.text.trim()
        settings.port = portSpinner.value as Int
        settings.channelId = channelIdField.text.trim()
        settings.autoConnect = autoConnectBox.isSelected
        settings.keepAliveEnabled = keepAliveBox.isSelected
        settings.keepAliveInterval = (keepAliveIntervalSpinner.value as Int).coerceIn(5, 300)
        settings.defaultMode = modeCode(defaultModeCombo.selectedItem as String)
        settings.loadHistoryOnSwitch = loadHistoryOnSwitchBox.isSelected
        settings.approveEdits = approveEditsBox.isSelected
        settings.autoApplyEdits = autoApplyEditsBox.isSelected
        settings.runCommandsInTerminal = runInTerminalBox.isSelected
        settings.rewindEnabled = rewindEnabledBox.isSelected
        settings.gitEnabled = gitEnabledBox.isSelected
        settings.projectTreeEnabled = projectTreeEnabledBox.isSelected
        settings.projectTreeMaxFiles = (projectTreeMaxFilesSpinner.value as Int).coerceIn(10, 2000)
    }

    override fun reset() {
        hostField.text = settings.host
        portSpinner.value = settings.port
        channelIdField.text = settings.channelId
        autoConnectBox.isSelected = settings.autoConnect
        keepAliveBox.isSelected = settings.keepAliveEnabled
        keepAliveIntervalSpinner.value = settings.keepAliveInterval
        defaultModeCombo.selectedItem = modeLabel(settings.defaultMode)
        loadHistoryOnSwitchBox.isSelected = settings.loadHistoryOnSwitch
        approveEditsBox.isSelected = settings.approveEdits
        autoApplyEditsBox.isSelected = settings.autoApplyEdits
        runInTerminalBox.isSelected = settings.runCommandsInTerminal
        rewindEnabledBox.isSelected = settings.rewindEnabled
        gitEnabledBox.isSelected = settings.gitEnabled
        projectTreeEnabledBox.isSelected = settings.projectTreeEnabled
        projectTreeMaxFilesSpinner.value = settings.projectTreeMaxFiles
    }

    override fun disposeUIResources() {
        panel = null
    }
}
