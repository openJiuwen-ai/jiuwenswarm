import type { AgentMode } from '../types';

/** Concrete MACRO lanes after Auto resolves (excludes Auto selection and harness). */
export type MacroLaneMode = 'agent' | 'team';

/** Modes that use the single-agent rails / queue UX (not team / harness). */
export function isSingleAgentMode(mode: AgentMode | null | undefined): boolean {
  return mode === 'agent' || mode === 'agent.plan' || mode === 'auto';
}

export function resolveEffectiveAgentMode(
  mode: AgentMode,
  lastMacroRoutedMode: MacroLaneMode | null | undefined,
): AgentMode {
  if (mode === 'auto' && lastMacroRoutedMode) {
    return lastMacroRoutedMode;
  }
  return mode;
}

export function isEffectiveTeamMode(
  mode: AgentMode,
  lastMacroRoutedMode: MacroLaneMode | null | undefined,
): boolean {
  return resolveEffectiveAgentMode(mode, lastMacroRoutedMode) === 'team';
}

export function normalizeMacroLaneMode(raw: unknown): MacroLaneMode | null {
  if (typeof raw !== 'string') return null;
  const normalized = raw.trim().toLowerCase();
  if (normalized === 'team' || normalized === 'cluster' || normalized === 'agent.team') {
    return 'team';
  }
  if (
    normalized === 'agent' ||
    normalized === 'agent.fast' ||
    normalized === 'fast' ||
    normalized === 'performance' ||
    normalized === 'agent.plan' ||
    normalized === 'plan' ||
    normalized === 'planning'
  ) {
    return 'agent';
  }
  return null;
}
