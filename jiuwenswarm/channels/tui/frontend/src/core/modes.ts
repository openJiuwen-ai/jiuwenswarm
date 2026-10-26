export type ClientMode =
  | "agent.plan"
  | "agent.fast"
  | "code.plan"
  | "code.normal"
  | "code.team"
  | "team"
  | "team.plan";

export function isClientMode(value: string): value is ClientMode {
  return (
    value === "agent.plan" ||
    value === "agent.fast" ||
    value === "code.plan" ||
    value === "code.normal" ||
    value === "code.team" ||
    value === "team" ||
    value === "team.plan"
  );
}

export function isTeamMode(mode: ClientMode): boolean {
  return mode === "team" || mode === "team.plan" || mode === "code.team";
}
