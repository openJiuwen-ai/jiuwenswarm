package com.jiuwenswarm.plugin.context

import com.intellij.openapi.project.Project
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Collects basic git context for the current project by shelling out to the `git` binary.
 * Using subprocess instead of git4idea APIs keeps the plugin a single-module dependency-free
 * deployment and works across all JetBrains IDEs without declaring optional plugin dependencies.
 *
 * Returns null (silently) when:
 * - The project has no base path
 * - The directory is not inside a git repository
 * - git is not on PATH
 */
object GitContextProvider {

    /**
     * Returns a single-line git summary, e.g.:
     * "Git: branch=feature/foo, 3 uncommitted changes"
     * or null if git context is not available.
     */
    fun collect(project: Project): String? {
        val basePath = project.basePath ?: return null
        return try {
            val branch = runGit(basePath, "git", "rev-parse", "--abbrev-ref", "HEAD")
                .trim()
                .ifBlank { return null }

            // "HEAD" means detached HEAD state — still useful to report
            val statusLines = runGit(basePath, "git", "status", "--porcelain")
                .lines()
                .count { it.isNotBlank() }

            buildString {
                append("Git: branch=$branch")
                when {
                    statusLines == 1 -> append(", 1 uncommitted change")
                    statusLines > 1  -> append(", $statusLines uncommitted changes")
                    else             -> append(", clean")
                }
            }
        } catch (_: Exception) {
            // Not a git repo, git not on PATH, or timed out — suppress silently
            null
        }
    }

    // ──────────────────────────────────────────────────────────────────────────
    private fun runGit(workDir: String, vararg cmd: String): String {
        val process = ProcessBuilder(*cmd)
            .directory(File(workDir))
            .redirectErrorStream(false)
            .start()
        // Discard stderr so error output doesn't bleed into our result
        process.errorStream.use { it.readBytes() }
        val output = process.inputStream.bufferedReader().readText()
        val finished = process.waitFor(5, TimeUnit.SECONDS)
        if (!finished || process.exitValue() != 0) {
            throw RuntimeException("git exited with ${process.exitValue()}")
        }
        return output
    }
}
