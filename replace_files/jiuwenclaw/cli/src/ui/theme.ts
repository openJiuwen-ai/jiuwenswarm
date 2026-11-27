import { Chalk } from "chalk";
import type { EditorTheme, MarkdownTheme, SelectListTheme } from "@mariozechner/pi-tui";

export const chalk = new Chalk({ level: 3 });

export type ThemeName = "system" | "dark" | "light";
export type AccentColorName = "default" | "blue" | "green" | "pink" | "purple" | "red" | "yellow";

const THEME_OPTIONS: readonly ThemeName[] = ["system", "dark", "light"] as const;
const ACCENT_OPTIONS: readonly AccentColorName[] = [
  "default",
  "blue",
  "green",
  "pink",
  "purple",
  "red",
  "yellow",
] as const;

type ThemeDefinition = {
  textPrimary: string;
  textSecondary: string;
  textDim: string;
  textSubtle: string;
  textAccent: string;
  textUser: string;
  textAssistant: string;
  textThinking: string;
  textSystem: string;
  textInfo: string;
  textTool: string;
  statusSuccess: string;
  statusError: string;
  statusWarning: string;
  statusInfo: string;
  borderPanel: string;
  borderQuestion: string;
  surfaceUserBg: string;
  surfaceUserFg: string;
  markdownHeading: string;
  markdownCode: string;
  markdownCodeBlock: string;
  diffAddBg: string;
  diffAddFg: string;
  diffRemoveBg: string;
  diffRemoveFg: string;
  diffContextBg: string;
  diffContextFg: string;
};

const THEME_DEFINITIONS: Record<"light" | "dark", ThemeDefinition> = {
  light: {
    textPrimary: "#000000",
    textSecondary: "#d77757",
    textDim: "#666666",
    textSubtle: "#afafaf",
    textAccent: "#5769f7",
    textUser: "#2563eb",
    textAssistant: "#000000",
    textThinking: "#966c1e",
    textSystem: "#666666",
    textInfo: "#5769f7",
    textTool: "#5769f7",
    statusSuccess: "#2c7a39",
    statusError: "#ab2b3f",
    statusWarning: "#966c1e",
    statusInfo: "#5769f7",
    borderPanel: "#999999",
    borderQuestion: "#966c1e",
    surfaceUserBg: "#f0f0f0",
    surfaceUserFg: "#000000",
    markdownHeading: "#d77757",
    markdownCode: "#5769f7",
    markdownCodeBlock: "#000000",
    diffAddBg: "#c7e1cb",
    diffAddFg: "#2f9d44",
    diffRemoveBg: "#fdd2d8",
    diffRemoveFg: "#d1454b",
    diffContextBg: "#f0f0f0",
    diffContextFg: "#666666",
  },
  dark: {
    textPrimary: "#ffffff",
    textSecondary: "#d77757",
    textDim: "#999999",
    textSubtle: "#505050",
    textAccent: "#b1b9f9",
    textUser: "#7ab4e8",
    textAssistant: "#ffffff",
    textThinking: "#ffc107",
    textSystem: "#999999",
    textInfo: "#b1b9f9",
    textTool: "#b1b9f9",
    statusSuccess: "#4eba65",
    statusError: "#ff6b80",
    statusWarning: "#ffc107",
    statusInfo: "#b1b9f9",
    borderPanel: "#888888",
    borderQuestion: "#ffc107",
    surfaceUserBg: "#373737",
    surfaceUserFg: "#ffffff",
    markdownHeading: "#d77757",
    markdownCode: "#b1b9f9",
    markdownCodeBlock: "#ffffff",
    diffAddBg: "#47584a",
    diffAddFg: "#38a660",
    diffRemoveBg: "#69484d",
    diffRemoveFg: "#b3596b",
    diffContextBg: "#373737",
    diffContextFg: "#999999",
  },
};

const ACCENT_COLORS: Record<Exclude<AccentColorName, "default">, string> = {
  blue: "#2563eb",
  green: "#16a34a",
  pink: "#db2777",
  purple: "#7c3aed",
  red: "#dc2626",
  yellow: "#ca8a04",
};

let currentThemeName: ThemeName = "system";
let currentAccentColor: AccentColorName = "default";

// TODO: Persist theme/accent across CLI restarts after the broader config/settings story is finalized.

function detectSystemTheme(): "light" | "dark" {
  const colorfgbg = process.env.COLORFGBG;
  if (colorfgbg) {
    const parts = colorfgbg.split(";");
    const bg = Number.parseInt(parts[parts.length - 1] ?? "", 10);
    if (Number.isFinite(bg)) {
      return bg >= 0 && bg <= 6 ? "dark" : "light";
    }
  }

  // 2. Check Windows Terminal session indicator
  // Windows Terminal defaults to dark theme
  if (process.env.WT_SESSION) {
    return "dark";
  }

  // 3. Check for common light-themed terminals
  if (process.env.TERM_PROGRAM === "Apple_Terminal") {
    return "light";
  }

  // 4. Windows PowerShell/CMD typically use dark backgrounds
  // Check for Windows platform with no light terminal indicators
  if (process.platform === "win32") {
    // PowerShell and CMD on Windows usually have dark blue/black backgrounds
    return "dark";
  }

  // 5. Default to dark for most modern terminals
  return "dark";
}

function getResolvedThemeName(): "light" | "dark" {
  return currentThemeName === "system" ? detectSystemTheme() : currentThemeName;
}

function getThemeDefinition(): ThemeDefinition {
  return THEME_DEFINITIONS[getResolvedThemeName()];
}

function getAccentHex(): string {
  if (currentAccentColor === "default") {
    return getThemeDefinition().textAccent;
  }
  return ACCENT_COLORS[currentAccentColor];
}

export function getThemeOptions(): readonly ThemeName[] {
  return THEME_OPTIONS;
}

export function getAccentColorOptions(): readonly AccentColorName[] {
  return ACCENT_OPTIONS;
}

export function getCurrentThemeName(): ThemeName {
  return currentThemeName;
}

export function getCurrentAccentColor(): AccentColorName {
  return currentAccentColor;
}

export function setCurrentThemeName(theme: ThemeName): void {
  currentThemeName = theme;
}

export function setCurrentAccentColor(color: AccentColorName): void {
  currentAccentColor = color;
}

export const palette = {
  text: {
    primary: (value: string) => chalk.hex(getThemeDefinition().textPrimary)(value),
    secondary: (value: string) => chalk.hex(getThemeDefinition().textSecondary)(value),
    accent: (value: string) => chalk.hex(getAccentHex())(value),
    dim: (value: string) => chalk.hex(getThemeDefinition().textDim)(value),
    subtle: (value: string) => chalk.hex(getThemeDefinition().textSubtle)(value),
    user: (value: string) => chalk.hex(getThemeDefinition().textUser)(value),
    assistant: (value: string) => chalk.hex(getThemeDefinition().textAssistant)(value),
    thinking: (value: string) => chalk.hex(getThemeDefinition().textThinking)(value),
    system: (value: string) => chalk.hex(getThemeDefinition().textSystem)(value),
    info: (value: string) => chalk.hex(getThemeDefinition().textInfo)(value),
    tool: (value: string) => chalk.hex(getThemeDefinition().textTool)(value),
  },
  surface: {
    user: (value: string) =>
      chalk.bgHex(getThemeDefinition().surfaceUserBg).hex(getThemeDefinition().surfaceUserFg)(
        value,
      ),
  },
  status: {
    success: (value: string) => chalk.hex(getThemeDefinition().statusSuccess)(value),
    error: (value: string) => chalk.hex(getThemeDefinition().statusError)(value),
    warning: (value: string) => chalk.hex(getThemeDefinition().statusWarning)(value),
    info: (value: string) => chalk.hex(getThemeDefinition().statusInfo)(value),
  },
  diff: {
    add: (value: string) =>
      chalk.bgHex(getThemeDefinition().diffAddBg).hex(getThemeDefinition().diffAddFg)(value),
    remove: (value: string) =>
      chalk.bgHex(getThemeDefinition().diffRemoveBg).hex(getThemeDefinition().diffRemoveFg)(value),
    context: (value: string) =>
      chalk.bgHex(getThemeDefinition().diffContextBg).hex(getThemeDefinition().diffContextFg)(
        value,
      ),
  },
  border: {
    panel: (value: string) => chalk.hex(getThemeDefinition().borderPanel)(value),
    active: (value: string) => chalk.hex(getAccentHex())(value),
    question: (value: string) => chalk.hex(getThemeDefinition().borderQuestion)(value),
  },
};

export const selectListTheme: SelectListTheme = {
  selectedPrefix: (value: string) => chalk.hex(getAccentHex())(value),
  selectedText: (value: string) => chalk.bold.hex(getThemeDefinition().textPrimary)(value),
  description: (value: string) => chalk.hex(getThemeDefinition().textDim)(value),
  scrollInfo: (value: string) => chalk.hex(getThemeDefinition().textDim)(value),
  noMatch: (value: string) => chalk.hex(getThemeDefinition().textDim)(value),
};

export const editorTheme: EditorTheme = {
  borderColor: (value: string) => chalk.hex(getThemeDefinition().borderPanel)(value),
  selectList: selectListTheme,
};

export const markdownTheme: MarkdownTheme = {
  heading: (value: string) => chalk.bold.hex(getThemeDefinition().markdownHeading)(value),
  link: (value: string) => chalk.hex(getAccentHex())(value),
  linkUrl: (value: string) => chalk.hex(getThemeDefinition().textDim)(value),
  code: (value: string) => chalk.hex(getThemeDefinition().markdownCode)(value),
  codeBlock: (value: string) => chalk.hex(getThemeDefinition().markdownCodeBlock)(value),
  codeBlockBorder: (value: string) => chalk.hex(getThemeDefinition().borderPanel)(value),
  quote: (value: string) => chalk.italic.hex(getThemeDefinition().textDim)(value),
  quoteBorder: (value: string) => chalk.hex(getThemeDefinition().borderPanel)(value),
  hr: (value: string) => chalk.hex(getThemeDefinition().borderPanel)(value),
  listBullet: (value: string) => chalk.hex(getAccentHex())(value),
  bold: (value: string) => chalk.bold(value),
  italic: (value: string) => chalk.italic(value),
  strikethrough: (value: string) => chalk.strikethrough(value),
  underline: (value: string) => chalk.underline(value),
};
