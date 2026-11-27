import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

export interface ProjectInfo {
  root: string;
  hasClaudeMd: boolean;
  languages: string[];
  frameworks: string[];
  buildTools: string[];
  testFrameworks: string[];
  lintTools: string[];
  packageManagers: string[];
  fileCounts: Record<string, number>;
  topLevelFiles: string[];
  topLevelDirs: string[];
  aiToolConfigs: string[];
  gitTracked: boolean;
  isMonorepo: boolean;
}

/** Scanning rules for detecting project characteristics. */
const PROJECT_CONFIG_FILES: Record<string, { file: string; category: string }> = {
  node: { file: "package.json", category: "languages" },
  python: { file: "pyproject.toml", category: "languages" },
  "python (setup.py)": { file: "setup.py", category: "languages" },
  "python (requirements.txt)": { file: "requirements.txt", category: "languages" },
  rust: { file: "Cargo.toml", category: "languages" },
  go: { file: "go.mod", category: "languages" },
  java: { file: "pom.xml", category: "languages" },
  "java (gradle)": { file: "build.gradle", category: "languages" },
  "java (gradle kotlin)": { file: "build.gradle.kts", category: "languages" },
  ruby: { file: "Gemfile", category: "languages" },
  php: { file: "composer.json", category: "languages" },
  swift: { file: "Package.swift", category: "languages" },
  kotlin: { file: "build.gradle.kts", category: "languages" },
  scala: { file: "build.sbt", category: "languages" },
  dart: { file: "pubspec.yaml", category: "languages" },
  dotnet: { file: "*.csproj", category: "languages" },
  terraform: { file: "*.tf", category: "languages" },
};

const BUILD_TOOL_FILES: Record<string, string> = {
  "Makefile": "make",
  "CMakeLists.txt": "cmake",
  "meson.build": "meson",
  "package.json": "npm/yarn/pnpm/bun",
  "pyproject.toml": "uv/pip/poetry",
  "Cargo.toml": "cargo",
  "go.mod": "go modules",
  "pom.xml": "maven",
  "build.gradle": "gradle",
  "build.gradle.kts": "gradle (kotlin)",
  "build.sbt": "sbt",
  "Gemfile": "bundler",
  "composer.json": "composer",
  "pubspec.yaml": "pub",
};

const TEST_FRAMEWORK_INDICATORS: Record<string, string> = {
  jest: "jest.config",
  vitest: "vitest.config",
  mocha: ".mocharc",
  pytest: "pytest.ini",
  "unittest (python)": "tests/",
  cargo_test: "Cargo.toml",
  go_test: "_test.go",
  rspec: "spec/",
};

const LINT_TOOL_INDICATORS: Record<string, string> = {
  eslint: ".eslintrc",
  prettier: ".prettierrc",
  biome: "biome.json",
  ruff: "ruff.toml",
  flake8: ".flake8",
  black: "pyproject.toml",
  clippy: "Cargo.toml",
  golangci: ".golangci.yml",
  rubocop: ".rubocop.yml",
  phpstan: "phpstan.neon",
};

const AI_TOOL_CONFIG_FILES = [
  ".cursorrules",
  ".cursor/rules",
  ".github/copilot-instructions.md",
  ".github/copilot-instructions",
  ".windsurfrules",
  ".aider.input.history",
  ".ai/",
  "AGENTS.md",
];

const MONOREPO_INDICATORS = [
  "lerna.json",
  "pnpm-workspace.yaml",
  "turbo.json",
  "nx.json",
  "bunfig.toml",
];

/**
 * Scan a project directory and return structured information.
 */
export function scanProject(root: string): ProjectInfo {
  const languages: string[] = [];
  const frameworks: string[] = [];
  const buildTools: string[] = [];
  const testFrameworks: string[] = [];
  const lintTools: string[] = [];
  const packageManagers: string[] = [];
  const fileCounts: Record<string, number> = {};
  const aiToolConfigs: string[] = [];

  const topLevelEntries = safeReaddir(root);
  const topLevelFiles = topLevelEntries.filter((e) => isFile(root, e));
  const topLevelDirs = topLevelEntries.filter((e) => isDir(root, e));

  // Detect languages from config files
  for (const [name, { file, category }] of Object.entries(PROJECT_CONFIG_FILES)) {
    if (file.includes("*")) {
      // Glob pattern - check for matching files
      const ext = file.replace("*", "");
      if (topLevelFiles.some((f) => f.endsWith(ext))) {
        languages.push(name);
      }
    } else if (topLevelFiles.includes(file)) {
      languages.push(name);
    }
  }

  // Detect build tools
  for (const [file, tool] of Object.entries(BUILD_TOOL_FILES)) {
    if (topLevelFiles.includes(file)) {
      buildTools.push(tool);
    }
  }

  // Detect test frameworks
  for (const [framework, indicator] of Object.entries(TEST_FRAMEWORK_INDICATORS)) {
    if (indicator.endsWith("/")) {
      if (topLevelDirs.includes(indicator)) {
        testFrameworks.push(framework);
      }
    } else if (topLevelFiles.some((f) => f.includes(indicator))) {
      testFrameworks.push(framework);
    }
  }

  // Detect lint tools (check both root files and deeper config)
  for (const [tool, indicator] of Object.entries(LINT_TOOL_INDICATORS)) {
    if (topLevelFiles.includes(indicator)) {
      lintTools.push(tool);
    }
  }

  // Detect package managers
  if (topLevelFiles.includes("package-lock.json")) packageManagers.push("npm");
  if (topLevelFiles.includes("yarn.lock")) packageManagers.push("yarn");
  if (topLevelFiles.includes("pnpm-lock.yaml")) packageManagers.push("pnpm");
  if (topLevelFiles.includes("bun.lockb") || topLevelFiles.includes("bun.lock")) packageManagers.push("bun");
  if (topLevelFiles.includes("uv.lock")) packageManagers.push("uv");
  if (topLevelFiles.includes("poetry.lock")) packageManagers.push("poetry");
  if (topLevelFiles.includes("Pipfile.lock")) packageManagers.push("pipenv");

  // Count file types
  const extensions = new Set<string>();
  for (const entry of topLevelEntries) {
    const ext = getExtension(entry);
    if (ext) extensions.add(ext);
  }
  // Scan subdirectories for file counts
  for (const dir of topLevelDirs.filter((d) => !d.startsWith(".") && d !== "node_modules")) {
    scanDirFileCounts(join(root, dir), fileCounts, 2);
  }

  // Detect AI tool configs
  for (const aiFile of AI_TOOL_CONFIG_FILES) {
    if (aiFile.endsWith("/")) {
      if (isDir(root, aiFile.slice(0, -1))) {
        aiToolConfigs.push(aiFile);
      }
    } else if (existsSync(join(root, aiFile))) {
      aiToolConfigs.push(aiFile);
    }
  }

  // Check git
  const gitTracked = existsSync(join(root, ".git"));

  // Check monorepo
  const isMonorepo = MONOREPO_INDICATORS.some((f) => topLevelFiles.includes(f));

  // Check CLAUDE.md
  const hasClaudeMd =
    existsSync(join(root, "CLAUDE.md")) ||
    existsSync(join(root, "CLAUDE.local.md"));

  // Read package.json for framework detection
  if (topLevelFiles.includes("package.json")) {
    const pkg = readPackageJson(root);
    if (pkg) {
      const allDeps = {
        ...pkg.dependencies,
        ...pkg.devDependencies,
      };
      const depNames = Object.keys(allDeps);

      // Framework detection
      if (depNames.some((d) => d.includes("react") || d.includes("next"))) frameworks.push("React");
      if (depNames.some((d) => d.includes("vue"))) frameworks.push("Vue");
      if (depNames.some((d) => d.includes("svelte"))) frameworks.push("Svelte");
      if (depNames.some((d) => d.includes("angular"))) frameworks.push("Angular");
      if (depNames.some((d) => d.includes("django"))) frameworks.push("Django");
      if (depNames.some((d) => d.includes("fastapi"))) frameworks.push("FastAPI");
      if (depNames.some((d) => d.includes("flask"))) frameworks.push("Flask");
      if (depNames.some((d) => d.includes("express"))) frameworks.push("Express");
      if (depNames.some((d) => d.includes("tailwind"))) frameworks.push("TailwindCSS");
      if (depNames.some((d) => d.includes("vite"))) frameworks.push("Vite");
      if (depNames.some((d) => d.includes("webpack"))) frameworks.push("Webpack");
      if (depNames.some((d) => d.includes("typescript") || pkg.devDependencies?.typescript)) {
        if (!languages.includes("typescript")) {
          frameworks.push("TypeScript");
        }
      }
    }
  }

  // Read pyproject.toml for Python framework detection
  if (topLevelFiles.includes("pyproject.toml")) {
    const content = readFile(join(root, "pyproject.toml"));
    if (content) {
      if (content.includes("django")) frameworks.push("Django");
      if (content.includes("fastapi")) frameworks.push("FastAPI");
      if (content.includes("flask")) frameworks.push("Flask");
      if (content.includes("pytest")) testFrameworks.push("pytest");
    }
  }

  return {
    root,
    hasClaudeMd,
    languages,
    frameworks,
    buildTools,
    testFrameworks,
    lintTools,
    packageManagers,
    fileCounts,
    topLevelFiles,
    topLevelDirs,
    aiToolConfigs,
    gitTracked,
    isMonorepo,
  };
}

/**
 * Generate CLAUDE.md content from project information.
 */
export function generateClaudeMd(info: ProjectInfo): string {
  const lines: string[] = [];

  lines.push("# CLAUDE.md");
  lines.push("");
  lines.push(
    "This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.",
  );
  lines.push("");

  // Project Overview
  lines.push("## Project Overview");
  lines.push("");
  if (info.languages.length > 0) {
    lines.push(`Primary languages: ${info.languages.join(", ")}`);
  }
  if (info.frameworks.length > 0) {
    lines.push(`Frameworks: ${info.frameworks.join(", ")}`);
  }
  if (info.isMonorepo) {
    lines.push("This is a monorepo.");
  }
  lines.push("");

  // Directory Structure
  lines.push("## Directory Structure");
  lines.push("");
  for (const dir of info.topLevelDirs.filter((d) => !d.startsWith("."))) {
    lines.push(`- \`${dir}/\` - ${inferDirDescription(dir)}`);
  }
  lines.push("");

  // Build/Lint/Test Commands
  const hasNpm = info.buildTools.some((t) => t.includes("npm"));
  const hasPython = info.buildTools.some((t) => t.includes("uv") || t.includes("pip"));

  lines.push("## Build/Lint/Test Commands");
  lines.push("");
  if (hasNpm) {
    lines.push("```bash");
    lines.push("# Install dependencies");
    lines.push("npm install");
    lines.push("");
    lines.push("# Build");
    lines.push("npm run build");
    lines.push("");
    lines.push("# Test");
    lines.push("npm test");
    lines.push("");
    lines.push("# Lint");
    lines.push("npm run lint");
    lines.push("```");
    lines.push("");
  }
  if (hasPython) {
    lines.push("```bash");
    lines.push("# Install dependencies");
    lines.push("uv sync");
    lines.push("");
    lines.push("# Test");
    lines.push("uv run pytest");
    lines.push("");
    if (info.lintTools.includes("ruff")) {
      lines.push("# Lint");
      lines.push("uv run ruff check .");
    }
    lines.push("```");
    lines.push("");
  }
  if (!hasNpm && !hasPython && info.buildTools.length === 0) {
    lines.push("_(Build commands not yet configured. Add them here.)_");
    lines.push("");
  }

  // Code Style
  lines.push("## Code Style");
  lines.push("");
  if (info.lintTools.length > 0) {
    lines.push(`Linting/formatting tools: ${info.lintTools.join(", ")}`);
    if (info.lintTools.includes("prettier")) {
      lines.push("- Format: `npx prettier --write .`");
    }
    if (info.lintTools.includes("ruff")) {
      lines.push("- Lint: `ruff check .`; Format: `ruff format .`");
    }
    if (info.lintTools.includes("eslint")) {
      lines.push("- Lint: `npx eslint .`");
    }
  } else {
    lines.push("_(Code style not yet configured. Add linting/formatting rules here.)_");
  }
  lines.push("");

  // Agent-Specific Instructions
  lines.push("## Important Conventions");
  lines.push("");
  lines.push("- Before making any changes, read relevant source files first");
  lines.push("- Use all available tools to ensure changes are correct before submitting");
  lines.push("- Never remove or modify existing tests unless explicitly requested");
  lines.push("- Always write safe, secure, and correct code");
  lines.push("");

  // Existing AI Configs
  if (info.aiToolConfigs.length > 0) {
    lines.push("## Existing AI Tool Configurations");
    lines.push("");
    lines.push("The following AI tool config files were detected and may contain relevant context:");
    lines.push("");
    for (const aiFile of info.aiToolConfigs) {
      lines.push(`- \`${aiFile}\``);
    }
    lines.push("");
  }

  return lines.join("\n");
}

// --- Helper functions ---

function safeReaddir(dir: string): string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}

function isFile(root: string, name: string): boolean {
  try {
    return statSync(join(root, name)).isFile();
  } catch {
    return false;
  }
}

function isDir(root: string, name: string): boolean {
  try {
    return statSync(join(root, name)).isDirectory();
  } catch {
    return false;
  }
}

function getExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function scanDirFileCounts(
  dir: string,
  counts: Record<string, number>,
  remainingDepth: number,
): void {
  if (remainingDepth <= 0) return;
  const entries = safeReaddir(dir);
  for (const entry of entries) {
    if (entry === "node_modules" || entry === ".git" || entry.startsWith(".")) continue;
    const fullPath = join(dir, entry);
    try {
      const stat = statSync(fullPath);
      if (stat.isFile()) {
        const ext = getExtension(entry);
        counts[ext] = (counts[ext] ?? 0) + 1;
      } else if (stat.isDirectory()) {
        scanDirFileCounts(fullPath, counts, remainingDepth - 1);
      }
    } catch {
      // Skip inaccessible files
    }
  }
}

function readPackageJson(root: string): {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
} | null {
  const content = readFile(join(root, "package.json"));
  if (!content) return null;
  try {
    return JSON.parse(content);
  } catch {
    return null;
  }
}

function readFile(path: string): string | null {
  try {
    return readFileSync(path, "utf-8");
  } catch {
    return null;
  }
}

function inferDirDescription(name: string): string {
  const descriptions: Record<string, string> = {
    src: "source code",
    lib: "library code",
    app: "application code",
    packages: "monorepo packages",
    apps: "monorepo applications",
    test: "tests",
    tests: "tests",
    __test__: "tests",
    __tests__: "tests",
    spec: "test specs",
    docs: "documentation",
    doc: "documentation",
    scripts: "build/utility scripts",
    bin: "executable scripts",
    config: "configuration files",
    conf: "configuration files",
    public: "static assets",
    static: "static assets",
    assets: "static assets",
    components: "UI components",
    pages: "page components",
    routes: "route definitions",
    api: "API endpoints",
    services: "service layer",
    utils: "utility functions",
    helpers: "helper functions",
    types: "TypeScript type definitions",
    models: "data models",
    schemas: "data schemas",
    migrations: "database migrations",
    docker: "Docker configuration",
    ".github": "GitHub workflows and config",
  };
  return descriptions[name] || "project files";
}
