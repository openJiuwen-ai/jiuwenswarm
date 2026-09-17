import teamLeaderAvatar from '../assets/teamleader.svg';
import userInTeamAvatar from '../assets/user-in-team.svg';
import teamAvatar2 from '../assets/Team-2.svg';
import teamAvatar3 from '../assets/Team-3.svg';
import teamAvatar4 from '../assets/Team-4.svg';
import teamAvatar5 from '../assets/Team-5.svg';
import teamAvatar6 from '../assets/Team-6.svg';
import claudeCodeAvatar from '../assets/providers/anthropic.png';
import codexAvatar from '../assets/providers/openai.png';

const TEAM_MEMBER_AVATARS = [
  teamAvatar2,
  teamAvatar3,
  teamAvatar4,
  teamAvatar5,
  teamAvatar6,
];

// 人类成员（role=human_agent）专用。目前只有"团队中的用户"这一张，多个人类成员
// 靠 member_id 哈希出的底色区分；设计补齐插画后往这个数组里加即可，无需改逻辑。
const HUMAN_MEMBER_AVATARS = [userInTeamAvatar];

// 外部 CLI 成员按后端认脸，直接复用仓库里已有的官方品牌图标（ModelProviderIcon
// 同源），和模型选择器里的视觉认知保持一致。
const CLI_AGENT_AVATARS: Record<string, string> = {
  claude: claudeCodeAvatar,
  codex: codexAvatar,
};

// 品牌图标自带底色，套成员那套彩色底会脏，统一给中性底。
const CLI_AGENT_AVATAR_BACKGROUND = '#F2F3F5';

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

export type TeamMemberAvatarKind = 'leader' | 'user' | 'human' | 'cli' | 'member';

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

/** 成员身份，决定用哪一套头像；由调用方从成员名册取。 */
export interface TeamMemberIdentity {
  /** TeamRole 值：human_agent 认人类脸 */
  role?: string;
  /** 老事件用 mode='human' 表达人类成员，等价于 role='human_agent' */
  mode?: string;
  /** 外部 CLI 后端名：claude / codex / ... */
  cliAgent?: string | null;
}

function isHumanIdentity(identity?: TeamMemberIdentity): boolean {
  if (!identity) return false;
  return identity.role === 'human_agent' || identity.mode === 'human';
}

/** CLI 后端名归一到头像 key：值可能写成 claude / claude-code / codex 等。 */
function resolveCliAvatarKey(cliAgent?: string | null): string | null {
  const value = cliAgent?.trim().toLowerCase();
  if (!value) return null;
  if (value.includes('claude')) return 'claude';
  if (value.includes('codex') || value.includes('openai')) return 'codex';
  return null;
}

export function resolveTeamMemberAvatar(
  member?: string,
  identity?: TeamMemberIdentity
): ResolvedTeamMemberAvatar {
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

  // 外部 CLI 成员认后端的脸：同一队里可能同时有 Claude Code 和 Codex，用哈希插画
  // 就完全看不出谁是谁。品牌图标底色自带，套彩色底会脏，故给中性底。
  const cliKey = resolveCliAvatarKey(identity?.cliAgent);
  if (cliKey) {
    return {
      src: CLI_AGENT_AVATARS[cliKey],
      kind: 'cli',
      normalizedId,
      backgroundColor: CLI_AGENT_AVATAR_BACKGROUND,
    };
  }

  const hashKey = normalizedId || 'unknown_member';
  const hash = hashMemberKey(hashKey);

  // 人类成员单独一套：这一格背后是真人，和 AI 队友混用同一套插画会误导。
  if (isHumanIdentity(identity)) {
    return {
      src: HUMAN_MEMBER_AVATARS[hash % HUMAN_MEMBER_AVATARS.length],
      kind: 'human',
      normalizedId,
      backgroundColor: getMemberAvatarBackgroundColor(hashKey),
    };
  }

  return {
    src: TEAM_MEMBER_AVATARS[hash % TEAM_MEMBER_AVATARS.length],
    kind: 'member',
    normalizedId,
    backgroundColor: getMemberAvatarBackgroundColor(hashKey),
  };
}

export function getTeamMemberAvatarSrc(
  member?: string,
  identity?: TeamMemberIdentity
): string {
  return resolveTeamMemberAvatar(member, identity).src;
}
