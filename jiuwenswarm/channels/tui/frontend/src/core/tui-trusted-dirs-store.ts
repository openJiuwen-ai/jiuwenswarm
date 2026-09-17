import { readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";

import { loadTuiConfig, saveTuiConfig } from "./tui-config-store.js";

/**
 * Trusted directories storage — project-scoped.
 *
 * Each project (identified by its cwd) maintains its own trusted dirs list.
 * Stored in ~/.jiuwenswarm-tui/config.json as `trustedDirs: {projectDir: [dir1, dir2]}`.
 */
let _trustedDirsByProject: Record<string, string[]> | null = null;

/**
 * Normalize a path for comparison (handle trailing separators, case on Windows)
 */
function normalizePath(path: string): string {
  const trimmed = path.trim();
  if (!trimmed) {
    return "";
  }
  // Expand ~ to home directory before resolving
  let expanded = trimmed;
  if (expanded === "~") {
    expanded = homedir();
  } else if (expanded.startsWith("~/")) {
    expanded = homedir() + expanded.slice(1);
  }
  const resolved = resolve(expanded);
  // On Windows, normalize case
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

/**
 * Migrate legacy flat `trustedDirs: string[]` format to project-scoped format.
 * The first element in the flat array is treated as the project cwd,
 * and the entire list becomes that project's trusted dirs.
 */
function migrateLegacyFormat(config: Record<string, unknown>): Record<string, string[]> {
  const raw = config.trustedDirs;
  if (Array.isArray(raw) && raw.length > 0) {
    // Use the first dir as project cwd, all dirs become that project's list
    const projectCwd = normalizePath(raw[0]);
    const migrated: Record<string, string[]> = {};
    if (projectCwd) {
      const normalizedList = raw.map(d => normalizePath(d)).filter(d => d);
      migrated[projectCwd] = normalizedList;
    }
    // Persist the migrated format immediately
    saveTuiConfig({ trustedDirs: migrated });
    return migrated;
  }
  // Empty array or not an array → empty project-scoped map
  const empty: Record<string, string[]> = {};
  saveTuiConfig({ trustedDirs: empty });
  return empty;
}

/**
 * Ensure _trustedDirsByProject is loaded from persisted config.
 */
function ensureLoaded(): void {
  if (_trustedDirsByProject === null) {
    const config = loadTuiConfig();
    const raw = config.trustedDirs;
    if (Array.isArray(raw)) {
      _trustedDirsByProject = migrateLegacyFormat(config as Record<string, unknown>);
    } else if (raw && typeof raw === "object") {
      _trustedDirsByProject = raw as Record<string, string[]>;
    } else {
      _trustedDirsByProject = {};
    }
  }
}

/**
 * Persist current _trustedDirsByProject to config file.
 */
function persist(): void {
  saveTuiConfig({ trustedDirs: _trustedDirsByProject! });
}

/**
 * Get the project key (normalized cwd) used for scoping.
 * Defaults to process.cwd() but can be overridden.
 */
let _currentProjectDir: string | null = null;
let _currentCwd: string | null = null;

/**
 * Override the project directory used for scoping trusted dirs.
 * The path is normalized to an absolute path.
 * This is a session-level override — not persisted, because on restart
 * the project scope must match the user's actual cwd.
 */
export function setCurrentProjectDir(dir: string): void {
  _currentProjectDir = normalizePath(dir);
}

/**
 * Get the current project scope directory (absolute path).
 */
export function getCurrentProjectDir(): string {
  return getProjectKey();
}

/**
 * Override the dynamic cwd sent with each request.
 */
export function setCurrentCwd(dir: string): void {
  _currentCwd = normalizePath(dir);
}

/**
 * Get the dynamic cwd for runtime execution.
 */
export function getCurrentCwd(): string {
  if (_currentCwd) {
    return _currentCwd;
  }
  return getProjectKey();
}

/**
 * Get the current project directory key (normalized absolute path).
 * Priority: in-memory _currentProjectDir > process.cwd()
 */
function getProjectKey(): string {
  if (_currentProjectDir) {
    return _currentProjectDir;
  }
  return normalizePath(process.cwd());
}

/**
 * Get trusted directories for the current project only.
 * Returns empty array if no dirs set for this project.
 */
export function getTrustedDirs(): string[] {
  ensureLoaded();
  const key = getProjectKey();
  const projectDirs = _trustedDirsByProject![key] || [];
  const validDirs = projectDirs.filter((dir) => validateDirPath(dir) === "valid");
  if (validDirs.length !== projectDirs.length) {
    if (validDirs.length > 0) {
      _trustedDirsByProject![key] = validDirs;
    } else {
      delete _trustedDirsByProject![key];
    }
    persist();
  }
  return [...validDirs];
}

/**
 * Add a trusted directory to the current project.
 * @param path - Directory path to add (must be a folder, not a file)
 * @returns "added" if added, "exists" if already trusted, "not_found" if path doesn't exist, "invalid" if invalid path or not a directory, "no_access" if permission denied
 */
export function addTrustedDir(path: string): "added" | "exists" | "not_found" | "invalid" | "no_access" {
  ensureLoaded();
  const normalized = normalizePath(path);
  if (!normalized) {
    return "invalid";
  }
  try {
    const stats = statSync(normalized);
    if (!stats.isDirectory()) {
      return "invalid";
    }
  } catch (err: any) {
    if (err.code === "EACCES" || err.code === "EPERM") {
      return "no_access";
    }
    if (err.code === "ENOENT") {
      return "not_found";
    }
    return "invalid";
  }
  const access = checkDirAccess(normalized);
  if (access !== "valid") {
    return access;
  }
  const key = getProjectKey();
  const projectDirs = _trustedDirsByProject![key] || [];
  if (projectDirs.includes(normalized)) {
    return "exists";
  }
  projectDirs.push(normalized);
  _trustedDirsByProject![key] = projectDirs;
  persist();
  return "added";
}

/**
 * Check that a normalized directory path is accessible (readable).
 * @returns "valid" if accessible, "no_access" if permission denied, "invalid" for other errors
 */
function checkDirAccess(normalized: string): "valid" | "no_access" | "invalid" {
  try {
    readdirSync(normalized);
  } catch (err: any) {
    if (err.code === "EACCES" || err.code === "EPERM") {
      return "no_access";
    }
    return "invalid";
  }
  return "valid";
}

/**
 * Validate a directory path without modifying trusted dirs state.
 * @param path - Directory path to validate
 * @returns "valid" if accessible directory, "not_found" if path doesn't exist, "invalid" if not a directory, "no_access" if permission denied
 */
export function validateDirPath(path: string): "valid" | "not_found" | "invalid" | "no_access" {
  const normalized = normalizePath(path);
  if (!normalized) {
    return "invalid";
  }
  try {
    const stats = statSync(normalized);
    if (!stats.isDirectory()) {
      return "invalid";
    }
  } catch (err: any) {
    if (err.code === "EACCES" || err.code === "EPERM") {
      return "no_access";
    }
    if (err.code === "ENOENT") {
      return "not_found";
    }
    return "invalid";
  }
  const access = checkDirAccess(normalized);
  if (access !== "valid") {
    return access;
  }
  return "valid";
}

/**
 * Reset trusted dirs for the current project and set a single path.
 * @param path - Directory path to set as the only trusted dir (must be a folder, not a file)
 * @returns "set" if set successfully, "not_found" if path doesn't exist, "invalid" if invalid path or not a directory, "no_access" if permission denied
 */
export function setTrustedDir(path: string): "set" | "not_found" | "invalid" | "no_access" {
  ensureLoaded();
  const normalized = normalizePath(path);
  if (!normalized) {
    return "invalid";
  }
  try {
    const stats = statSync(normalized);
    if (!stats.isDirectory()) {
      return "invalid";
    }
  } catch (err: any) {
    if (err.code === "EACCES" || err.code === "EPERM") {
      return "no_access";
    }
    if (err.code === "ENOENT") {
      return "not_found";
    }
    return "invalid";
  }
  const access = checkDirAccess(normalized);
  if (access !== "valid") {
    return access;
  }
  const key = getProjectKey();
  _trustedDirsByProject![key] = [normalized];
  persist();
  return "set";
}

/**
 * Remove a trusted directory from the current project.
 * @param path - Directory path to remove
 * @returns true if removed, false if not found in this project's trusted dirs
 */
export function removeTrustedDir(path: string): boolean {
  ensureLoaded();
  const normalized = normalizePath(path);
  if (!normalized) {
    return false;
  }
  const key = getProjectKey();
  const projectDirs = _trustedDirsByProject![key] || [];
  const index = projectDirs.indexOf(normalized);
  if (index === -1) {
    return false;
  }
  projectDirs.splice(index, 1);
  if (projectDirs.length === 0) {
    delete _trustedDirsByProject![key];
  } else {
    _trustedDirsByProject![key] = projectDirs;
  }
  persist();
  return true;
}

/**
 * Clear all trusted directories for the current project.
 */
export function clearTrustedDirs(): void {
  ensureLoaded();
  const key = getProjectKey();
  delete _trustedDirsByProject![key];
  persist();
}

/**
 * Check if a path is a trusted directory for the current project.
 * @param path - Directory path to check
 * @returns true if trusted for the current project
 */
export function isTrustedDir(path: string): boolean {
  ensureLoaded();
  const normalized = normalizePath(path);
  if (!normalized) {
    return false;
  }
  const key = getProjectKey();
  const projectDirs = _trustedDirsByProject![key] || [];
  return projectDirs.includes(normalized);
}

/**
 * Get the default workspace path.
 */
export function getDefaultWorkspacePath(): string {
  return resolve(homedir(), ".jiuwenswarm/agent/workspace");
}
