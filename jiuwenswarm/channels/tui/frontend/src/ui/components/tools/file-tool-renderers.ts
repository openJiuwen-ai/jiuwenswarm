import type { ToolCallDisplay } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { summarize } from "../../rendering/text.js";
import type { DetailedToolRenderOptions } from "./tool-render-types.js";
import {
  TOOL_EXPAND_HINT,
  countLogicalLines,
  extractTrailingBracketNotices,
  getNumericArg,
  getStringArg,
  getStringList,
  getToolFilePath,
  isCodeLikePath,
  isPlainObject,
  parseToolResultPayload,
  renderAddedLines,
  renderSimpleDiff,
  renderToolBranch,
  renderToolTail,
  renderToolTitle,
  summarizeToolResultByKind,
  toolStateColor,
} from "./tool-render-shared.js";

export function renderSessionTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const description =
    typeof args.description === "string" && args.description
      ? args.description
      : (tool.description ?? "session task");
  const index = typeof args.index === "number" ? args.index : undefined;
  const total = typeof args.total === "number" ? args.total : undefined;
  const label =
    index !== undefined && total !== undefined
      ? `Run session ${index}/${total} · ${description}`
      : `Run session · ${description}`;
  const lines = renderToolTitle(width, tool, label, options.animationPhase);

  if (tool.result) {
    const previewLines = tool.result.split("\n").filter(Boolean);
    lines.push(
      ...renderToolTail(
        width,
        tool.summary ??
          summarizeToolResultByKind(tool.name, tool.result) ??
          summarize(tool.result, 120),
        toolStateColor(tool),
      ),
    );
    if (options.showDetails) {
      for (const line of previewLines.slice(0, 6)) {
        lines.push(...renderToolBranch(width, line, palette.text.assistant));
      }
      if (previewLines.length > 6) {
        lines.push(
          ...renderToolBranch(width, `+ ${previewLines.length - 6} more lines`, palette.text.dim),
        );
      }
    } else if (previewLines.length > 0) {
      lines.push(...renderToolBranch(width, TOOL_EXPAND_HINT, palette.text.dim));
    }
  }

  return lines;
}

export function renderReadTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const payload = parseToolResultPayload(tool);
  const path = getToolFilePath(args) ?? getStringArg(payload ?? {}, "path", "file_path", "file");
  const offset = typeof args.offset === "number" ? args.offset : undefined;
  const limit = typeof args.limit === "number" ? args.limit : undefined;
  const rangeSuffix =
    offset !== undefined || limit !== undefined
      ? `:${offset ?? 1}${limit !== undefined ? `-${(offset ?? 1) + limit - 1}` : ""}`
      : "";
  const lines = renderToolTitle(
    width,
    tool,
    `Read ${path ?? tool.description ?? tool.name}${rangeSuffix}`,
    options.animationPhase,
  );

  if (tool.result) {
    const content = getStringArg(payload ?? {}, "content", "result", "data") ?? tool.result;
    const { notices } = extractTrailingBracketNotices(content);
    const totalLines =
      getNumericArg(payload ?? {}, "totalLines", "total_lines") ?? countLogicalLines(content);
    const startLine = getNumericArg(payload ?? {}, "start_line", "startLine");
    const endLine = getNumericArg(payload ?? {}, "end_line", "endLine");
    const truncated = typeof payload?.truncated === "boolean" ? payload.truncated : undefined;
    if (path) {
      const lineRange =
        startLine !== undefined && endLine !== undefined ? ` · ${startLine}-${endLine}` : "";
      const meta = `${totalLines} lines${lineRange}${isCodeLikePath(path) ? " · code" : ""}`;
      lines.push(...renderToolTail(width, meta, palette.text.dim));
    }
    if (truncated) {
      lines.push(...renderToolBranch(width, "result truncated", palette.status.warning));
    }
    for (const notice of notices) {
      lines.push(...renderToolBranch(width, notice, palette.status.warning));
    }
  }

  return lines;
}

export function renderListTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const path = getToolFilePath(args) ?? ".";
  const limit = typeof args.limit === "number" ? args.limit : undefined;
  const lines = renderToolTitle(
    width,
    tool,
    `List ${path}${limit !== undefined ? ` (limit ${limit})` : ""}`,
    options.animationPhase,
  );

  if (tool.result) {
    const payload = parseToolResultPayload(tool);
    const files = getStringList(payload ?? {}, "files");
    const dirs = getStringList(payload ?? {}, "dirs").map((dir) => `${dir}/`);
    const payloadEntries = [...files, ...dirs];
    const { mainLines, notices } = extractTrailingBracketNotices(tool.result);
    const visibleEntries = payloadEntries.length > 0 ? payloadEntries : mainLines;
    const shown = visibleEntries.slice(0, options.showDetails ? 12 : 6);
    lines.push(
      ...renderToolTail(
        width,
        `${visibleEntries.length} entr${visibleEntries.length === 1 ? "y" : "ies"}`,
        palette.text.dim,
      ),
    );
    if (visibleEntries.length > shown.length) {
      lines.push(
        ...renderToolBranch(
          width,
          `+ ${visibleEntries.length - shown.length} more entries`,
          palette.text.dim,
        ),
      );
    }
    for (const notice of notices) {
      lines.push(...renderToolBranch(width, notice, palette.status.warning));
    }
  }

  return lines;
}

export function renderWriteTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const path = getToolFilePath(args);
  const content = typeof args.content === "string" ? args.content : "";
  const lines = renderToolTitle(
    width,
    tool,
    `Write ${path ?? tool.description ?? tool.name}`,
    options.animationPhase,
  );

  if (content) {
    lines.push(
      ...renderToolBranch(
        width,
        `${countLogicalLines(content)} lines · ${content.length} chars`,
        palette.text.dim,
      ),
    );
    const diffLines = renderAddedLines(Math.max(1, width - 2), content);
    const shown = options.showDetails ? diffLines : diffLines.slice(0, 6);
    lines.push(...shown.flatMap((line) => renderToolBranch(width, line, (value) => value)));
    if (!options.showDetails && diffLines.length > shown.length) {
      lines.push(
        ...renderToolBranch(
          width,
          `+ ${diffLines.length - shown.length} more diff lines`,
          palette.text.dim,
        ),
      );
      lines.push(...renderToolBranch(width, TOOL_EXPAND_HINT, palette.text.dim));
    }
  }

  if (tool.result) {
    lines.push(
      ...renderToolTail(
        width,
        tool.summary ??
          summarizeToolResultByKind(tool.name, tool.result) ??
          summarize(tool.result, 120),
        toolStateColor(tool),
      ),
    );
  }

  return lines;
}

export function renderEditTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const path = getToolFilePath(args);
  const lines = renderToolTitle(
    width,
    tool,
    `Edit ${path ?? tool.description ?? tool.name}`,
    options.animationPhase,
  );

  const oldString =
    typeof args.old_string === "string"
      ? args.old_string
      : typeof args.oldText === "string"
        ? args.oldText
        : undefined;
  const newString =
    typeof args.new_string === "string"
      ? args.new_string
      : typeof args.newText === "string"
        ? args.newText
        : undefined;

  if (oldString !== undefined && newString !== undefined) {
    const diffLines = renderSimpleDiff(Math.max(1, width - 2), oldString, newString);
    const shown = options.showDetails ? diffLines : diffLines.slice(0, 6);
    lines.push(...shown.flatMap((line) => renderToolBranch(width, line, (value) => value)));
    if (!options.showDetails && diffLines.length > shown.length) {
      lines.push(
        ...renderToolBranch(
          width,
          `+ ${diffLines.length - shown.length} more diff lines`,
          palette.text.dim,
        ),
      );
      lines.push(...renderToolBranch(width, TOOL_EXPAND_HINT, palette.text.dim));
    }
  } else if (Array.isArray(args.edits)) {
    lines.push(...renderToolTail(width, `${args.edits.length} edit block(s)`, palette.text.dim));
    const shownEdits = args.edits.slice(0, options.showDetails ? 3 : 1);
    for (const edit of shownEdits) {
      if (!isPlainObject(edit)) continue;
      const before =
        typeof edit.oldText === "string"
          ? edit.oldText
          : typeof edit.old_string === "string"
            ? edit.old_string
            : "";
      const after =
        typeof edit.newText === "string"
          ? edit.newText
          : typeof edit.new_string === "string"
            ? edit.new_string
            : "";
      if (!before && !after) continue;
      const diffLines = renderSimpleDiff(Math.max(1, width - 2), before, after);
      lines.push(
        ...diffLines
          .slice(0, options.showDetails ? 6 : 2)
          .flatMap((line) => renderToolBranch(width, line, (value) => value)),
      );
    }
    if (args.edits.length > shownEdits.length) {
      lines.push(
        ...renderToolBranch(
          width,
          `+ ${args.edits.length - shownEdits.length} more edit blocks`,
          palette.text.dim,
        ),
      );
    }
    if (!options.showDetails && args.edits.length > 0) {
      lines.push(...renderToolBranch(width, TOOL_EXPAND_HINT, palette.text.dim));
    }
  }

  if (tool.result) {
    lines.push(
      ...renderToolTail(
        width,
        tool.summary ??
          summarizeToolResultByKind(tool.name, tool.result) ??
          summarize(tool.result, 120),
        toolStateColor(tool),
      ),
    );
  }

  return lines;
}
