import type { TeamMemberEvent, TeamMessageEvent, TeamTaskEvent } from "../../core/types.js";
import { padToWidth } from "../rendering/text.js";
import { palette } from "../theme.js";
import {
  isWorkingStatusLabel,
  latestMemberSummaries,
  memberStatusPhrase,
  taskEventLabel,
  truncate,
} from "./team-shared.js";

export function renderTeamStatusPill(
  memberEvents: TeamMemberEvent[],
  taskEvents: TeamTaskEvent[],
  messageEvents: TeamMessageEvent[],
  width: number,
): string[] {
  const members = latestMemberSummaries(memberEvents, messageEvents);
  if (members.length === 0 && taskEvents.length === 0 && messageEvents.length === 0) {
    return [];
  }

  const workingCount = members.filter((member) => isWorkingStatusLabel(member.statusLabel)).length;
  const parts: string[] = [];
  if (members.length > 0) {
    parts.push(`${members.length} teammate${members.length === 1 ? "" : "s"}`);
  }
  if (workingCount > 0) {
    parts.push(`${workingCount} working`);
  } else if (members[0]) {
    parts.push(memberStatusPhrase(members[0]));
  }
  const latestTask = taskEvents.at(-1);
  if (latestTask) {
    parts.push(truncate(taskEventLabel(latestTask), 32));
  }
  const content = parts.length > 0 ? parts.join(" · ") : "team active";
  return [padToWidth(palette.text.dim(`• ${content} · ctrl+g to view`), width)];
}
