package com.jiuwenswarm.plugin.editor

import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.intellij.diff.DiffContentFactory
import com.intellij.diff.DiffDialogHints
import com.intellij.diff.DiffManager
import com.intellij.diff.requests.SimpleDiffRequest
import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.command.WriteCommandAction
import com.intellij.openapi.diagnostic.logger
import com.intellij.openapi.fileEditor.FileDocumentManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.util.Ref
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VfsUtil
import com.intellij.openapi.vfs.VirtualFile
import com.jiuwenswarm.plugin.settings.JiuwenSwarmSettings
import java.io.File

private val LOG = logger<DiffApplier>()

/**
 * Parses `chat.tool_call` WebSocket events for file-editing tools and either
 * shows a side-by-side diff dialog (default) or silently applies the change
 * when "auto-apply edits" is enabled in settings.
 *
 * Supported tools:
 *  - `str_replace_editor` (command = str_replace | create)
 *  - `edit_file` (file_path + old_string/new_string)
 *  - `write_file`
 *  - `create_file`
 */
object DiffApplier {

    /** Callable from any thread. Returns true if the event was handled.
     *  Accepts either a raw gateway message (tool_name inside payload) or
     *  a flattened event (tool_name at root).
     */
    fun handle(project: Project, event: JsonObject): Boolean {
        // All UI work (approval dialogs, VFS/document access, diff dialog, write
        // commands) must run on the EDT. If a caller dispatched us from a pooled
        // thread (or the wire path changed), hop to the EDT instead of crashing.
        if (!ApplicationManager.getApplication().isDispatchThread) {
            val result = Ref<Boolean>(false)
            ApplicationManager.getApplication().invokeAndWait {
                result.set(handleOnEdt(project, event))
            }
            return result.get()
        }
        return handleOnEdt(project, event)
    }

    private fun handleOnEdt(project: Project, event: JsonObject): Boolean {
        val toolName = event.get("tool_name")?.asString
            ?: event.getAsJsonObject("payload")?.get("tool_name")?.asString
            ?: event.getAsJsonObject("payload")?.getAsJsonObject("tool_call")?.get("name")?.asString
            ?: return false
        if (toolName !in EDIT_TOOLS) return false

        val args = parseArguments(event) ?: run {
            LOG.warn("DiffApplier: could not parse arguments for $toolName")
            return false
        }

        val settings = JiuwenSwarmSettings.instance()
        val autoApply = settings.autoApplyEdits
        val requireApproval = settings.approveEdits
        LOG.info("DiffApplier: handling $toolName autoApply=$autoApply args=$args")

        return when (toolName) {
            "str_replace_editor" -> handleStrReplaceEditor(project, args, autoApply, requireApproval)
            "edit_file" -> handleEditFile(project, args, autoApply, requireApproval)
            "write_file", "create_file" -> handleWriteFile(project, args, autoApply, requireApproval)
            else -> false
        }
    }

    // ──────────────────────────────────────────────────────────────────────────
    // edit_file (ACP-style: file_path + old_string/new_string)
    // ──────────────────────────────────────────────────────────────────────────

    private fun handleEditFile(
        project: Project,
        args: JsonObject,
        autoApply: Boolean,
        requireApproval: Boolean,
    ): Boolean {
        val path = args.get("file_path")?.asString
            ?: args.get("path")?.asString
            ?: return false
        val oldStr = args.get("old_string")?.asString
            ?: args.get("old_str")?.asString
            ?: return false
        val newStr = args.get("new_string")?.asString
            ?: args.get("new_str")?.asString
            ?: ""
        if (requireApproval && !askApproval(project, "Apply edit to ${File(path).name}?")) {
            notify(project, "Edit rejected: ${File(path).name}")
            return false
        }
        return applyStrReplace(project, path, oldStr, newStr, autoApply)
    }

    // ──────────────────────────────────────────────────────────────────────────
    // str_replace_editor
    // ──────────────────────────────────────────────────────────────────────────

    private fun handleStrReplaceEditor(
        project: Project,
        args: JsonObject,
        autoApply: Boolean,
        requireApproval: Boolean,
    ): Boolean {
        val command = args.get("command")?.asString ?: "str_replace"
        val path = args.get("path")?.asString ?: return false

        return when (command) {
            "str_replace" -> {
                val oldStr = args.get("old_str")?.asString ?: return false
                val newStr = args.get("new_str")?.asString ?: ""
                if (requireApproval && !askApproval(project, "Apply edit to ${File(path).name}?")) {
                    notify(project, "Edit rejected: ${File(path).name}")
                    return false
                }
                applyStrReplace(project, path, oldStr, newStr, autoApply)
            }
            "create" -> {
                val content = args.get("file_text")?.asString
                    ?: args.get("content")?.asString
                    ?: ""
                if (requireApproval && !askApproval(project, "Create file ${File(path).name}?")) {
                    notify(project, "Create rejected: ${File(path).name}")
                    return false
                }
                writeEntireFile(project, path, content, autoApply, isNew = true)
            }
            else -> false
        }
    }

    // ──────────────────────────────────────────────────────────────────────────
    // write_file / create_file
    // ──────────────────────────────────────────────────────────────────────────

    private fun handleWriteFile(
        project: Project,
        args: JsonObject,
        autoApply: Boolean,
        requireApproval: Boolean,
    ): Boolean {
        val path = args.get("path")?.asString ?: return false
        val content = args.get("content")?.asString ?: ""
        val isNew = !File(path).exists()
        val label = if (isNew) "Create" else "Overwrite"
        if (requireApproval && !askApproval(project, "$label file ${File(path).name}?")) {
            notify(project, "$label rejected: ${File(path).name}")
            return false
        }
        return writeEntireFile(project, path, content, autoApply, isNew)
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Approval dialog
    // ──────────────────────────────────────────────────────────────────────────

    private fun askApproval(project: Project, message: String): Boolean {
        return Messages.showYesNoDialog(
            project,
            message,
            "JiuwenSwarm — File Edit",
            Messages.getQuestionIcon(),
        ) == Messages.YES
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Core operations
    // ──────────────────────────────────────────────────────────────────────────

    private fun applyStrReplace(
        project: Project,
        path: String,
        oldStr: String,
        newStr: String,
        autoApply: Boolean,
    ): Boolean {
        val vf = LocalFileSystem.getInstance().refreshAndFindFileByPath(path) ?: run {
            notify(project, "File not found: $path", error = true)
            return false
        }
        val document = FileDocumentManager.getInstance().getDocument(vf) ?: return false
        val originalText = document.text

        val idx = originalText.indexOf(oldStr)
        if (idx < 0) {
            notify(project, "Could not locate the target text in $path", error = true)
            return false
        }

        val proposed = originalText.substring(0, idx) + newStr + originalText.substring(idx + oldStr.length)

        if (autoApply) {
            WriteCommandAction.runWriteCommandAction(project, "JiuwenSwarm edit: $path", null, {
                document.replaceString(idx, idx + oldStr.length, newStr)
            })
            notify(project, "Applied edit to ${vf.name}")
        } else {
            showDiff(project, vf.name, originalText, proposed)
        }
        return true
    }

    private fun writeEntireFile(
        project: Project,
        path: String,
        content: String,
        autoApply: Boolean,
        isNew: Boolean,
    ): Boolean {
        val label = if (isNew) "Create" else "Overwrite"

        if (autoApply) {
            ApplicationManager.getApplication().invokeLater {
                WriteCommandAction.runWriteCommandAction(project, "JiuwenSwarm $label: $path", null, {
                    val file = File(path)
                    if (!file.parentFile.exists()) file.parentFile.mkdirs()
                    val vf: VirtualFile = if (isNew || !file.exists()) {
                        VfsUtil.createDirectories(file.parent)
                        val parentVf = LocalFileSystem.getInstance()
                            .refreshAndFindFileByPath(file.parent)!!
                        parentVf.createChildData(this, file.name)
                    } else {
                        LocalFileSystem.getInstance().refreshAndFindFileByPath(path)!!
                    }
                    VfsUtil.saveText(vf, content)
                })
                notify(project, "$label applied: $path")
            }
        } else {
            val originalText = if (isNew) "" else runCatching {
                LocalFileSystem.getInstance().findFileByPath(path)
                    ?.let { FileDocumentManager.getInstance().getDocument(it) }
                    ?.text ?: ""
            }.getOrElse { "" }

            showDiff(project, File(path).name, originalText, content)
        }
        return true
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Diff dialog
    // ──────────────────────────────────────────────────────────────────────────

    private fun showDiff(
        project: Project,
        title: String,
        originalText: String,
        proposedText: String,
    ) {
        ApplicationManager.getApplication().invokeLater {
            val factory = DiffContentFactory.getInstance()
            val left = factory.create(project, originalText)
            val right = factory.create(project, proposedText)
            val request = SimpleDiffRequest(
                "JiuwenSwarm — Proposed Edit: $title",
                left, right,
                "Current", "Proposed",
            )
            DiffManager.getInstance().showDiff(project, request, DiffDialogHints.FRAME)
        }
    }

    // ──────────────────────────────────────────────────────────────────────────
    // Helpers
    // ──────────────────────────────────────────────────────────────────────────

    private fun parseArguments(event: JsonObject): JsonObject? {
        val toolCall = event.getAsJsonObject("tool_call")
            ?: event.getAsJsonObject("payload")?.getAsJsonObject("tool_call")
            ?: return null
        val argsEl: JsonElement = toolCall.get("arguments") ?: return null
        return when {
            argsEl.isJsonObject -> argsEl.asJsonObject
            argsEl.isJsonPrimitive -> runCatching {
                JsonParser.parseString(argsEl.asString).asJsonObject
            }.getOrNull()
            else -> null
        }
    }

    private fun notify(project: Project, message: String, error: Boolean = false) {
        ApplicationManager.getApplication().invokeLater {
            val type = if (error) NotificationType.ERROR else NotificationType.INFORMATION
            NotificationGroupManager.getInstance()
                .getNotificationGroup("JiuwenSwarm")
                .createNotification("JiuwenSwarm", message, type)
                .notify(project)
        }
    }

    private val EDIT_TOOLS = setOf("str_replace_editor", "write_file", "create_file", "edit_file")
}
