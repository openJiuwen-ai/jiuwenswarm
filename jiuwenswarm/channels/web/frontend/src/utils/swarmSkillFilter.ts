/**
 * Dual-read filter for team / swarm skill packages.
 * Aligns with develop ``SkillPickerPanel`` / backend
 * ``skill_type.SWARM_KINDS`` / ``skill_type === 'swarm_skill'``.
 *
 * Picker policy (v2):
 * - Single-agent: ordinary (+ skillpack) only — hide Swarm (develop-aligned).
 * - Team + bound: ordinary only (block Swarm).
 * - Team + unbound: ordinary + at most one Swarm (UI groups/labels them).
 *
 * Note: picker hide ≠ runtime hide. Single-agent SkillUseRail still scans the
 * shared library unless a separate host filter is added.
 */

const SWARM_SKILL_KINDS = new Set(['swarm-skill', 'team-skill']);
const SWARM_SKILL_TYPES = new Set(['swarm_skill', 'swarm-skill', 'teamskills']);

export type SwarmSkillFilterFields = {
  kind?: string | null;
  skill_type?: string | null;
  skillType?: string | null;
};

/** True when the skill is a Swarm / team SOP package. */
export function isSwarmSkillItem(skill: SwarmSkillFilterFields): boolean {
  const kindRaw = skill.kind;
  // Only accept string kinds — ignore nested role objects accidentally mirrored.
  const kind = typeof kindRaw === 'string' ? kindRaw.trim() : '';
  if (kind && SWARM_SKILL_KINDS.has(kind)) return true;
  const skillType = String(skill.skill_type || skill.skillType || '').trim();
  return Boolean(skillType) && SWARM_SKILL_TYPES.has(skillType);
}

/**
 * Whether a skill should appear in the chat skill picker.
 * @param isTeamMode session mode === team
 * @param boundTeamName non-empty when a team_name is already bound
 */
export function isSkillVisibleInPicker(
  skill: SwarmSkillFilterFields,
  isTeamMode: boolean,
  boundTeamName: string,
): boolean {
  // Single-agent: hide Swarm (协作剧本属 Team 无团路径).
  if (!isTeamMode) return !isSwarmSkillItem(skill);
  // Bound team: Swarm XOR — ordinary only.
  if (String(boundTeamName || '').trim()) return !isSwarmSkillItem(skill);
  // Unbound team: ordinary + Swarm both allowed (host enforces ≤1 Swarm on send).
  return true;
}
