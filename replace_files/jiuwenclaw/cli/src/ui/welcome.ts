import { visibleWidth } from "@mariozechner/pi-tui";
import type { ConnectionStatus } from "../core/ws-client.js";
import { padToWidth } from "./rendering/text.js";
import { chalk } from "./theme.js";

const ART_TITLE_RAW = [
  "",
  "     ██╗██╗██╗   ██╗██╗    ██╗███████╗███╗   ██╗    ██████╗██╗      █████╗ ██╗    ██╗",
  "     ██║██║██║   ██║██║    ██║██╔════╝████╗  ██║   ██╔════╝██║     ██╔══██╗██║    ██║",
  "     ██║██║██║   ██║██║ █╗ ██║█████╗  ██╔██╗ ██║   ██║     ██║     ███████║██║ █╗ ██║",
  "██   ██║██║██║   ██║██║███╗██║██╔══╝  ██║╚██╗██║   ██║     ██║     ██╔══██║██║███╗██║",
  "╚█████╔╝██║╚██████╔╝╚███╔███╔╝███████╗██║ ╚████║   ╚██████╗███████╗██║  ██║╚███╔███╔╝",
  " ╚════╝ ╚═╝ ╚═════╝  ╚══╝╚══╝ ╚══════╝╚═╝  ╚═══╝    ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ",
] as const;

const BG_MAGENTA = "#2E0B23";
const GRADIENT_COLORS = [
  "#FFD700",
  "#FFD000",
  "#FFC000",
  "#FFB000",
  "#FFA000",
  "#FF9000",
  "#FF8000",
  "#FF7000",
  "#FF6000",
  "#FF5000",
  "#FF4500",
  "#FF3D00",
];

function applyGradient(line: string, colorIndex: number): string {
  const color = GRADIENT_COLORS[Math.min(colorIndex, GRADIENT_COLORS.length - 1)] ?? "#FFD700";
  return chalk.hex(color)(line);
}

function centerLine(line: string, width: number): string {
  const lineWidth = visibleWidth(line);
  const totalPadding = Math.max(0, width - lineWidth);
  const leftPadding = Math.floor(totalPadding / 2);
  const rightPadding = totalPadding - leftPadding;
  return " ".repeat(leftPadding) + line + " ".repeat(rightPadding);
}

function connectionHint(status: ConnectionStatus): string | null {
  switch (status) {
    case "connecting":
      return "Connecting to backend…";
    case "reconnecting":
      return "Backend unavailable · retrying connection";
    case "idle":
      return "Backend unavailable · start jiuwenclaw-gateway or check --url";
    case "auth_failed":
      return "Authentication failed · check --token";
    case "connected":
    default:
      return null;
  }
}

export function buildWelcomeLines(
  width: number,
  connectionStatus: ConnectionStatus,
  modelInfo: { provider: string; model: string; version: string } = { provider: "", model: "", version: "" },
  mode: string = ""
): string[] {
  const artWidth = Math.max(...ART_TITLE_RAW.map((line) => visibleWidth(line)));
  const hint = connectionHint(connectionStatus);
  const version = modelInfo.version || "0.1.0";
  const provider = modelInfo.provider || "";
  const model = modelInfo.model || "";
  if (width >= artWidth + 6) {
    const coloredArt = ART_TITLE_RAW.map((line, index) => {
      const coloredLine = applyGradient(line, index);
      return centerLine(coloredLine, width);
    });
    const subtitle = chalk.hex("#FFFFFF")(`v${version} | Provider: ${provider} | Model: ${model} | Mode: ${mode}`);
    const poweredBy = chalk.hex("#FFFFFF")("Powered by ") + chalk.hex("#655795")("openJiuwen SDK") + chalk.hex("#FFFFFF")(` v${version} (`) + chalk.hex("#3a7378")("https://gitcode.com/openJiuwen/agent-core") + chalk.hex("#FFFFFF")(")");
    const cmdBoxWidth = 80;
    const cmdBoxLine = (content: string) => {
      const lineWidth = visibleWidth(content);
      const padding = Math.max(0, cmdBoxWidth - 4 - lineWidth);
      const left = Math.floor(padding / 2);
      const right = padding - left;
      return chalk.hex("#FFFFFF")("│") + " ".repeat(left) + chalk.hex("#FFFFFF")(content) + " ".repeat(right) + chalk.hex("#FFFFFF")(" │");
    };
    const shortCmdTitle = chalk.hex("#FFFFFF")(" 快捷命令 ");
    const titleWithBorder = "───────" + shortCmdTitle + "───────";
    const titleLineWidth = visibleWidth(titleWithBorder);
    const topPadding = Math.max(0, cmdBoxWidth - 2 - titleLineWidth);
    const topLeft = Math.floor(topPadding / 2);
    const topRight = topPadding - topLeft;
    const cmdTop = chalk.hex("#FFFFFF")("┌") + "─".repeat(topLeft) + titleWithBorder + "─".repeat(topRight) + chalk.hex("#FFFFFF")("┐");
    const cmdBottom = chalk.hex("#FFFFFF")("└") + "─".repeat(cmdBoxWidth - 2) + chalk.hex("#FFFFFF")("┘");
    const commands = " /help - 查看帮助    /mode - 切换模式    /skills - 可用技能    /exit - 退出  ";
    return [
      ...coloredArt,
      "",
      centerLine(subtitle, width),
      centerLine(poweredBy, width),
      "",
      centerLine(cmdTop, width),
      centerLine(cmdBoxLine(commands), width),
      centerLine(cmdBottom, width),
      ...(hint ? [centerLine(chalk.hex("#FFFFFF")(hint), width)] : []),
    ];
  }

  return [
    padToWidth(chalk.hex("#FFD700")("JIUWEN CLAW"), width),
    "",
    padToWidth(chalk.hex("#FFFFFF")(`v${version} | Provider: ${provider} | Model: ${model} | Mode: ${mode}`), width),
    padToWidth(chalk.hex("#FFFFFF")("Powered by ") + chalk.hex("#655795")("openJiuwen SDK") + chalk.hex("#FFFFFF")(` v${version}`), width),
    padToWidth(chalk.hex("#3a7378")("https://gitcode.com/openJiuwen/agent-core"), width),
    "",
    padToWidth(chalk.hex("#FFFFFF")("┌────────────────────────────────────────────────────────────┐"), width),
    padToWidth(chalk.hex("#FFFFFF")("│                    ") + chalk.hex("#FFFFFF")(" 快捷命令 ") + chalk.hex("#FFFFFF")("                    │"), width),
    padToWidth(chalk.hex("#FFFFFF")("├────────────────────────────────────────────────────────────┤"), width),
    padToWidth(chalk.hex("#FFFFFF")("│  ") + chalk.hex("#FFFFFF")("/help - 查看帮助                                            ") + chalk.hex("#FFFFFF")("│"), width),
    padToWidth(chalk.hex("#FFFFFF")("│  ") + chalk.hex("#FFFFFF")("/mode - 切换模式                                           ") + chalk.hex("#FFFFFF")("│"), width),
    padToWidth(chalk.hex("#FFFFFF")("│  ") + chalk.hex("#FFFFFF")("/skills - 可用技能                                         ") + chalk.hex("#FFFFFF")("│"), width),
    padToWidth(chalk.hex("#FFFFFF")("│  ") + chalk.hex("#FFFFFF")("/exit - 退出                                               ") + chalk.hex("#FFFFFF")("│"), width),
    padToWidth(chalk.hex("#FFFFFF")("└────────────────────────────────────────────────────────────┘"), width),
    ...(hint ? [padToWidth(chalk.hex("#FFFFFF")(hint), width)] : []),
  ];
}
