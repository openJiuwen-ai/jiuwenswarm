import { writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { copyToClipboard } from "../clipboard.js";
import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { HistoryItem, ToolCallDisplay } from "../../types.js";

/**
 * Ensure the filename (last path component) ends with .txt.
 * Only the basename is modified — path prefixes like "../" or "subdir/" are preserved.
 */
function ensureTxtExtension(inputPath: string): string {
  const dir = dirname(inputPath);
  const base = basename(inputPath);
  const finalBase = base.endsWith(".txt") ? base : base.replace(/\.[^.]+$/, "") + ".txt";
  return dir === "." ? finalBase : join(dir, finalBase);
}

function formatTimestamp(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${year}-${month}-${day}-${hours}${minutes}${seconds}`;
}

function extractFirstPrompt(entries: HistoryItem[]): string {
  const firstUserEntry = entries.find((e) => e.kind === "user");
  if (!firstUserEntry || firstUserEntry.kind !== "user") return "";
  const text = firstUserEntry.content.trim().split("\n")[0] || "";
  return text.length > 50 ? text.substring(0, 49) + "…" : text;
}

function sanitizeFilename(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s一-鿿-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

function renderEntriesToPlainText(entries: HistoryItem[]): string {
  const lines: string[] = [];

  for (const entry of entries) {
    const time = entry.at ? new Date(entry.at).toLocaleString() : "";

    switch (entry.kind) {
      case "user":
        lines.push(`[User] ${time}`);
        lines.push(entry.content);
        lines.push("");
        break;

      case "assistant":
        lines.push(`[Assistant] ${time}`);
        lines.push(entry.content);
        lines.push("");
        break;

      case "thinking":
        lines.push(`[Thinking] ${time}`);
        lines.push(entry.content);
        lines.push("");
        break;

      case "tool_group": {
        const toolNames = entry.tools.map((t: ToolCallDisplay) => t.name).join(", ");
        lines.push(`[Tools] ${time} — ${toolNames}`);
        for (const tool of entry.tools) {
          lines.push(`  ${tool.name}${tool.summary ? `: ${tool.summary}` : ""}`);
          if (tool.result) {
            const truncated =
              tool.result.length > 500 ? tool.result.substring(0, 499) + "…" : tool.result;
            lines.push(`  Result: ${truncated}`);
          }
        }
        lines.push("");
        break;
      }

      case "collapsed_tool_group": {
        const hint = entry.latestHint || "";
        const counts = entry.counts;
        const countStr = Object.entries(counts)
          .filter(([, v]) => v > 0)
          .map(([k, v]) => `${v} ${k}`)
          .join(", ");
        lines.push(`[Tools] ${time} — ${hint || countStr}`);
        lines.push("");
        break;
      }

      case "system":
        lines.push(`[System] ${time}`);
        lines.push(entry.content);
        lines.push("");
        break;

      case "error":
        lines.push(`[Error] ${time}`);
        lines.push(entry.content);
        lines.push("");
        break;

      case "info":
        lines.push(`[Info] ${time}`);
        lines.push(entry.content);
        lines.push("");
        break;

      case "diff":
        lines.push(`[Diff] ${time}`);
        for (const turn of entry.meta.turns) {
          lines.push(`  Turn ${turn.turnIndex}: ${turn.userPromptPreview}`);
          for (const [, file] of Object.entries(turn.files)) {
            lines.push(
              `    ${file.filePath} (+${file.linesAdded}/-${file.linesRemoved}${file.isNewFile ? " [new]" : ""})`,
            );
          }
        }
        lines.push("");
        break;
    }
  }

  return lines.join("\n");
}

export function createExportCommand(): SlashCommand {
  return {
    name: "export",
    description: "Export current conversation to a file or clipboard",
    usage: "/export [filename]",
    example: "/export my-chat.txt",
    kind: CommandKind.BUILT_IN,
    hidden: false,
    takesArgs: true,
    action: async (ctx, args) => {
      const content = renderEntriesToPlainText(ctx.entries);
      const arg = args.trim();

      // Direct export when filename is provided — skip dialog
      if (arg) {
        const filename = ensureTxtExtension(arg);
        const workspaceDir = ctx.getWorkspaceDir() || process.cwd();
        const filepath = resolve(workspaceDir, filename);

        try {
          writeFileSync(filepath, content, { encoding: "utf-8" });
          ctx.addItem(addInfo(ctx.sessionId, `Conversation exported to: ${filepath}`, "e"));
        } catch (error) {
          const message = error instanceof Error ? error.message : "Unknown error";
          ctx.addItem(addError(ctx.sessionId, `Failed to export: ${message}`));
        }
        return;
      }

      // No args — show interactive dialog (two-step: method → filename)
      const firstPrompt = extractFirstPrompt(ctx.entries);
      const timestamp = formatTimestamp(new Date());
      const sanitized = firstPrompt ? sanitizeFilename(firstPrompt) : "";
      const defaultFilename = sanitized
        ? `${timestamp}-${sanitized}.txt`
        : `conversation-${timestamp}.txt`;
      const workspaceDir = ctx.getWorkspaceDir() || process.cwd();
      const defaultFilepath = resolve(workspaceDir, defaultFilename);

      // Step 1: select export method
      try {
        const [methodAnswer] = await ctx.askQuestions(
          [
            {
              header: "Export",
              question: "How would you like to export the conversation?",
              options: [
                {
                  label: "Copy to clipboard",
                  description: "Copy the conversation content to your clipboard",
                },
                {
                  label: "Save to file",
                  description: `Save to ${defaultFilepath}`,
                },
              ],
            },
          ],
          "command_export_method",
        );

        const method = methodAnswer?.selected_options?.[0];
        if (!method) {
          ctx.addItem(addInfo(ctx.sessionId, "Export cancelled", "e"));
          return;
        }

        if (method === "Copy to clipboard") {
          const ok = await copyToClipboard(content);
          if (!ok) {
            ctx.addItem(
              addError(ctx.sessionId, "Clipboard unavailable — try saving to file instead"),
            );
            return;
          }
          ctx.addItem(addInfo(ctx.sessionId, "Conversation copied to clipboard", "e"));
          return;
        }

        // Step 2: select/edit filename
        const [filenameAnswer] = await ctx.askQuestions(
          [
            {
              header: "Filename",
              question: `Enter filename (default: ${defaultFilename}):`,
              options: [
                {
                  label: defaultFilename,
                  description: `Save as ${defaultFilepath}`,
                },
                {
                  label: "Other",
                  description: "Enter a custom filename",
                },
              ],
            },
          ],
          "command_export_filename",
        );

        if (!filenameAnswer?.selected_options?.[0]) {
          ctx.addItem(addInfo(ctx.sessionId, "Export cancelled", "e"));
          return;
        }

        let chosenFilename: string;
        if (filenameAnswer.selected_options[0] === "Other" && filenameAnswer.custom_input) {
          chosenFilename = filenameAnswer.custom_input.trim();
        } else {
          chosenFilename = filenameAnswer.selected_options[0];
        }

        chosenFilename = ensureTxtExtension(chosenFilename);
        const chosenFilepath = resolve(workspaceDir, chosenFilename);

        try {
          writeFileSync(chosenFilepath, content, { encoding: "utf-8" });
          ctx.addItem(addInfo(ctx.sessionId, `Conversation exported to: ${chosenFilepath}`, "e"));
        } catch (error) {
          const message = error instanceof Error ? error.message : "Unknown error";
          ctx.addItem(addError(ctx.sessionId, `Failed to export: ${message}`));
        }
      } catch (err) {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            `Export cancelled: ${err instanceof Error ? err.message : String(err)}`,
            "e",
          ),
        );
      }
    },
  };
}