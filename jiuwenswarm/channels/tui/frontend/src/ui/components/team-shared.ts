import type { TeamMemberEvent, TeamMessageEvent, TeamTaskEvent } from "../../core/types.js";

export type TeamMemberSummary = {
  memberId: string;
  statusLabel: string;
  timestamp: number;
  preview?: string;
  previewKind?: "broadcast" | "p2p";
};

export function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

export function formatElapsed(timestamp: number): string {
  const diff = Math.max(0, Date.now() - timestamp);
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h`;
}

function normalizeStatusValue(value: string | undefined): string {
  return value?.trim().toLowerCase() ?? "";
}

export function memberStatusLabel(event: TeamMemberEvent): string {
  const type = normalizeStatusValue(event.type);
  const oldStatus = normalizeStatusValue(event.oldStatus);
  const nextStatus = normalizeStatusValue(event.newStatus);
  const current = nextStatus || oldStatus;

  if (type.endsWith(".spawned")) return "spawned";
  if (type.endsWith(".restarted")) return "restarted";
  if (type.endsWith(".shutdown")) return "shutdown";
  if (type.endsWith(".execution_changed")) {
    if (current.includes("exec") || current.includes("run")) return "working";
    if (current.includes("idle")) return "idle";
  }
  if (type.endsWith(".status_changed")) {
    if (current.includes("busy")) return "busy";
    if (current.includes("wait")) return "waiting";
    if (current.includes("block")) return "blocked";
    if (current.includes("idle")) return "idle";
    if (current.includes("active")) return "active";
  }
  if (current.includes("think")) return "thinking";
  if (current.includes("exec")) return "working";
  if (current.includes("run")) return "running";
  if (current.includes("busy")) return "busy";
  if (current.includes("idle")) return "idle";
  if (current.includes("wait")) return "waiting";
  if (current.includes("block")) return "blocked";
  if (current.includes("error") || current.includes("fail")) return "error";
  return current || "idle";
}

export function isWorkingStatusLabel(statusLabel: string): boolean {
  const normalized = normalizeStatusValue(statusLabel);
  return (
    normalized === "working" ||
    normalized === "running" ||
    normalized === "busy" ||
    normalized === "active" ||
    normalized === "thinking"
  );
}

export type TeamStatusTone = "active" | "idle" | "warning" | "error" | "subtle";

export function memberStatusTone(statusLabel: string): TeamStatusTone {
  const normalized = normalizeStatusValue(statusLabel);
  if (normalized === "shutdown" || normalized === "error") return "error";
  if (normalized === "waiting" || normalized === "blocked") return "warning";
  if (normalized === "idle" || normalized === "spawned" || normalized === "restarted")
    return "subtle";
  if (isWorkingStatusLabel(normalized)) return "active";
  return "idle";
}

export function isLeaderMember(memberId: string): boolean {
  return /(^|[_-])leader($|[_-])/i.test(memberId) || /teamleader/i.test(memberId);
}

export function memberDisplayTitle(memberId: string): string {
  if (isLeaderMember(memberId)) {
    return "Team Lead";
  }
  return memberId.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

export function memberStatusPhrase(summary: TeamMemberSummary): string {
  if (isWorkingStatusLabel(summary.statusLabel)) {
    return summary.statusLabel;
  }
  if (summary.statusLabel === "idle") {
    return `idle for ${formatElapsed(summary.timestamp)}`;
  }
  if (summary.statusLabel === "waiting" || summary.statusLabel === "blocked") {
    return `${summary.statusLabel} for ${formatElapsed(summary.timestamp)}`;
  }
  if (summary.statusLabel === "spawned" || summary.statusLabel === "restarted") {
    return `${summary.statusLabel} ${formatElapsed(summary.timestamp)} ago`;
  }
  return summary.statusLabel;
}

export function latestMemberSummaries(
  memberEvents: TeamMemberEvent[],
  messageEvents: TeamMessageEvent[],
): TeamMemberSummary[] {
  const latestMembers = new Map<string, TeamMemberEvent>();
  for (const event of memberEvents) {
    const previous = latestMembers.get(event.memberId);
    if (!previous || event.timestamp >= previous.timestamp) {
      latestMembers.set(event.memberId, event);
    }
  }
  const latestMessages = new Map<string, TeamMessageEvent>();
  for (const event of messageEvents) {
    for (const memberId of [event.fromMember, event.toMember].filter(Boolean) as string[]) {
      const previous = latestMessages.get(memberId);
      if (!previous || event.timestamp >= previous.timestamp) {
        latestMessages.set(memberId, event);
      }
    }
  }
  return [...latestMembers.values()]
    .map((event) => ({
      memberId: event.memberId,
      statusLabel: memberStatusLabel(event),
      timestamp: event.timestamp,
      preview: latestMessages.get(event.memberId)?.content.trim() || undefined,
      previewKind: latestMessages.get(event.memberId)
        ? ((latestMessages.get(event.memberId)?.toMember ? "p2p" : "broadcast") as
            | "broadcast"
            | "p2p")
        : undefined,
    }))
    .sort((a, b) => {
      const aLeader = isLeaderMember(a.memberId) ? 0 : 1;
      const bLeader = isLeaderMember(b.memberId) ? 0 : 1;
      if (aLeader !== bLeader) return aLeader - bLeader;
      return b.timestamp - a.timestamp;
    });
}

export function orderedMemberIds(
  memberEvents: TeamMemberEvent[],
  messageEvents: TeamMessageEvent[],
): string[] {
  return latestMemberSummaries(memberEvents, messageEvents).map((member) => member.memberId);
}

export function isTeamWorking(
  memberEvents: TeamMemberEvent[],
  messageEvents: TeamMessageEvent[],
): boolean {
  return latestMemberSummaries(memberEvents, messageEvents).some((member) =>
    isWorkingStatusLabel(member.statusLabel),
  );
}

export function teamWorkingStartedAtMs(
  memberEvents: TeamMemberEvent[],
  messageEvents: TeamMessageEvent[],
): number | undefined {
  const activeMembers = latestMemberSummaries(memberEvents, messageEvents).filter((member) =>
    isWorkingStatusLabel(member.statusLabel),
  );
  if (activeMembers.length === 0) {
    return undefined;
  }
  return Math.min(...activeMembers.map((member) => member.timestamp));
}

export function taskEventLabel(event: TeamTaskEvent): string {
  const action = event.type.replace(/^team\.task\./, "").replaceAll("_", " ");
  return `${action} ${event.taskId}${event.status ? ` · ${event.status}` : ""}`.trim();
}

export function messageEventLabel(event: TeamMessageEvent): string {
  const route = event.toMember
    ? `p2p ${event.fromMember} → ${event.toMember}`
    : `broadcast ${event.fromMember} → team`;
  return `${route} · ${event.content.replace(/\s+/g, " ").trim()}`;
}

export function messagePreviewLabel(
  content: string,
  kind: "broadcast" | "p2p" | undefined,
): string {
  const prefix = kind === "broadcast" ? "broadcast" : kind === "p2p" ? "p2p" : null;
  return prefix ? `${prefix} · ${content}` : content;
}
