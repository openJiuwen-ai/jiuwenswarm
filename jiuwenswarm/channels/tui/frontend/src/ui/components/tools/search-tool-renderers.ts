import type { ToolCallDisplay } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { summarize } from "../../rendering/text.js";
import type { DetailedToolRenderOptions } from "./tool-render-types.js";
import {
  getNumericArg,
  getStringArg,
  getStringList,
  getStringListFromValue,
  isPlainObject,
  nonEmptyLines,
  parseToolResultPayload,
  parseToolResultValue,
  renderPreviewLines,
  renderStructuredBranch,
  renderToolBranch,
  renderToolTail,
  renderToolTitle,
  shouldRenderStructuredPayloadByDefault,
  summarizePath,
  summarizeToolArguments,
  summarizeToolResultByKind,
  toolDisplayName,
  toolStateColor,
} from "./tool-render-shared.js";

export function renderGlobTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const payload = parseToolResultPayload(tool);
  const parsedValue = parseToolResultValue(tool);
  const pattern =
    getStringArg(args, "glob", "pattern", "path", "file_path") ??
    getStringArg(payload ?? {}, "glob", "pattern") ??
    tool.description ??
    tool.name;
  const lines = renderToolTitle(
    width,
    tool,
    `Glob ${summarize(pattern, 120)}`,
    options.animationPhase,
  );
  const root = getStringArg(args, "root", "cwd", "dir_path");
  if (root) {
    lines.push(
      ...renderToolBranch(width, `root: ${summarizePath(root) ?? root}`, palette.text.dim),
    );
  }

  if (tool.result) {
    const payloadMatchLines = getStringList(payload ?? {}, "matching_files", "files");
    const valueMatchLines = getStringListFromValue(parsedValue);
    const matchLines = payloadMatchLines.length > 0 ? payloadMatchLines : valueMatchLines;
    const count = getNumericArg(payload ?? {}, "count") ?? matchLines.length;
    lines.push(
      ...renderToolTail(
        width,
        tool.summary ?? `${count} file${count === 1 ? "" : "s"}`,
        toolStateColor(tool),
      ),
    );
    if (
      !matchLines.length &&
      payload &&
      options.showDetails &&
      shouldRenderStructuredPayloadByDefault(tool.name)
    ) {
      lines.push(
        ...renderStructuredBranch(width, payload, options.showDetails, palette.text.assistant),
      );
    } else if (
      !matchLines.length &&
      parsedValue !== undefined &&
      options.showDetails &&
      shouldRenderStructuredPayloadByDefault(tool.name)
    ) {
      lines.push(
        ...renderStructuredBranch(width, parsedValue, options.showDetails, palette.text.assistant),
      );
    }
  }

  return lines;
}

export function renderSearchTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const payload = parseToolResultPayload(tool);
  const parsedValue = parseToolResultValue(tool);
  const query =
    getStringArg(args, "pattern", "query", "q", "prompt", "glob") ??
    getStringArg(payload ?? {}, "pattern", "query", "q") ??
    tool.description ??
    tool.name;
  const root =
    getStringArg(args, "path", "cwd", "root", "dir_path") ??
    getStringArg(payload ?? {}, "path", "cwd", "root");
  const title = root
    ? `Search ${summarize(query, 96)} in ${summarizePath(root) ?? root}`
    : `Search ${summarize(query, 120)}`;
  const lines = renderToolTitle(width, tool, title, options.animationPhase);

  if (tool.result) {
    const payloadResultLines = getStringList(
      payload ?? {},
      "matches",
      "results",
      "items",
      "hits",
      "files",
    );
    const valueResultLines = getStringListFromValue(parsedValue);
    const resultLines = payloadResultLines.length > 0 ? payloadResultLines : valueResultLines;
    const fallbackLines = nonEmptyLines(tool.result);
    const visibleLines = resultLines.length > 0 ? resultLines : fallbackLines;
    const count =
      getNumericArg(payload ?? {}, "count", "match_count", "matches_count", "total") ??
      visibleLines.length;
    lines.push(
      ...renderToolTail(
        width,
        tool.summary ?? `${count} match${count === 1 ? "" : "es"}`,
        toolStateColor(tool),
      ),
    );
    if (visibleLines.length > 0 && count > visibleLines.length) {
      lines.push(
        ...renderToolBranch(
          width,
          `+ ${count - visibleLines.length} more matches`,
          palette.text.dim,
        ),
      );
    } else if (
      payload &&
      options.showDetails &&
      shouldRenderStructuredPayloadByDefault(tool.name)
    ) {
      lines.push(
        ...renderStructuredBranch(
          width,
          payload,
          options.showDetails,
          tool.isError ? palette.status.error : palette.text.assistant,
        ),
      );
    } else if (
      parsedValue !== undefined &&
      options.showDetails &&
      shouldRenderStructuredPayloadByDefault(tool.name)
    ) {
      lines.push(
        ...renderStructuredBranch(
          width,
          parsedValue,
          options.showDetails,
          tool.isError ? palette.status.error : palette.text.assistant,
        ),
      );
    }
  }

  return lines;
}

export function renderMcpTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const title = toolDisplayName(tool);
  const detail =
    getStringArg(args, "query", "q", "prompt", "url", "path", "file_path", "file") ??
    tool.description ??
    summarizeToolArguments(tool.name, args);
  const lines = renderToolTitle(
    width,
    tool,
    `${title}${detail ? ` · ${summarize(detail, 120)}` : ""}`,
    options.animationPhase,
  );

  if (isPlainObject(tool.arguments)) {
    const argsSummary = summarizeToolArguments(tool.name, tool.arguments);
    if (argsSummary) {
      lines.push(...renderToolTail(width, argsSummary, palette.text.dim));
    }
  }

  if (tool.result) {
    const payload = parseToolResultPayload(tool);
    const parsedValue = parseToolResultValue(tool);
    lines.push(
      ...renderToolTail(
        width,
        tool.summary ??
          summarizeToolResultByKind(tool.name, tool.result) ??
          summarize(tool.result, 120),
        toolStateColor(tool),
      ),
    );
    if (payload && options.showDetails && shouldRenderStructuredPayloadByDefault(tool.name)) {
      lines.push(
        ...renderStructuredBranch(
          width,
          payload,
          options.showDetails,
          tool.isError ? palette.status.error : palette.text.assistant,
        ),
      );
    } else if (
      parsedValue !== undefined &&
      options.showDetails &&
      shouldRenderStructuredPayloadByDefault(tool.name)
    ) {
      lines.push(
        ...renderStructuredBranch(
          width,
          parsedValue,
          options.showDetails,
          tool.isError ? palette.status.error : palette.text.assistant,
        ),
      );
    } else if (options.showDetails && shouldRenderStructuredPayloadByDefault(tool.name)) {
      lines.push(
        ...renderPreviewLines(
          width,
          nonEmptyLines(tool.result),
          tool.isError ? palette.status.error : palette.text.assistant,
          8,
          4,
          options.showDetails,
          "lines",
        ),
      );
    }
  }

  return lines;
}

export function renderGenericTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const title = toolDisplayName(tool);
  const detail = tool.description ?? summarizeToolArguments(tool.name, tool.arguments);
  const separator = title.startsWith("Query ") ? " · " : " ";
  const lines = renderToolTitle(
    width,
    tool,
    `${title}${detail ? `${separator}${detail}` : ""}`,
    options.animationPhase,
  );

  if (isPlainObject(tool.arguments)) {
    lines.push(
      ...renderStructuredBranch(width, tool.arguments, options.showDetails, palette.text.dim),
    );
  }

  if (tool.result) {
    const summary =
      tool.summary ??
      summarizeToolResultByKind(tool.name, tool.result) ??
      summarize(tool.result, 120);
    lines.push(...renderToolTail(width, summary, toolStateColor(tool)));

    const parsedResult = parseToolResultValue(tool);
    if (parsedResult !== tool.result) {
      lines.push(
        ...renderStructuredBranch(
          width,
          parsedResult,
          options.showDetails,
          tool.isError ? palette.status.error : palette.text.assistant,
        ),
      );
    } else if (options.showDetails) {
      const previewLines = tool.result.split("\n").filter(Boolean);
      const shown = previewLines.slice(0, tool.isError ? 2 : 4);
      for (const line of shown) {
        lines.push(
          ...renderToolBranch(
            width,
            line,
            tool.isError ? palette.status.error : palette.text.assistant,
          ),
        );
      }
      if (previewLines.length > shown.length) {
        lines.push(
          ...renderToolBranch(
            width,
            `+ ${previewLines.length - shown.length} more lines`,
            palette.text.dim,
          ),
        );
      }
    }
  }

  return lines;
}
