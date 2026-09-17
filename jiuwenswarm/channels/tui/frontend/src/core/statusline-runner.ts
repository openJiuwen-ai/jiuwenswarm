import { execFile, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { win32 } from "node:path";

export interface StatusLineShellInvocation {
  executable: string;
  args: string[];
}

type PathExists = (path: string) => boolean;

function gitBashCandidates(env: NodeJS.ProcessEnv): string[] {
  const candidates = [env.JIUWENSWARM_GIT_BASH_PATH, env.CLAUDE_CODE_GIT_BASH_PATH];
  const pathDirs = (env.Path ?? env.PATH ?? "").split(";").filter(Boolean);
  for (const dir of pathDirs) {
    candidates.push(win32.join(dir, "bash.exe"));
  }
  if (env.ProgramFiles) candidates.push(win32.join(env.ProgramFiles, "Git", "bin", "bash.exe"));
  if (env["ProgramFiles(x86)"]) {
    candidates.push(win32.join(env["ProgramFiles(x86)"]!, "Git", "bin", "bash.exe"));
  }
  if (env.LOCALAPPDATA) {
    candidates.push(win32.join(env.LOCALAPPDATA, "Programs", "Git", "bin", "bash.exe"));
  }
  return candidates.filter((candidate): candidate is string => !!candidate);
}

export function findGitBash(
  env: NodeJS.ProcessEnv = process.env,
  pathExists: PathExists = existsSync,
): string | null {
  return gitBashCandidates(env).find((candidate) => pathExists(candidate)) ?? null;
}

/** Match Claude Code: Git Bash on Windows when installed, otherwise PowerShell. */
export function buildStatusLineShellInvocation(
  command: string,
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
  pathExists: PathExists = existsSync,
): StatusLineShellInvocation {
  if (platform !== "win32") {
    return { executable: "sh", args: ["-c", command] };
  }

  const gitBash = findGitBash(env, pathExists);
  if (gitBash) {
    return { executable: gitBash, args: ["-c", command] };
  }
  return {
    executable: "powershell.exe",
    args: ["-NoProfile", "-NonInteractive", "-Command", command],
  };
}

export function runStatusLineCommand(
  command: string,
  jsonInput: string,
  cwd: string,
  callback: (error: Error | null, stdout: string) => void,
): ChildProcess {
  const invocation = buildStatusLineShellInvocation(command);
  const child = execFile(
    invocation.executable,
    invocation.args,
    { timeout: 3_000, maxBuffer: 10_240, cwd },
    (error, stdout) => callback(error, stdout),
  );
  child.stdin?.end(jsonInput);
  return child;
}
