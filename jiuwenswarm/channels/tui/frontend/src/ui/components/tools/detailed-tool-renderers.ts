import type { ToolCallDisplay } from "../../../core/types.js";
import { renderFetchTool, renderRunTool } from "./command-tool-renderers.js";
import {
  renderEditTool,
  renderListTool,
  renderReadTool,
  renderSessionTool,
  renderWriteTool,
} from "./file-tool-renderers.js";
import {
  isEditTool,
  isFetchTool,
  isGlobTool,
  isListTool,
  isMcpTool,
  isReadTool,
  isRunTool,
  isSearchTool,
  isWriteTool,
} from "./tool-render-shared.js";
import type { DetailedToolRenderOptions } from "./tool-render-types.js";
import {
  renderGenericTool,
  renderGlobTool,
  renderMcpTool,
  renderSearchTool,
} from "./search-tool-renderers.js";

export function renderDetailedToolLines(
  tool: ToolCallDisplay,
  width: number,
  options: DetailedToolRenderOptions,
): string[] {
  if (tool.name === "session") {
    return renderSessionTool(tool, width, options);
  }
  if (isReadTool(tool.name)) {
    return renderReadTool(tool, width, options);
  }
  if (isListTool(tool.name)) {
    return renderListTool(tool, width, options);
  }
  if (isWriteTool(tool.name)) {
    return renderWriteTool(tool, width, options);
  }
  if (isEditTool(tool.name)) {
    return renderEditTool(tool, width, options);
  }
  if (isRunTool(tool.name)) {
    return renderRunTool(tool, width, options);
  }
  if (isFetchTool(tool.name)) {
    return renderFetchTool(tool, width, options);
  }
  if (isGlobTool(tool.name)) {
    return renderGlobTool(tool, width, options);
  }
  if (isSearchTool(tool.name)) {
    return renderSearchTool(tool, width, options);
  }
  if (isMcpTool(tool.name)) {
    return renderMcpTool(tool, width, options);
  }
  return renderGenericTool(tool, width, options);
}
