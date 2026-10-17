import { existsSync, mkdirSync, readdirSync, statSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, parse, relative } from "node:path";
import { addError, addInfo, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import { getEditorEnvironmentHint } from "../../utils/editor.js";
import {
  findGitRoot,
  formatMemoryPathForDisplay,
  getDisplayPath,
  isAncestorOrSelfDir,
} from "./memory-path-utils.js";

export interface MemoryFile {
  path: string;
  relative_path: string;
  kind: string;
  exists: boolean;
  size: number;
  mtime: number;
  lines: number;
}

interface MemoryEditResult {
  path: string;
  exists: boolean;
  content_preview: string;
  kind: string;
  editable: boolean;
  reason?: string;
}

interface MemoryStatusResult {
  current_mode: string;
  storage_mode: string;
  engine: string;
  enabled: boolean;
  proactive: boolean;
  forbidden_enabled: boolean;
  auto_memory_enabled: boolean;
  /** code mode 专属：子 agent 兜底提取开关；agent mode 为 null（UI 不展示） */
  auto_coding_memory?: boolean | null;
  index?: {
    available: boolean;
    provider?: string | null;
    model?: string | null;
    files_count: number;
    chunks_count: number;
    dirty: boolean;
    fts: Record<string, unknown>;
    vector: Record<string, unknown>;
    cache: Record<string, unknown>;
  };
  project_memory?: {
    files_count: number;
    total_chars: number;
    max_chars: number;
    project_dir?: string;
  };
  coding_memory?: {
    files_count: number;
    total_chars: number;
    dir: string;
  };
  auto_memory?: {
    files_count: number;
    total_chars: number;
    dir: string;
  };
  external_memory?: {
    provider: string;
    enabled: boolean;
  };
}

interface MemoryToggleResult {
  key: string;
  old_value: boolean;
  new_value: boolean;
  mode_affected: string;
  needs_restart: boolean;
}

interface MemoryOpenResult {
  memory_dir: string;
  project_memory_dir: string;
  project_dir?: string;
  coding_memory_dir?: string;
  auto_memory_dir?: string;
}

// ---------------------------------------------------------------------------
// Frontend-side memory file discovery (mirrors Claude Code's unguarded walk)
// ---------------------------------------------------------------------------

/** File patterns to scan at each directory level (aligned with backend's files.py). */
const PROJECT_MEMORY_FILES: [string, string][] = [
  ["JIUWENSWARM.md", "project"],
  [".jiuwen/JIUWENSWARM.md", "project"],
];
const LOCAL_MEMORY_FILES: [string, string][] = [
  ["JIUWENSWARM.local.md", "local"],
];

/** Probe a single path on disk; returns real state if file exists, placeholder if not. */
function probeFile(absPath: string, relPath: string, kind: string): MemoryFile {
  if (existsSync(absPath)) {
    try {
      const stat = statSync(absPath);
      const content = readFileSync(absPath, "utf-8");
      const lines = content.replace(/[\r\n]+$/, "").split(/\r\n|\n/).length;
      return {
        path: absPath,
        relative_path: relPath,
        kind,
        exists: true,
        size: stat.size,
        mtime: Math.floor(stat.mtimeMs / 1000),
        lines,
      };
    } catch {
      // stat/read failed — still mark exists, just with zero metrics
      return { path: absPath, relative_path: relPath, kind, exists: true, size: 0, mtime: 0, lines: 0 };
    }
  }
  return { path: absPath, relative_path: relPath, kind, exists: false, size: 0, mtime: 0, lines: 0 };
}

/** Normalize path for de-duplication (case-insensitive on Windows). */
function normalizePathKey(p: string): string {
  try {
    return process.platform === "win32" ? p.toLowerCase() : p;
  } catch {
    return p;
  }
}

function errorCode(error: unknown): string | undefined {
  return typeof error === "object" && error !== null && "code" in error
    ? String((error as { code?: unknown }).code)
    : undefined;
}

function createEmptyMemoryFile(filePath: string): void {
  mkdirSync(dirname(filePath), { recursive: true });
  try {
    writeFileSync(filePath, "", { encoding: "utf-8", flag: "wx" });
  } catch (error) {
    if (errorCode(error) !== "EEXIST" || !existsSync(filePath)) throw error;
  }
}

/**
 * Walk from CWD upward to root, scanning each directory for memory files.
 * This mirrors Claude Code's unguarded traversal in claudemd.ts — no project
 * root marker is required, every level is scanned unconditionally.
 *
 * Order: root → CWD (outermost ancestor first, CWD last), so closer files
 * have higher priority (loaded later → override earlier).
 */
function discoverMemoryFilesFromFs(cwd: string): MemoryFile[] {
  const results: MemoryFile[] = [];
  const seenPaths = new Set<string>();

  // 1. User-level memory
  const userJiuwenDir = join(homedir(), ".jiuwen");
  const userMemoryPath = join(userJiuwenDir, "JIUWENSWARM.md");
  const userFile = probeFile(userMemoryPath, relative(homedir(), userMemoryPath), "user");
  if (userFile.exists) {
    results.push(userFile);
    seenPaths.add(normalizePathKey(userFile.path));
  }
  // .jiuwen/rules/*.md at user level
  const userRulesDir = join(userJiuwenDir, "rules");
  if (existsSync(userRulesDir)) {
    try {
      for (const entry of readdirSync(userRulesDir)) {
        if (entry.endsWith(".md")) {
          const absPath = join(userRulesDir, entry);
          const f = probeFile(absPath, relative(homedir(), absPath), "user");
          if (f.exists && !seenPaths.has(normalizePathKey(f.path))) {
            results.push(f);
            seenPaths.add(normalizePathKey(f.path));
          }
        }
      }
    } catch { /* ignore unreadable dirs */ }
  }

  // 2. Project & Local — walk from root → CWD (reversed so closer dirs come last = higher priority)
  const dirs: string[] = [];
  let currentDir = cwd;
  const root = parse(currentDir).root;
  while (currentDir !== root) {
    dirs.push(currentDir);
    currentDir = dirname(currentDir);
  }
  // root directory itself is NOT included (same as Claude Code)

  // Reverse: root → CWD so closer-to-CWD files appear later (higher priority)
  dirs.reverse();

  for (const dir of dirs) {
    for (const [rel, kind] of PROJECT_MEMORY_FILES) {
      const absPath = join(dir, rel);
      const f = probeFile(absPath, relative(cwd, absPath), kind);
      if (!seenPaths.has(normalizePathKey(absPath))) {
        seenPaths.add(normalizePathKey(absPath));
        if (f.exists) results.push(f);
      }
    }
    // .jiuwen/rules/*.md at this level
    const rulesDir = join(dir, ".jiuwen", "rules");
    if (existsSync(rulesDir)) {
      try {
        for (const entry of readdirSync(rulesDir)) {
          if (entry.endsWith(".md")) {
            const absPath = join(rulesDir, entry);
            if (!seenPaths.has(normalizePathKey(absPath))) {
              seenPaths.add(normalizePathKey(absPath));
              const f = probeFile(absPath, relative(cwd, absPath), "project");
              if (f.exists) results.push(f);
            }
          }
        }
      } catch { /* ignore */ }
    }
    for (const [rel, kind] of LOCAL_MEMORY_FILES) {
      const absPath = join(dir, rel);
      if (!seenPaths.has(normalizePathKey(absPath))) {
        seenPaths.add(normalizePathKey(absPath));
        const f = probeFile(absPath, relative(cwd, absPath), kind);
        if (f.exists) results.push(f);
      }
    }
  }

  return results;
}

function modeToShort(mode: string): string {
  // 新三段命名 agent.code.* / team.code.* 也是 code profile；agent.work.* 归到 canonical "agent"。
  if (mode.startsWith("code") || mode.startsWith("agent.code") || mode.startsWith("team.code")) return "code";
  if (mode.startsWith("agent")) return "agent";
  return mode;
}

/** 收集并排序可编辑规则文件（合并后端 list + 前端发现，含占位条目）。
 *  供 list / edit 页签复用。 */
export async function collectOrderedMemoryFiles(
  ctx: import("../types.js").CommandContext,
  mode: string,
): Promise<{ files: MemoryFile[]; userMemoryPath: string; gitRoot: string | null; projectDir: string }> {
  const projectDir = ctx.getCurrentProjectDir();
  const listPayload = await ctx.request<{ files: MemoryFile[] }>("memory.list", { mode });
  const files = listPayload.files ?? [];

  // Frontend-side unguarded traversal to fill gaps
  const discovered = discoverMemoryFilesFromFs(projectDir);
  const frontendByPath = new Map<string, MemoryFile>();
  for (const f of discovered) {
    frontendByPath.set(normalizePathKey(f.path), f);
  }
  const seenPaths = new Set(files.map((f) => normalizePathKey(f.path)));
  const mergedFiles: MemoryFile[] = files.map((f) => {
    if (f.relative_path === f.path) {
      const frontend = frontendByPath.get(normalizePathKey(f.path));
      if (frontend && frontend.relative_path !== frontend.path) {
        return { ...f, relative_path: frontend.relative_path };
      }
    }
    return f;
  });
  for (const f of discovered) {
    if (!seenPaths.has(normalizePathKey(f.path))) {
      mergedFiles.push(f);
      seenPaths.add(normalizePathKey(f.path));
    }
  }

  const homeDir = homedir();
  const gitRoot = findGitRoot(projectDir);
  const userMemoryPath = join(homeDir, ".jiuwen", "JIUWENSWARM.md");
  const filePathLowerFn = (p: string) => (process.platform === "win32" ? p.toLowerCase() : p);

  const projectMemoryFile = mergedFiles.find(
    (f) => f.kind === "project"
      && f.path.endsWith("JIUWENSWARM.md")
      && !f.path.endsWith("JIUWENSWARM.local.md"),
  );
  const localMemoryFile = mergedFiles.find(
    (f) => f.kind === "local" && f.path.endsWith("JIUWENSWARM.local.md"),
  );
  const userMemoryFile = mergedFiles.find(
    (f) => filePathLowerFn(f.path) === filePathLowerFn(userMemoryPath),
  );
  const projectRules = mergedFiles.filter(
    (f) => f.kind === "project" && f !== projectMemoryFile && f !== localMemoryFile,
  );
  const userRules = mergedFiles.filter(
    (f) => f.kind === "user" && filePathLowerFn(f.path) !== filePathLowerFn(userMemoryPath),
  );

  const orderedFiles: MemoryFile[] = [];
  if (projectMemoryFile) orderedFiles.push(projectMemoryFile);
  for (const f of projectRules) orderedFiles.push(f);
  if (localMemoryFile) orderedFiles.push(localMemoryFile);
  if (userMemoryFile) orderedFiles.push(userMemoryFile);
  for (const f of userRules) orderedFiles.push(f);

  const projMemBase = gitRoot || projectDir;
  if (!projectMemoryFile) {
    orderedFiles.unshift(probeFile(join(projMemBase, "JIUWENSWARM.md"), "JIUWENSWARM.md", "project"));
  }
  if (!localMemoryFile) {
    const insertIdx = orderedFiles.findIndex(
      (f) => filePathLowerFn(f.path) === filePathLowerFn(userMemoryPath),
    );
    const localProbe = probeFile(join(projMemBase, "JIUWENSWARM.local.md"), "JIUWENSWARM.local.md", "local");
    if (insertIdx >= 0) {
      orderedFiles.splice(insertIdx, 0, localProbe);
    } else {
      orderedFiles.push(localProbe);
    }
  }
  if (!userMemoryFile) {
    orderedFiles.push(probeFile(userMemoryPath, "JIUWENSWARM.md", "user"));
  }
  return { files: orderedFiles, userMemoryPath, gitRoot, projectDir };
}

/** edit 页签：纯文件选择器（无开关/打开文件夹行），选中即用 $EDITOR 打开。 */
async function editMemorySelector(ctx: import("../types.js").CommandContext): Promise<void> {
  const mode = modeToShort(ctx.mode);
  try {
    const { files, userMemoryPath, gitRoot, projectDir } = await collectOrderedMemoryFiles(ctx, mode);
    const filePathLowerFn = (p: string) => (process.platform === "win32" ? p.toLowerCase() : p);
    const options = files.map((f) => {
      const displayPath = getDisplayPath(f.path, projectDir);
      let label: string;
      let description: string | undefined;
      if (filePathLowerFn(f.path) === filePathLowerFn(userMemoryPath)) {
        label = "User memory";
        description = `Saved in ${displayPath}`;
      } else if (
        f.kind === "project"
        && f.path.endsWith("JIUWENSWARM.md")
        && !f.path.endsWith("JIUWENSWARM.local.md")
        && !f.path.endsWith(".jiuwen/JIUWENSWARM.md")
      ) {
        label = "Project memory";
        description = `${gitRoot ? "Checked in at" : "Saved in"} ${displayPath}`;
      } else if (f.kind === "local" && f.path.endsWith("JIUWENSWARM.local.md")) {
        label = "Local memory";
        description = `Saved in ${displayPath}`;
      } else {
        label = displayPath;
        description = undefined;
      }
      return { label, description, value: f.path };
    });

    let selectedValue: string | undefined;
    try {
      const [answer] = await ctx.askQuestions(
        [
          {
            header: "Memory edit",
            question: "Select a file to edit:",
            options: options.map((o) => ({ label: o.label, description: o.description })),
          },
        ],
        "local_command_memory_edit",
      );
      const selectedLabel = answer?.selected_options?.[0];
      selectedValue = selectedLabel
        ? options.find((o) => o.label === selectedLabel)?.value
        : undefined;
    } catch {
      ctx.addItem(addInfo(ctx.sessionId, "Cancelled.", "i"));
      return;
    }
    if (!selectedValue) {
      ctx.addItem(addInfo(ctx.sessionId, "Cancelled.", "i"));
      return;
    }
    await editMemoryByPath(ctx, selectedValue);
  } catch (err) {
    ctx.addItem(
      addError(ctx.sessionId, `Failed to list memory files: ${err instanceof Error ? err.message : String(err)}`),
    );
  }
}

/** 页签总页面：SelectList 模拟（无原生页签原语时的降级方案）。
 *  initialTab 指定时直达该页签；否则弹出页签选择器。 */
const MEMORY_TABS = ["edit", "status", "toggle", "open"] as const;
type MemoryTab = (typeof MEMORY_TABS)[number];

async function showMemoryConsole(
  ctx: import("../types.js").CommandContext,
  initialTab?: MemoryTab,
): Promise<void> {
  if (initialTab === "edit") return editMemorySelector(ctx);
  if (initialTab === "status") return showMemoryStatus(ctx);
  if (initialTab === "toggle") return showToggleList(ctx);
  if (initialTab === "open") return openMemoryDir(ctx);

  // 无 initialTab：弹出页签选择器
  try {
    const [answer] = await ctx.askQuestions(
      [
        {
          header: "Memory",
          question: "Select a tab:",
          options: MEMORY_TABS.map((t) => ({ label: t })),
        },
      ],
      "local_command_memory_console",
    );
    const selected = answer?.selected_options?.[0] as MemoryTab | undefined;
    if (!selected) {
      ctx.addItem(addInfo(ctx.sessionId, "Cancelled.", "i"));
      return;
    }
    await showMemoryConsole(ctx, selected);
  } catch {
    ctx.addItem(addInfo(ctx.sessionId, "Cancelled.", "i"));
  }
}

async function editMemory(
  ctx: import("../types.js").CommandContext,
  args: string,
): Promise<void> {
  const targetPath = args.trim();

  if (!targetPath) {
    await editMemorySelector(ctx);
    return;
  }

  await editMemoryByPath(ctx, targetPath);
}

async function editMemoryByPath(
  ctx: import("../types.js").CommandContext,
  path: string,
): Promise<void> {
  try {
    const trustedDirs = ctx.getTrustedDirs();
    const projectDir = ctx.getCurrentProjectDir();
    const gitRoot = findGitRoot(projectDir);

    // 把 display path（相对路径或 ~ 缩写）解析为绝对路径
    // getDisplayPath 可能返回相对于 gitRoot/projectDir 的路径或 ~ 缩写
    let resolvedPath = path;
    if (path.startsWith("~/") || path === "~") {
      resolvedPath = join(homedir(), path.slice(1));
    } else if (!path.match(/^[A-Za-z]:[/\\]/) && !path.startsWith("/")) {
      // 已存在文件优先按 projectDir 解析；否则使用 git root（如有）
      // 或 projectDir 作为创建位置。
      const fromProject = join(projectDir, path);
      if (existsSync(fromProject)) {
        resolvedPath = fromProject;
      } else {
        if (gitRoot) {
          resolvedPath = join(gitRoot, path);
        } else {
          resolvedPath = fromProject;
        }
      }
    }
    path = resolvedPath;

    // 后端 _validate_edit_path 只白名单 project_dir 单层,会拒绝编辑 projectDir
    // 祖先目录里的 JIUWENSWARM.md / JIUWENSWARM.local.md。这类文件是合法的
    // project memory(前端 discoverMemoryFilesFromFs 已识别并列入选择器),
    // 且由用户主动从列表选中,故绕过 memory.edit RPC,直接用本地 openInEditor
    // 打开(与 keybindings.ts 打开配置文件同级风险,不经后端校验)。
    const baseName = path.replace(/\\/g, "/").split("/").pop() ?? "";
    const fileParent = path.replace(/[/\\][^/\\]*$/, "");
    const isAncestorMemFile =
      (baseName === "JIUWENSWARM.md" || baseName === "JIUWENSWARM.local.md")
      && !!projectDir
      && !isAncestorOrSelfDir(projectDir, path) // 文件不在 projectDir 内部
      && isAncestorOrSelfDir(fileParent, projectDir); // 其父目录是 projectDir 的祖先/本身

    if (isAncestorMemFile) {
      const displayPath = getDisplayPath(path, projectDir);
      if (!existsSync(path)) {
        const isGitRootMemoryFile =
          !!gitRoot
          && isAncestorOrSelfDir(fileParent, gitRoot)
          && isAncestorOrSelfDir(gitRoot, fileParent);
        if (!isGitRootMemoryFile) {
          ctx.addItem(addError(ctx.sessionId, `Cannot edit: ${path} — memory file does not exist.`));
          return;
        }
        try {
          createEmptyMemoryFile(path);
        } catch (error) {
          ctx.addItem(
            addError(
              ctx.sessionId,
              `Cannot create memory file: ${error instanceof Error ? error.message : String(error)}`,
            ),
          );
          return;
        }
      }
      if (ctx.openInEditor) {
        const editorEnvironmentHint = getEditorEnvironmentHint();
        // openInEditor blocks until the editor window closes (TUI frozen in
        // the meantime). Report the result only after the editor exits.
        await ctx.openInEditor(path, (success) => {
          if (success === false) {
            ctx.addItem(addError(ctx.sessionId, `Failed to open editor for memory file: ${displayPath}`));
            return;
          }
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              `Memory file edited successfully: ${displayPath}\n\n> ${editorEnvironmentHint}`,
              "m",
            ),
          );
        });
      } else {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            `Edit with:  $EDITOR ${displayPath}`,
            "i",
          ),
        );
      }
      return;
    }

    const payload = await ctx.request<MemoryEditResult>("memory.edit", {
      path,
      trusted_dirs: trustedDirs.length > 0 ? trustedDirs : undefined,
      cwd: ctx.getWorkspaceDir(),
    });

    if (!payload.editable) {
      const reason = payload.reason ?? "path not in allowed memory directories";
      ctx.addItem(addError(ctx.sessionId, `Cannot edit: ${path} — ${reason}.`));
      return;
    }

    if (!payload.exists && !existsSync(payload.path)) {
      try {
        createEmptyMemoryFile(payload.path);
      } catch (error) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            `Cannot create memory file: ${error instanceof Error ? error.message : String(error)}`,
          ),
        );
        return;
      }
    }

    if (ctx.openInEditor) {
      const projectDir = ctx.getCurrentProjectDir();
      const displayPath = getDisplayPath(payload.path, projectDir);
      const editorEnvironmentHint = getEditorEnvironmentHint();
      // openInEditor blocks until the editor window closes (TUI frozen in
      // the meantime). Report the result only after the editor exits.
      await ctx.openInEditor(payload.path, (success) => {
        if (success === false) {
          ctx.addItem(addError(ctx.sessionId, `Failed to open editor for memory file: ${displayPath}`));
          return;
        }
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            `Memory file edited successfully: ${displayPath}\n\n> ${editorEnvironmentHint}`,
            "m",
          ),
        );
      });
    } else {
      const projectDir = ctx.getCurrentProjectDir();
      const displayPath = getDisplayPath(payload.path, projectDir);
      ctx.addItem(
        addInfo(
          ctx.sessionId,
          `Edit with:  $EDITOR ${displayPath}`,
          "i",
        ),
      );
    }
  } catch (err) {
    ctx.addItem(
      addError(ctx.sessionId, `Failed to edit memory file: ${err instanceof Error ? err.message : String(err)}`),
    );
  }
}

async function showMemoryStatus(
  ctx: import("../types.js").CommandContext,
): Promise<void> {
  const mode = modeToShort(ctx.mode);
  try {
    const payload = await ctx.request<MemoryStatusResult>("memory.status", {
      detailed: true,
      mode,
    });

    const items: { label: string; value: string; description?: string }[] = [];

    items.push({ label: "Current Mode", value: payload.current_mode });
    items.push({ label: "Storage Mode", value: payload.storage_mode });
    items.push({ label: "Engine", value: payload.engine });

    // 开关值行：镜像当前 mode 的 toggle 集合（registry 单一数据源）
    for (const t of togglesForMode(mode)) {
      items.push({ label: t.label, value: t.readValue(payload) ? "✓ on" : "✗ off" });
    }

    // Index / FTS5 / Vector / Cache：仅 agent mode（code mode 不走 agent 记忆引擎）
    if (modeCategory(mode) === "agent" && payload.index) {
      items.push({ label: "Index Available", value: payload.index.available ? "✓" : "✗" });
      items.push({ label: "Embedding Provider", value: payload.index.provider ?? "N/A" });
      items.push({ label: "Embedding Model", value: payload.index.model ?? "N/A" });
      items.push({ label: "Files Indexed", value: String(payload.index.files_count) });
      items.push({ label: "Chunks", value: String(payload.index.chunks_count) });
      items.push({ label: "Dirty", value: payload.index.dirty ? "yes" : "no" });
      const ftsInfo = payload.index.fts as { enabled?: boolean; available?: boolean; error?: string } | undefined;
      const vecInfo = payload.index.vector as { enabled?: boolean; available?: boolean; dims?: number; error?: string } | undefined;
      const cacheInfo = payload.index.cache as { enabled?: boolean; entries?: number } | undefined;
      items.push({
        label: "FTS5",
        value: ftsInfo?.available ? "✓ enabled" : "✗ disabled",
        description: ftsInfo?.error,
      });
      items.push({
        label: "Vector",
        value: vecInfo?.available ? `✓ enabled (dims: ${vecInfo.dims ?? "?"})` : "✗ disabled",
        description: vecInfo?.error,
      });
      items.push({
        label: "Cache",
        value: cacheInfo?.enabled ? `✓ ${cacheInfo.entries ?? 0} entries` : "✗ disabled",
      });
    }

    if (payload.project_memory) {
      items.push({
        label: "Project Memory Files",
        value: String(payload.project_memory.files_count),
      });
      items.push({
        label: "Project Memory Chars",
        value: `${payload.project_memory.total_chars} / ${payload.project_memory.max_chars}`,
      });
      if (payload.project_memory.project_dir) {
        items.push({
          label: "Project Dir",
          value: formatMemoryPathForDisplay(payload.project_memory.project_dir),
        });
      }
    }

    // Coding Memory 统计：仅 code mode
    if (modeCategory(mode) === "code" && payload.coding_memory) {
      items.push({
        label: "Coding Memory Files",
        value: String(payload.coding_memory.files_count),
      });
      items.push({
        label: "Coding Memory Chars",
        value: String(payload.coding_memory.total_chars),
      });
      if (payload.coding_memory.dir) {
        items.push({
          label: "Coding Memory Dir",
          value: formatMemoryPathForDisplay(payload.coding_memory.dir),
        });
      }
    }

    // Auto Memory 统计：仅 agent mode
    if (modeCategory(mode) === "agent" && payload.auto_memory) {
      items.push({
        label: "Auto Memory Files",
        value: String(payload.auto_memory.files_count),
      });
      items.push({
        label: "Auto Memory Chars",
        value: String(payload.auto_memory.total_chars),
      });
      if (payload.auto_memory.dir) {
        items.push({
          label: "Auto Memory Dir",
          value: formatMemoryPathForDisplay(payload.auto_memory.dir),
        });
      }
    }

    if (payload.external_memory) {
      items.push({
        label: "External Memory",
        value: `${payload.external_memory.provider} ${payload.external_memory.enabled ? "✓" : "✗"}`,
      });
    }

    ctx.addItem(
      makeItem(ctx.sessionId, "info", "Memory Status (detailed)", "m", {
        view: "kv",
        title: "Memory Status",
        items,
      }),
    );
  } catch (err) {
    ctx.addItem(
      addError(ctx.sessionId, `Failed to get memory status: ${err instanceof Error ? err.message : String(err)}`),
    );
  }
}

// ---- MemoryActionRegistry: 单一数据源（按 mode 过滤）----
// 消除 TOGGLE_KEYS(4) 与 toggle completion(3) 漂移；开关集合按 mode 自适应。
// agent mode: memory_enabled / memory_forbidden_enabled
// code mode:  memory_enabled / auto_coding_memory / memory_forbidden_enabled

type MemoryModeCategory = "agent" | "code";

interface ToggleDef {
  key: string;
  label: string;
  modes: MemoryModeCategory[];
  getConfigPath: (mode: string) => string;
  readValue: (payload: MemoryStatusResult) => boolean;
}

const TOGGLE_DEFS: ToggleDef[] = [
  {
    key: "memory_enabled",
    label: "Memory",
    modes: ["agent", "code"],
    getConfigPath: (mode) =>
      mode === "code" ? "modes.code.memory.enabled" : `modes.agent.${mode}.memory.enabled`,
    readValue: (p) => p.enabled,
  },
      // {
      //   key: "memory_proactive",
      //   label: "Proactive memory",
      //   modes: ["agent"],
      //   getConfigPath: (mode) => `modes.agent.${mode}.memory.is_proactive`,
      //   readValue: (p) => p.proactive,
      // },
  {
    key: "auto_coding_memory",
    label: "Auto coding memory",
    modes: ["code"],
    getConfigPath: () => "modes.code.memory.auto_coding_memory",
    readValue: (p) => p.auto_coding_memory ?? false,
  },
  {
    key: "memory_forbidden_enabled",
    label: "Forbidden filter",
    modes: ["agent", "code"],
    getConfigPath: () => "memory.forbidden_memory_definition.enabled",
    readValue: (p) => p.forbidden_enabled,
  },
];

function modeCategory(shortMode: string): MemoryModeCategory {
  return shortMode === "code" ? "code" : "agent";
}

function togglesForMode(shortMode: string): ToggleDef[] {
  const cat = modeCategory(shortMode);
  return TOGGLE_DEFS.filter((t) => t.modes.includes(cat));
}

async function toggleMemory(
  ctx: import("../types.js").CommandContext,
  args: string,
): Promise<void> {
  const key = args.trim();

  if (!key) {
    await showToggleList(ctx);
    return;
  }

  await toggleByKey(ctx, key);
}

async function showToggleList(
  ctx: import("../types.js").CommandContext,
): Promise<void> {
  const mode = modeToShort(ctx.mode);
  try {
    const payload = await ctx.request<MemoryStatusResult>("memory.status", {
      mode,
    });

    const items = togglesForMode(mode).map((t) => {
      const current = t.readValue(payload);
      return {
        label: t.key,
        value: `${t.label} ${current ? "✓ on" : "✗ off"}`,
        description: t.getConfigPath(mode),
      };
    });

    ctx.addItem(
      makeItem(ctx.sessionId, "info", "Memory Toggles", "m", {
        view: "kv",
        title: "Memory Toggles",
        items,
      }),
    );

    ctx.addItem(
      addInfo(
        ctx.sessionId,
        `Usage: /memory toggle <key>  (affects mode: ${mode})`,
        "i",
      ),
    );
  } catch (err) {
    ctx.addItem(
      addError(ctx.sessionId, `Failed to get toggle status: ${err instanceof Error ? err.message : String(err)}`),
    );
  }
}

async function toggleByKey(
  ctx: import("../types.js").CommandContext,
  key: string,
): Promise<void> {
  const mode = modeToShort(ctx.mode);
  const defs = togglesForMode(mode);
  const validKeys = defs.map((t) => t.key);
  if (!validKeys.includes(key)) {
    ctx.addItem(
      addError(ctx.sessionId, `Unknown toggle key: ${key}. Valid keys: ${validKeys.join(", ")}`),
    );
    return;
  }

  try {
    const payload = await ctx.request<MemoryToggleResult>("memory.toggle", {
      key,
      mode,
    });

    const label = defs.find((t) => t.key === key)?.label ?? key;
    ctx.addItem(
      addInfo(
        ctx.sessionId,
        `${label}: ${payload.old_value ? "on" : "off"} → ${payload.new_value ? "on" : "off"}${payload.needs_restart ? " (restart session to apply)" : ""}`,
        "m",
      ),
    );
  } catch (err) {
    ctx.addItem(
      addError(ctx.sessionId, `Toggle failed: ${err instanceof Error ? err.message : String(err)}`),
    );
  }
}

async function openMemoryDir(
  ctx: import("../types.js").CommandContext,
): Promise<void> {
  const mode = modeToShort(ctx.mode);
  const cat = modeCategory(mode);
  try {
    const payload = await ctx.request<MemoryOpenResult>("memory.open", {
      project_dir: ctx.getCurrentProjectDir() || undefined,
    });

    // 按 mode 过滤目录：agent 显 Memory Dir（auto memory）；code 显 Coding Memory Dir
    const options: { label: string; description?: string; value: string }[] = [];
    if (cat === "agent") {
      options.push({ label: "Memory Dir", value: payload.memory_dir });
    }
    if (cat === "code" && payload.coding_memory_dir) {
      options.push({ label: "Coding Memory Dir", value: payload.coding_memory_dir });
    }
    options.push({ label: "Project Dir", value: payload.project_memory_dir });
    if (payload.project_dir) {
      options.push({ label: "User Project Dir", value: payload.project_dir });
    }

    let selectedValue: string | undefined;
    try {
      const [answer] = await ctx.askQuestions(
        [
          {
            header: "Memory open",
            question: "Select a directory to open:",
            options: options.map((o) => ({
              label: o.label,
              description: formatMemoryPathForDisplay(o.value),
            })),
          },
        ],
        "local_command_memory_open",
      );
      const selectedLabel = answer?.selected_options?.[0];
      selectedValue = selectedLabel
        ? options.find((o) => o.label === selectedLabel)?.value
        : undefined;
    } catch {
      ctx.addItem(addInfo(ctx.sessionId, "Cancelled.", "i"));
      return;
    }
    if (!selectedValue) {
      ctx.addItem(addInfo(ctx.sessionId, "Cancelled.", "i"));
      return;
    }

    // 优先调系统文件管理器打开；不支持时(无 GUI 或未注入回调)显示可复制路径提示
    const opened = ctx.openFolder?.(selectedValue);
    const displayValue = formatMemoryPathForDisplay(selectedValue);
    if (opened) {
      ctx.addItem(addInfo(ctx.sessionId, `Opened memory folder: ${displayValue}`, "m"));
    } else {
      // 无 GUI explorer(如无头 Linux 服务器)或未注入 openFolder 回调:
      // 显示可复制路径 + 平台命令,避免误导用户以为文件夹已打开。
      let cmd: string;
      if (process.platform === "win32") {
        cmd = `explorer "${displayValue}"`;
      } else if (process.platform === "darwin") {
        cmd = `open "${displayValue}"`;
      } else {
        cmd = `xdg-open "${displayValue}"`;
      }
      ctx.addItem(
        addInfo(
          ctx.sessionId,
          `No GUI explorer detected. Path: ${displayValue}\nOpen with:  ${cmd}`,
          "i",
        ),
      );
    }
  } catch (err) {
    ctx.addItem(
      addError(ctx.sessionId, `Failed to get memory directories: ${err instanceof Error ? err.message : String(err)}`),
    );
  }
}

export function createMemoryCommand(): SlashCommand {
  return {
    name: "memory",
    altNames: ["mem"],
    description: "Manage memory settings and files (Auto-memory, edit, toggle, open)",
    usage: "/memory [edit|status|toggle|open] [args]",
    example: "/memory",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      // 无参 → 弹出页签选择器（edit/status/toggle/open）；
      // 子命令 /memory <sub> 由各自 subCommand 直达对应页签。
      // 若跟了不存在的子命令（如已废弃的 list），参考 CC 报错而非触发页签选择器。
      const firstArg = args.trim().split(/\s+/)[0];
      if (firstArg && !(MEMORY_TABS as readonly string[]).includes(firstArg)) {
        ctx.addItem(addError(ctx.sessionId, `Unknown memory subcommand: ${firstArg}`));
        return;
      }
      await showMemoryConsole(ctx);
    },
    completion: async () => {
      return ["edit", "status", "toggle", "open"];
    },
    subCommands: [
      {
        name: "edit",
        description: "Edit a memory file (interactive selection if no path given)",
        usage: "/memory edit [path]",
        example: "/memory edit memory/MEMORY.md",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          await editMemory(ctx, args);
        },
        completion: async (ctx) => {
          // 动态列规则文件（不含运行时记忆，后者只读），只返回存在的文件，去重
          // 路径用 getDisplayPath 展示（与 edit 页签一致）
          try {
            const { files, projectDir } = await collectOrderedMemoryFiles(ctx, ctx.mode);
            const seen = new Set<string>();
            return files
              .filter((f) => f.exists)
              .map((f) => getDisplayPath(f.path, projectDir))
              .filter((p) => {
                const key = p.toLowerCase().replace(/\\/g, "/");
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
              });
          } catch {
            return [];
          }
        },
      },
      {
        name: "status",
        description: "Show detailed memory system status",
        usage: "/memory status",
        example: "/memory status",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          await showMemoryStatus(ctx);
        },
      },
      {
        name: "toggle",
        description: "Toggle memory settings (memory_enabled, memory_forbidden_enabled)",
        usage: "/memory toggle [key]",
        example: "/memory toggle memory_enabled",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          await toggleMemory(ctx, args);
        },
        completion: async (ctx) => togglesForMode(modeToShort(ctx.mode)).map((t) => t.key),
      },
      {
        name: "open",
        description: "Show memory directory paths",
        usage: "/memory open",
        example: "/memory open",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          await openMemoryDir(ctx);
        },
      },
    ],
  };
}
