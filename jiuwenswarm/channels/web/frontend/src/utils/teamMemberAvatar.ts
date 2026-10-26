import teamLeaderAvatar from '../assets/teamleader.svg';
import userInTeamAvatar from '../assets/user-in-team.svg';
import teamAvatar2 from '../assets/Team-2.svg';
import teamAvatar3 from '../assets/Team-3.svg';
import teamAvatar4 from '../assets/Team-4.svg';
import teamAvatar5 from '../assets/Team-5.svg';
import teamAvatar6 from '../assets/Team-6.svg';

const TEAM_MEMBER_AVATARS = [
  teamAvatar2,
  teamAvatar3,
  teamAvatar4,
  teamAvatar5,
  teamAvatar6,
];

const TEAM_MEMBER_BACKGROUND_COLORS = [
  '#D7F4EE',
  '#FCE0E0',
  '#E2E8FF',
  '#FFF0C9',
  '#EADCF8',
  '#DCEFFB',
  '#F8DFEF',
  '#E5F4D1',
  '#FBE5D6',
  '#DBF0FF',
  '#F0E6CC',
  '#DDEBDD',
  '#F8D9D4',
  '#D8E3F7',
  '#EFE0F5',
  '#E1F2C4',
  '#FFE1B8',
  '#D9F1F5',
  '#F4D7E9',
  '#E7E0CF',
  '#D3E8DD',
  '#E6DDFF',
  '#F7E2B6',
  '#D9E1EA',
];

const FNV_OFFSET_BASIS = 2166136261;
const FNV_PRIME = 16777619;

export type TeamMemberAvatarKind = 'leader' | 'user' | 'member';

export interface ResolvedTeamMemberAvatar {
  src: string;
  kind: TeamMemberAvatarKind;
  normalizedId: string;
  backgroundColor?: string;
}

export function normalizeTeamMemberId(member?: string): string {
  return member?.trim().toLowerCase().replace(/[\s-]+/g, '_') ?? '';
}

export function isTeamLeaderMember(member?: string): boolean {
  const normalized = normalizeTeamMemberId(member);
  return normalized === 'team_leader' || normalized === 'teamleader';
}

export function isUserMember(member?: string): boolean {
  return normalizeTeamMemberId(member) === 'user';
}

function hashMemberKey(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 33 + value.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function hashString(value: string): number {
  let hash = FNV_OFFSET_BASIS;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, FNV_PRIME) >>> 0;
  }
  return hash;
}

function getMemberAvatarBackgroundColor(value: string): string {
  const hash = hashString(`${value}:avatar-bg`);
  return TEAM_MEMBER_BACKGROUND_COLORS[hash % TEAM_MEMBER_BACKGROUND_COLORS.length];
}

export function resolveTeamMemberAvatar(member?: string): ResolvedTeamMemberAvatar {
  const normalizedId = normalizeTeamMemberId(member);

  if (normalizedId === 'team_leader' || normalizedId === 'teamleader') {
    return {
      src: teamLeaderAvatar,
      kind: 'leader',
      normalizedId,
    };
  }

  if (normalizedId === 'user') {
    return {
      src: userInTeamAvatar,
      kind: 'user',
      normalizedId,
    };
  }

  const hashKey = normalizedId || 'unknown_member';
  const hash = hashMemberKey(hashKey);
  return {
    src: TEAM_MEMBER_AVATARS[hash % TEAM_MEMBER_AVATARS.length],
    kind: 'member',
    normalizedId,
    backgroundColor: getMemberAvatarBackgroundColor(hashKey),
  };
}

export function getTeamMemberAvatarSrc(member?: string): string {
  return resolveTeamMemberAvatar(member).src;
}
