/**
 * 前端侧规模限制：镜像后端 extensions_v2/webui/constants.py 的 MAX_* 常量。
 * 后端是权威校验方，这里只做防御性检查；后端调整限制时需同步本文件。
 */

import { WEBUI_SUPPORTED_SCHEMA_VERSION } from '../contracts/wire.ts';

export interface WebUILimits {
  maxDepth: number;
  maxNodes: number;
  maxTextBytes: number;
  maxListItems: number;
  maxFrameBytes: number;
  maxActionArgsBytes: number;
  maxTargetStringBytes: number;
  supportedSchemaVersion: number;
}

export const DEFAULT_WEBUI_LIMITS: WebUILimits = Object.freeze({
  maxDepth: 8,
  maxNodes: 512,
  maxTextBytes: 16 * 1024,
  maxListItems: 100,
  maxFrameBytes: 256 * 1024,
  maxActionArgsBytes: 8 * 1024,
  maxTargetStringBytes: 200,
  supportedSchemaVersion: WEBUI_SUPPORTED_SCHEMA_VERSION,
});
