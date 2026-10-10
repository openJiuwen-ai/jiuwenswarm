package com.jiuwenswarm.plugin.ui

import com.intellij.codeInsight.intention.IntentionAction
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.editor.Editor
import com.intellij.openapi.editor.impl.DocumentMarkupModel
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.TextRange
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.psi.PsiFile
import com.intellij.util.IncorrectOperationException

/**
 * Alt+Enter intention action: "Fix with JiuwenSwarm"
 *
 * Appears in the quick-fix menu whenever there is a highlighted error or warning
 * under the cursor.  When invoked it opens the JiuwenSwarm tool window and pre-fills
 * the chat input with the error message + surrounding code so the user just has to
 * press Enter.
 */
class FixWithAiIntention : IntentionAction {

    override fun getText()       = "Fix with JiuwenSwarm"
    override fun getFamilyName() = "JiuwenSwarm"
    override fun startInWriteAction() = false

    // ── Availability ──────────────────────────────────────────────────────────

    override fun isAvailable(project: Project, editor: Editor?, file: PsiFile?): Boolean {
        if (editor == null) return false
        val offset = editor.caretModel.offset
        val markupModel = DocumentMarkupModel.forDocument(editor.document, project, false)
            ?: return false
        return markupModel.allHighlighters.any { h ->
            h.errorStripeTooltip != null &&
            h.startOffset <= offset && offset <= h.endOffset.coerceAtLeast(h.startOffset)
        }
    }

    // ── Invocation ────────────────────────────────────────────────────────────

    @Throws(IncorrectOperationException::class)
    override fun invoke(project: Project, editor: Editor?, file: PsiFile?) {
        if (editor == null || file == null) return

        val document = editor.document
        val offset   = editor.caretModel.offset
        val caretLine = document.getLineNumber(offset)

        // Collect all error tooltips whose range covers the caret
        val markupModel = DocumentMarkupModel.forDocument(document, project, false)
        val errorText = markupModel?.allHighlighters
            ?.filter { h ->
                h.errorStripeTooltip != null &&
                h.startOffset <= offset && offset <= h.endOffset.coerceAtLeast(h.startOffset)
            }
            ?.mapNotNull { h ->
                h.errorStripeTooltip?.toString()
                    ?.replace(Regex("<[^>]+>"), "")   // strip HTML tags
                    ?.trim()
                    ?.ifBlank { null }
            }
            ?.distinct()
            ?.joinToString("\n")
            ?.ifBlank { null }
            ?: "error at cursor position"

        // Grab ±7 lines of context around the error
        val startLine   = maxOf(0, caretLine - 7)
        val endLine     = minOf(document.lineCount - 1, caretLine + 7)
        val codeSnippet = document.getText(
            TextRange(document.getLineStartOffset(startLine), document.getLineEndOffset(endLine))
        )

        val lang = file.fileType.defaultExtension.ifBlank { "code" }
        val prefill = buildString {
            appendLine("Fix this error in ${file.name}:")
            appendLine()
            appendLine("Error:")
            appendLine(errorText)
            appendLine()
            appendLine("```$lang")
            appendLine(codeSnippet.trimEnd())
            appendLine("```")
            appendLine()
        }

        ApplicationManager.getApplication().invokeLater {
            val tw = ToolWindowManager.getInstance(project).getToolWindow("JiuwenSwarm")
                ?: return@invokeLater
            tw.show {
                val comp = tw.contentManager.getContent(0)?.component
                (comp?.getClientProperty("jiuwenswarm.panel") as? ChatPanel)
                    ?.dispatchToWebview(mapOf("type" to "prefill", "content" to prefill))
            }
        }
    }
}
