import type { TeamMemberEvent, TeamMessageEvent, TeamTaskEvent } from "../../core/types.js";
import { padToWidth } from "../rendering/text.js";
import { palette } from "../theme.js";
import {
  formatElapsed,
  isLeaderMember,
  isWorkingStatusLabel,
  latestMemberSummaries,
  memberDisplayTitle,
  messageEventLabel,
  messagePreviewLabel,
  memberStatusPhrase,
  memberStatusTone,
  taskEventLabel,
  truncate,
  type TeamMemberSummary,
} from "./team-shared.js";

function compactMemberPreview(
  member: TeamMemberSummary,
  width: number,
  prefixWidth: number,
): string | null {
  if (!member.preview) {
    return null;
  }
  return truncate(
    messagePreviewLabel(member.preview.replace(/\s+/g, " "), member.previewKind),
    Math.max(12, width - prefixWidth),
  );
}

function colorMemberLine(line: string, statusLabel: string): string {
  switch (memberStatusTone(statusLabel)) {
    case "active":
      return palette.text.tool(line);
    case "warning":
      return palette.status.warning(line);
    case "error":
      return palette.status.error(line);
    case "subtle":
      return palette.text.subtle(line);
    case "idle":
    default:
      return palette.text.dim(line);
  }
}

function colorTreeContextLine(line: string, dimmed: boolean): string {
  return dimmed ? palette.text.subtle(line) : palette.text.dim(line);
}

function renderMemberTree(
  members: TeamMemberSummary[],
  taskEvents: TeamTaskEvent[],
  messageEvents: TeamMessageEvent[],
  width: number,
  selectedMemberId: string | null,
  viewedTeamMemberId: string | null,
  maxVisibleTeammates = 6,
  compact = false,
): string[] {
  const lines: string[] = [];
  const leader = members.find((member) => isLeaderMember(member.memberId));
  const teammates = members.filter((member) => !isLeaderMember(member.memberId));
  const latestTask = taskEvents.at(-1);
  const latestBroadcast = [...messageEvents].reverse().find((event) => !event.toMember);
  const selectedTeammateIndex = selectedMemberId
    ? teammates.findIndex((member) => member.memberId === selectedMemberId)
    : -1;
  const teammateOffset =
    teammates.length <= maxVisibleTeammates || selectedTeammateIndex < 0
      ? 0
      : Math.max(
          0,
          Math.min(
            selectedTeammateIndex - Math.floor(maxVisibleTeammates / 2),
            teammates.length - maxVisibleTeammates,
          ),
        );
  const visibleTeammates =
    teammates.length <= maxVisibleTeammates
      ? teammates
      : teammates.slice(teammateOffset, teammateOffset + maxVisibleTeammates);
  const contextualTeammates = viewedTeamMemberId
    ? visibleTeammates.filter((member) => member.memberId === selectedMemberId)
    : visibleTeammates;

  if (leader) {
    const leaderHint =
      leader.memberId === selectedMemberId
        ? viewedTeamMemberId === leader.memberId
          ? " · inspecting"
          : " · Enter inspect"
        : "";
    const leadPrimary = `Team Lead · ${memberStatusPhrase(leader)}`;
    lines.push(
      padToWidth(
        leader.memberId === selectedMemberId
          ? palette.text.assistant(truncate(leadPrimary + leaderHint, width - 1))
          : viewedTeamMemberId
            ? palette.text.subtle(truncate(leadPrimary, width - 1))
            : colorMemberLine(truncate(leadPrimary, width - 1), leader.statusLabel),
        width,
      ),
    );
    if (leader.memberId === selectedMemberId && !compact) {
      lines.push(
        padToWidth(colorTreeContextLine(`@${leader.memberId}`, Boolean(viewedTeamMemberId)), width),
      );
    }
    if (leader.preview) {
      lines.push(
        padToWidth(
          colorTreeContextLine(
            `⎿ ${truncate(messagePreviewLabel(leader.preview.replace(/\s+/g, " "), leader.previewKind), Math.max(12, compact ? width - 14 : width - 4))}`,
            Boolean(viewedTeamMemberId),
          ),
          width,
        ),
      );
    }
    if (latestTask) {
      lines.push(
        padToWidth(
          colorTreeContextLine(
            `⎿ ${truncate(taskEventLabel(latestTask), Math.max(12, compact ? width - 14 : width - 4))}`,
            Boolean(viewedTeamMemberId),
          ),
          width,
        ),
      );
    } else if (latestBroadcast && latestBroadcast.content.trim()) {
      lines.push(
        padToWidth(
          colorTreeContextLine(
            `⎿ ${truncate(latestBroadcast.content.replace(/\s+/g, " ").trim(), Math.max(12, compact ? width - 14 : width - 4))}`,
            Boolean(viewedTeamMemberId),
          ),
          width,
        ),
      );
    }
    if (teammates.length > 0) {
      lines.push(" ".repeat(width));
    }
  }

  if (teammateOffset > 0) {
    lines.push(
      padToWidth(colorTreeContextLine(`… ${teammateOffset} earlier teammates`, true), width),
    );
  }

  for (const [index, member] of contextualTeammates.entries()) {
    const isLast =
      viewedTeamMemberId || contextualTeammates.length === 1
        ? true
        : teammateOffset + index === teammates.length - 1;
    const branch = isLast ? "└─" : "├─";
    const selectedHint =
      member.memberId === selectedMemberId
        ? viewedTeamMemberId === member.memberId
          ? " · inspecting"
          : " · Enter inspect"
        : "";
    const primary = `${branch} ${memberDisplayTitle(member.memberId)} · ${memberStatusPhrase(member)}${selectedHint}`;
    lines.push(
      padToWidth(
        member.memberId === selectedMemberId
          ? palette.text.assistant(truncate(primary, width - 1))
          : viewedTeamMemberId
            ? palette.text.subtle(truncate(primary, width - 1))
            : colorMemberLine(truncate(primary, width - 1), member.statusLabel),
        width,
      ),
    );
    if (member.memberId === selectedMemberId && !compact) {
      const childPrefix = isLast ? "   " : "│  ";
      lines.push(
        padToWidth(
          colorTreeContextLine(`${childPrefix}@${member.memberId}`, Boolean(viewedTeamMemberId)),
          width,
        ),
      );
    }
    if (member.preview && (!viewedTeamMemberId || member.memberId === selectedMemberId)) {
      const childPrefix = isLast ? "   " : "│  ";
      lines.push(
        padToWidth(
          colorTreeContextLine(
            `${childPrefix}⎿ ${truncate(messagePreviewLabel(member.preview.replace(/\s+/g, " "), member.previewKind), Math.max(12, compact ? width - 16 : width - 6))}`,
            Boolean(viewedTeamMemberId),
          ),
          width,
        ),
      );
    }
  }

  const remainingTeammates = viewedTeamMemberId
    ? Math.max(0, teammates.length - contextualTeammates.length)
    : teammates.length - teammateOffset - visibleTeammates.length;
  if (remainingTeammates > 0) {
    lines.push(
      padToWidth(colorTreeContextLine(`… ${remainingTeammates} more teammates`, true), width),
    );
  }
  return lines;
}

function renderSelectedMemberDetail(
  memberEvents: TeamMemberEvent[],
  messageEvents: TeamMessageEvent[],
  memberSummary: TeamMemberSummary | undefined,
  selectedMemberId: string,
  width: number,
): string[] {
  const lines: string[] = [];
  const recentMemberEvents = memberEvents
    .filter((event) => event.memberId === selectedMemberId)
    .slice(-4)
    .reverse();
  const relatedMessages = messageEvents
    .filter((event) => event.fromMember === selectedMemberId || event.toMember === selectedMemberId)
    .slice(-4)
    .reverse();
  const roleLabel = isLeaderMember(selectedMemberId) ? "team-lead" : "teammate";
  const displayTitle = memberDisplayTitle(selectedMemberId);
  const activityItems = [
    ...recentMemberEvents.map((event) => {
      const action = event.type.replace(/^team\.member\./, "").replaceAll("_", " ");
      const status = event.newStatus ?? event.oldStatus ?? "";
      return {
        timestamp: event.timestamp,
        text: [action, status, `${formatElapsed(event.timestamp)} ago`].filter(Boolean).join(" · "),
      };
    }),
    ...relatedMessages.map((event) => ({
      timestamp: event.timestamp,
      text: `${messageEventLabel(event)} · ${formatElapsed(event.timestamp)} ago`,
    })),
  ]
    .sort((a, b) => b.timestamp - a.timestamp)
    .slice(0, 6);
  const recentActivitySummary = activityItems[0]?.text;

  lines.push(padToWidth(palette.text.subtle("Inspector"), width));
  lines.push(padToWidth(palette.text.subtle("┌"), width));
  const title = memberSummary
    ? `│ ${displayTitle} · ${memberStatusPhrase(memberSummary)}`
    : `│ ${displayTitle}`;
  lines.push(padToWidth(palette.text.secondary(title), width));
  lines.push(padToWidth(palette.text.dim(`│ @${selectedMemberId}`), width));
  if (memberSummary) {
    const activityLabel = isWorkingStatusLabel(memberSummary.statusLabel) ? "active" : "background";
    lines.push(
      padToWidth(
        palette.text.assistant(
          `│ ${roleLabel} · ${activityLabel} · updated ${formatElapsed(memberSummary.timestamp)} ago`,
        ),
        width,
      ),
    );
  }
  if (memberSummary?.preview) {
    lines.push(
      padToWidth(
        palette.text.subtle(
          `│ latest: ${truncate(messagePreviewLabel(memberSummary.preview.replace(/\s+/g, " "), memberSummary.previewKind), Math.max(12, width - 10))}`,
        ),
        width,
      ),
    );
  }

  if (activityItems.length > 0) {
    lines.push(padToWidth(palette.text.subtle("│"), width));
    lines.push(
      padToWidth(
        palette.text.assistant(
          `│ current: ${truncate(recentActivitySummary ?? "", Math.max(12, width - 13))}`,
        ),
        width,
      ),
    );
    lines.push(padToWidth(palette.text.subtle("│"), width));
    lines.push(padToWidth(palette.text.subtle("│ Recent activity"), width));
    for (const item of activityItems) {
      lines.push(
        padToWidth(palette.text.dim(`│ • ${truncate(item.text, Math.max(12, width - 4))}`), width),
      );
    }
  } else {
    lines.push(padToWidth(palette.text.subtle("│"), width));
    lines.push(padToWidth(palette.text.dim("│ • no recent activity"), width));
  }

  lines.push(padToWidth(palette.text.subtle("│"), width));
  lines.push(padToWidth(palette.text.subtle("└─ ← back"), width));
  return lines;
}

function renderCompactMiniTree(
  members: TeamMemberSummary[],
  taskEvents: TeamTaskEvent[],
  width: number,
): string[] {
  const lines: string[] = [];
  const leader = members.find((member) => isLeaderMember(member.memberId));
  const teammates = members.filter((member) => !isLeaderMember(member.memberId));
  const latestTask = taskEvents.at(-1);

  if (leader) {
    const leaderLine = `Team Lead · ${memberStatusPhrase(leader)}`;
    lines.push(
      padToWidth(colorMemberLine(truncate(leaderLine, width - 1), leader.statusLabel), width),
    );
    const leaderPreview =
      compactMemberPreview(leader, width, 4) ??
      (latestTask ? truncate(taskEventLabel(latestTask), Math.max(12, width - 4)) : null);
    if (leaderPreview) {
      lines.push(padToWidth(colorTreeContextLine(`⎿ ${leaderPreview}`, false), width));
    }
  }

  const visibleTeammates = teammates.slice(0, Math.max(1, leader ? 2 : 3));
  for (const member of visibleTeammates) {
    const teammateLine = `${memberDisplayTitle(member.memberId)} · ${memberStatusPhrase(member)}`;
    lines.push(
      padToWidth(colorMemberLine(truncate(teammateLine, width - 1), member.statusLabel), width),
    );
    const preview = compactMemberPreview(member, width, 4);
    if (preview) {
      lines.push(padToWidth(colorTreeContextLine(`⎿ ${preview}`, false), width));
    }
  }

  const hiddenCount = teammates.length - visibleTeammates.length;
  if (hiddenCount > 0) {
    lines.push(padToWidth(colorTreeContextLine(`… ${hiddenCount} more teammates`, true), width));
  }

  return lines;
}

export function renderTeamPanel(
  memberEvents: TeamMemberEvent[],
  taskEvents: TeamTaskEvent[],
  messageEvents: TeamMessageEvent[],
  width: number,
  selectedMemberId: string | null,
  viewedTeamMemberId: string | null,
): string[] {
  const members = latestMemberSummaries(memberEvents, messageEvents);
  const lines: string[] = [];
  const leader = members.find((member) => isLeaderMember(member.memberId));
  const teammates = members.filter((member) => !isLeaderMember(member.memberId));
  const workingCount = teammates.filter((member) =>
    isWorkingStatusLabel(member.statusLabel),
  ).length;
  const latestMessage = messageEvents.at(-1);

  lines.push(
    padToWidth(
      palette.text.secondary(
        `Team · ${teammates.length} teammate${teammates.length === 1 ? "" : "s"}${workingCount > 0 ? ` · ${workingCount} working` : leader ? ` · ${memberStatusPhrase(leader)}` : ""}`,
      ),
      width,
    ),
  );

  if (members.length > 0) {
    lines.push(
      ...renderMemberTree(
        members,
        taskEvents,
        messageEvents,
        width,
        selectedMemberId,
        viewedTeamMemberId,
        viewedTeamMemberId ? 3 : 6,
      ),
    );
  } else if (latestMessage) {
    lines.push(
      padToWidth(
        palette.text.dim(
          `⎿ ${truncate(messageEventLabel(latestMessage), Math.max(12, width - 2))}`,
        ),
        width,
      ),
    );
  }

  if (viewedTeamMemberId) {
    const viewedSummary = members.find((member) => member.memberId === viewedTeamMemberId);
    lines.push(" ".repeat(width));
    lines.push(padToWidth(palette.text.subtle("─".repeat(Math.max(8, width - 2))), width));
    lines.push(
      ...renderSelectedMemberDetail(
        memberEvents,
        messageEvents,
        viewedSummary,
        viewedTeamMemberId,
        width,
      ),
    );
  } else {
    lines.push(padToWidth(palette.text.subtle("↑/↓ navigate · Enter inspect"), width));
  }
  if (viewedTeamMemberId) {
    lines.push(
      padToWidth(palette.text.subtle("↑/↓ change teammate · Enter inspect · ← back"), width),
    );
  }
  lines.push(" ".repeat(width));
  return lines;
}

export function renderMiniTeamTree(
  memberEvents: TeamMemberEvent[],
  taskEvents: TeamTaskEvent[],
  messageEvents: TeamMessageEvent[],
  width: number,
): string[] {
  const members = latestMemberSummaries(memberEvents, messageEvents);
  if (members.length === 0) {
    return [];
  }
  return renderCompactMiniTree(members, taskEvents, width);
}
