import { statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, parse, relative } from "node:path";

// ---------------------------------------------------------------------------
// Path display utilities (aligned with Claude Code's getDisplayPath)
// ---------------------------------------------------------------------------

/**
 * 格式化 /memory 中展示的路径，不修改实际用于文件操作的原始路径。
 *
 * Windows 路径可能分别来自 Node 前端和 Python 后端，盘符大小写不一致。
 * 此处只将展示盘符转为大写，路径分隔符、目录名和文件名均保持不变。
 */
export function formatMemoryPathForDisplay(filePath: string): string {
  // Windows 命名空间路径：\\?\d:\repo -> \\?\D:\repo
  const namespacedPath = filePath.replace(
    /^((?:\\\\\?\\|\/\/\?\/))([A-Za-z]):(?=[\\/]|$)/,
    (_match, prefix: string, drive: string) => `${prefix}${drive.toUpperCase()}:`,
  );

  // 普通 Windows 路径：d:\repo -> D:\repo
  return namespacedPath.replace(
    /^([A-Za-z]):(?=[\\/]|$)/,
    (_match, drive: string) => `${drive.toUpperCase()}:`,
  );
}

//
// 统一的路径展示工具：从 memory.ts 的 getDisplayPath 与 app-screen.ts 的
// mvDisplayPath 合并而来。二者原本各有侧重——
//   getDisplayPath: 用 path.relative() 精确计算,处理 Windows 跨盘/Unicode/
//                   大小写问题,且能产生 ../ 前缀路径(来自 projectDir 候选)。
//   mvDisplayPath:  纯前缀匹配,简单但无法产生 ../ 路径;空结果用 "." 表示
//                   当前目录(Unix 惯例)。
// 合并后取两者之长:保留 relative() 的稳健性 + 空路径→"." 的友好展示,
// 并接受可选 gitRoot 参数避免调用方重复计算。

/**
 * Convert an absolute path to the shortest display-friendly path.
 * Mirrors Claude Code's getDisplayPath logic — generates ALL candidate paths
 * and picks the shortest one:
 *
 *   Candidates:
 *   1. Relative from git root  (e.g. ".jiuwen/rules/foo.md")
 *   2. Relative from projectDir (e.g. "JIUWENSWARM.local.md", "../JIUWENSWARM.md")
 *   3. Tilde notation          (e.g. "~/.jiuwen/JIUWENSWARM.md")
 *
 *   Winner = shortest candidate.
 *   Empty candidate (file === base dir) is shown as "." (current directory).
 *
 * All output uses forward slashes. A direct parent keeps its ../ prefix; deeper
 * ancestors use a stable ~/... or absolute form to avoid long traversal chains.
 *
 * On Windows, THREE critical issues must be handled:
 * - path.relative() returns absolute paths for cross-drive paths — must discard.
 * - Case mismatch: getCurrentProjectDir() lowercases on Windows, backend doesn't.
 * - Unicode bug: Node.js path.relative() silently drops \ from multi-byte paths.
 *
 * @param filePath   Absolute path to the file/dir to display.
 * @param projectDir Current project directory (CWD).
 * @param gitRoot    Optional pre-computed git root. If omitted, discovered from
 *                   projectDir via findGitRoot(). Pass it in when the caller
 *                   already has it to avoid redundant filesystem walks.
 */
export function getDisplayPath(
  filePath: string,
  projectDir: string,
  gitRoot?: string | null,
): string {
  const fileSlashes = formatMemoryPathForDisplay(filePath).replace(/\\/g, "/");
  const fileNorm = process.platform === "win32" ? fileSlashes.toLowerCase() : fileSlashes;
  const homeDir = homedir();
  const homeDirSlashes = formatMemoryPathForDisplay(homeDir).replace(/\\/g, "/");
  const homeDirNorm = process.platform === "win32" ? homeDirSlashes.toLowerCase() : homeDirSlashes;

  // Collect all valid candidate paths, then pick the shortest
  const candidates: string[] = [];
  let projectRelativeParentDepth = 0;

  // Resolve gitRoot: use provided value or discover from projectDir
  const resolvedGitRoot = gitRoot !== undefined ? gitRoot : findGitRoot(projectDir);

  // Candidate 1: relative from git root (only if file is inside projectDir)
  // 守卫:仅当文件位于 projectDir 内部时才采用 git-root 相对路径候选。
  // 否则(文件在 projectDir 的父级/祖先目录)git-root 相对路径会丢掉 ../
  // 前缀,把父目录文件伪装成当前目录文件,误导用户。此时应只保留 candidate 2
  // 的 projectDir 相对路径(必带 ../ 前缀)。
  const fileInsideProject = isAncestorOrSelfDir(projectDir, filePath);
  if (resolvedGitRoot && fileInsideProject) {
    const gitRootSlashes = formatMemoryPathForDisplay(resolvedGitRoot).replace(/\\/g, "/");
    const gitRootNorm = process.platform === "win32" ? gitRootSlashes.toLowerCase() : gitRootSlashes;
    const projectDirSlashes = formatMemoryPathForDisplay(projectDir).replace(/\\/g, "/");
    const projectDirNorm = process.platform === "win32" ? projectDirSlashes.toLowerCase() : projectDirSlashes;
    // Only use git root as base if projectDir is inside the git repo
    if (projectDirNorm.startsWith(gitRootNorm + "/") || projectDirNorm === gitRootNorm) {
      const relFromGit = relative(gitRootNorm, fileNorm);
      // Discard cross-drive absolute paths from relative(); allow empty (file === gitRoot)
      if (!relFromGit.startsWith("/") && !/^[A-Za-z]:/.test(relFromGit)) {
        const display = relative(gitRootSlashes, fileSlashes).replace(/\\/g, "/");
        candidates.push(display);
      }
    }
  }

  // Candidate 2: relative from projectDir. Preserve "../" paths so files in
  // parent/ancestor directories are not reduced to an ambiguous basename.
  // Cross-drive results are still discarded because path.relative() returns
  // an absolute path for them on Windows.
  const projectDirSlashes = formatMemoryPathForDisplay(projectDir).replace(/\\/g, "/");
  const projectDirNorm = process.platform === "win32" ? projectDirSlashes.toLowerCase() : projectDirSlashes;
  const relFromProj = relative(projectDirNorm, fileNorm);
  if (!relFromProj.startsWith("/") && !/^[A-Za-z]:/.test(relFromProj)) {
    const display = relative(projectDirSlashes, fileSlashes).replace(/\\/g, "/");
    const parentTraversal = display.match(/^(?:\.\.\/)+/);
    projectRelativeParentDepth = parentTraversal ? parentTraversal[0].length / 3 : 0;
    candidates.push(display);
  }

  // Candidate 3: tilde notation (if file is in home directory)
  let tildePath: string | undefined;
  if (fileNorm.startsWith(homeDirNorm + "/") || fileNorm === homeDirNorm) {
    tildePath = "~" + fileSlashes.slice(homeDirSlashes.length);
    candidates.push(tildePath);
  }

  // Match the open tab's readable home-relative presentation. For ancestor
  // files under home, prefer ~/... over a long ../../../../... chain. If the
  // file is outside home, the explicit ../ form remains the fallback.
  if (projectRelativeParentDepth > 0 && tildePath) {
    return tildePath;
  }

  // When the file is outside home, a rooted absolute path is clearer than a
  // multi-level traversal chain. Preserve ../ only for the direct-parent case.
  if (projectRelativeParentDepth > 1) {
    return fileSlashes;
  }

  // Pick the shortest candidate; empty string means "current dir" → "."
  if (candidates.length > 0) {
    return candidates
      .map((c) => (c.length === 0 ? "." : c))
      .reduce((shortest, c) => (c.length < shortest.length ? c : shortest));
  }

  // Fallback: file name only (avoid overly long absolute paths)
  const baseName = fileSlashes.split("/").pop() ?? fileSlashes;
  return baseName || fileSlashes;
}

/**
 * Find the git repository root directory.
 * Walks upward from cwd looking for a .git directory or file (worktree/submodule).
 * Returns the git root path if found, or null if not in a git repo.
 * Mirrors Claude Code's findGitRoot logic.
 */
export function findGitRoot(cwd: string): string | null {
  let current = cwd;
  const root = parse(current).root;
  while (current !== root) {
    try {
      const gitPath = join(current, ".git");
      const stat = statSync(gitPath);
      // .git can be a directory (regular repo) or file (worktree/submodule)
      if (stat.isDirectory() || stat.isFile()) {
        return current;
      }
    } catch {
      // .git doesn't exist at this level, continue walking up
    }
    current = dirname(current);
  }
  return null;
}

/**
 * 判断 ancestor 是否是 target 的祖先目录或本身(Windows 大小写不敏感)。
 *
 * 用于识别"文件位于 projectDir 的上级目录"这一场景——后端
 * `_validate_edit_path` 只白名单 project_dir 单层,会拒绝编辑 projectDir
 * 祖先目录里的 JIUWENSWARM.md / JIUWENSWARM.local.md;同时 getDisplayPath
 * 在父目录是 git root 时会把这类文件显示成无 ../ 的当前目录文件(伪装)。
 * 两处都需要用此函数识别并特殊处理。
 */
export function isAncestorOrSelfDir(ancestor: string, target: string): boolean {
  const a = ancestor.replace(/\\/g, "/").replace(/\/$/, "");
  const t = target.replace(/\\/g, "/").replace(/\/$/, "");
  const aNorm = process.platform === "win32" ? a.toLowerCase() : a;
  const tNorm = process.platform === "win32" ? t.toLowerCase() : t;
  return aNorm === tNorm || tNorm.startsWith(aNorm + "/");
}
