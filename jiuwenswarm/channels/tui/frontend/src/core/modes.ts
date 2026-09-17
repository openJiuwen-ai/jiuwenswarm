export type ClientMode =
  | "agent.plan"
  | "agent.fast"
  | "code.plan"
  | "code.normal"
  | "code.team"
  | "team"
  | "team.plan"
  | "team.plan.normal"
  | "team.plan.code";

export function isClientMode(value: string): value is ClientMode {
  return (
    value === "agent.plan" ||
    value === "agent.fast" ||
    value === "code.plan" ||
    value === "code.normal" ||
    value === "code.team" ||
    value === "team" ||
    value === "team.plan" ||
    value === "team.plan.normal" ||
    value === "team.plan.code"
  );
}

export function isTeamMode(mode: ClientMode): boolean {
  return (
    mode === "team" ||
    mode === "team.plan" ||
    mode === "team.plan.normal" ||
    mode === "team.plan.code" ||
    mode === "code.team"
  );
}

/** Keep runtime identifiers canonical while presenting the public TUI hierarchy. */
export function formatModeForDisplay(mode: string): string {
  return mode === "code.team" ? "team.code" : mode;
}
