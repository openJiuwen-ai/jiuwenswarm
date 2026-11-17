import { describe, expect, it } from 'vitest';
import {
  isSkillVisibleInPicker,
  isSwarmSkillItem,
} from './swarmSkillFilter';

describe('isSwarmSkillItem', () => {
  it('detects kind and skill_type markers', () => {
    expect(isSwarmSkillItem({ kind: 'swarm-skill' })).toBe(true);
    expect(isSwarmSkillItem({ kind: 'team-skill' })).toBe(true);
    expect(isSwarmSkillItem({ skill_type: 'swarm_skill' })).toBe(true);
    expect(isSwarmSkillItem({ skill_type: 'skill' })).toBe(false);
    expect(isSwarmSkillItem({})).toBe(false);
  });

  it('ignores non-string kind', () => {
    expect(isSwarmSkillItem({ kind: { id: 'ai_agent' } as unknown as string })).toBe(false);
  });
});

describe('isSkillVisibleInPicker', () => {
  const ordinary = { skill_type: 'skill' };
  const swarm = { kind: 'swarm-skill', skill_type: 'swarm_skill' };

  it('hides swarm in single-agent mode', () => {
    expect(isSkillVisibleInPicker(ordinary, false, '')).toBe(true);
    expect(isSkillVisibleInPicker(swarm, false, '')).toBe(false);
  });

  it('hides swarm when a team is bound', () => {
    expect(isSkillVisibleInPicker(ordinary, true, 'oc_team')).toBe(true);
    expect(isSkillVisibleInPicker(swarm, true, 'oc_team')).toBe(false);
  });

  it('allows ordinary and swarm when team unbound', () => {
    expect(isSkillVisibleInPicker(ordinary, true, '')).toBe(true);
    expect(isSkillVisibleInPicker(swarm, true, '')).toBe(true);
  });
});
