const EVOLUTION_SKILL_COMMANDS = [
  'evolve',
  'evolve_list',
  'evolve_simplify',
  'evolve_rebuild',
  'evolve_rollback',
] as const;

export type SkillSuggestion = {
  kind: 'skill';
  query: string;
  argumentStart: number;
};

/** Complete only the first argument of a standalone evolution command. */
export function getSkillArgumentSuggestion(beforeText: string): SkillSuggestion | null {
  const match = beforeText.match(/^\s*\/([a-z_]+)([ \t]*)([^\s]*)$/);
  if (!match || (!match[2] && match[3])) return null;
  if (!EVOLUTION_SKILL_COMMANDS.some((name) => name === match[1])) return null;
  return {
    kind: 'skill',
    query: match[3],
    argumentStart: beforeText.length - match[3].length,
  };
}

export function filterSkillSuggestions<
  T extends { name: string; display_name?: string; description?: string; skill_type?: string },
>(
  skills: T[],
  query: string,
  isTeamMode: boolean,
  forCommand = false,
): T[] {
  const normalizedQuery = query.trim().toLowerCase();
  return skills.filter((skill) => {
    const supported = isTeamMode
      ? skill.skill_type === 'swarm_skill'
      : !skill.skill_type || skill.skill_type === 'skill' || (!forCommand && skill.skill_type === 'skillpack');
    return (
      supported &&
      (!normalizedQuery ||
        [skill.name, skill.display_name, skill.description]
          .filter(Boolean)
          .join(' ')
          .toLowerCase()
          .includes(normalizedQuery))
    );
  });
}

/** Replace the complete skill token while preserving later arguments. */
export function getSkillArgumentReplacement(
  text: string,
  caret: number,
  suggestion: SkillSuggestion,
  name: string,
): { start: number; end: number; text: string } {
  const restOfToken = text.slice(caret).match(/^\S*/)?.[0] ?? '';
  const end = caret + restOfToken.length;
  const needsSeparator = suggestion.argumentStart > 0 && !/\s/.test(text[suggestion.argumentStart - 1]);
  return {
    start: suggestion.argumentStart,
    end,
    text: `${needsSeparator ? ' ' : ''}${name}${/^[ \t]/.test(text.slice(end)) ? '' : ' '}`,
  };
}
