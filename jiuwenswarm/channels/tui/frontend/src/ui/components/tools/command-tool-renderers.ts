import type { ToolCallDisplay } from "../../../core/types.js";
import { palette } from "../../theme.js";
import { summarize } from "../../rendering/text.js";
import type { DetailedToolRenderOptions } from "./tool-render-types.js";
import {
  getNumericArg,
  getStringArg,
  nonEmptyLines,
  parseFetchResult,
  parseToolResultPayload,
  renderPreviewLines,
  renderStructuredBranch,
  renderToolBranch,
  renderToolTail,
  renderToolTitle,
  shouldRenderStructuredPayloadByDefault,
  summarizePath,
  summarizeToolResultByKind,
  toolStateColor,
} from "./tool-render-shared.js";

function firstNonEmptyLines(...values: unknown[]): string[] {
  for (const value of values) {
    const lines = nonEmptyLines(value);
    if (lines.length > 0) return lines;
  }
  return [];
}

export function renderRunTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const payload = parseToolResultPayload(tool);
  const command =
    getStringArg(args, "command", "cmd", "script", "input") ??
    getStringArg(payload ?? {}, "command", "cmd") ??
    tool.description ??
    tool.name;
  const lines = renderToolTitle(
    width,
    tool,
    `Run ${summarize(command, 120)}`,
    options.animationPhase,
  );

  const cwd = getStringArg(args, "cwd", "path", "workdir");
  if (cwd) {
    lines.push(...renderToolBranch(width, `cwd: ${summarizePath(cwd) ?? cwd}`, palette.text.dim));
  }

  if (tool.result) {
    const exitCode =
      getNumericArg(payload ?? {}, "exit_code", "exitCode", "code") ??
      getNumericArg(args, "exit_code", "exitCode", "code");
    const stdoutLines = firstNonEmptyLines(
      payload?.stdout,
      payload?.output,
      payload?.content,
      payload?.result,
    );
    const stderrLines = nonEmptyLines(payload?.stderr);

    const summaryParts: string[] = [];
    if (exitCode !== undefined) {
      summaryParts.push(`exit ${exitCode}`);
    }
    const outputLines = stdoutLines.length > 0 || stderrLines.length > 0;
    const summaryText = tool.summary ?? summaryParts.join(" | ");
    if (summaryText || !outputLines) {
      lines.push(
        ...renderToolTail(
          width,
          summaryText || summarize(tool.result, 120),
          toolStateColor(tool),
        ),
      );
    }

    if (outputLines) {
      if (tool.isError || tool.status === "timeout") {
        lines.push(
          ...renderPreviewLines(
            width,
            stderrLines.length > 0 ? stderrLines : stdoutLines,
            tool.isError ? palette.status.error : palette.status.warning,
            4,
            2,
            options.showDetails,
            "lines",
          ),
        );
      }
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
      !payload &&
      options.showDetails &&
      shouldRenderStructuredPayloadByDefault(tool.name)
    ) {
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

export function renderFetchTool(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  const args =
    tool.arguments && typeof tool.arguments === "object"
      ? (tool.arguments as Record<string, unknown>)
      : {};
  const fetchResult = parseFetchResult(tool);
  const url = getStringArg(args, "url") ?? fetchResult.url ?? tool.description ?? tool.name;
  const lines = renderToolTitle(
    width,
    tool,
    `Fetch ${summarize(url, 120)}`,
    options.animationPhase,
  );

  const metaParts = [
    fetchResult.status ? `status ${fetchResult.status}` : undefined,
    fetchResult.title,
  ].filter((part): part is string => Boolean(part && part.trim()));
  if (metaParts.length > 0) {
    lines.push(...renderToolBranch(width, metaParts.join(" | "), palette.text.dim));
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
