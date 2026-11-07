import { useOrganizationConversationKey } from '../../features/teamOrganization/conversation';
import clsx from 'clsx';
import { useMemo } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { useAgentGroupVisualIdentity } from '../../features/agentManagement/useAgentGroupAvatars';
import { resolveAgentGroupMember } from '../../utils/agentGroupAvatars';
import { resolveTeamMemberAvatar, type TeamMemberIdentity } from '../../utils/teamMemberAvatar';

interface TeamMemberAvatarProps {
  member?: string;
  className?: string;
  imageClassName?: string;
  alt?: string;
  /**
   * 显式指定成员身份，覆盖名册查询。给"上下文本身已经确定了身份、但成员未必在
   * 名册里"的场景用（如人类成员邀请卡——邀请指令只对 human_agent 存在），
   * 免得受名册到达时序影响。
   */
  identity?: TeamMemberIdentity;
}

/**
 * 从当前会话的成员名册里取该成员的身份（人类 / 外部 CLI / 专家团成员）。
 *
 * 调用方手上只有 member_id，身份信息在名册里；在组件里订阅而不是让工具函数直接
 * 读 store，是为了名册更新时头像能跟着重渲染——成员刚建出来时 cli_agent 可能还没到。
 */
function useTeamMemberIdentity(member?: string): TeamMemberIdentity | undefined {
  const activeSessionId = useOrganizationConversationKey(useChatStore((s) => s.activeSessionId));
  const teamMembers = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamMembers);
  const departedMembers = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamDepartedMembers);
  return useMemo(() => {
    const id = member?.trim();
    if (!id) return undefined;
    // 离队成员做过的任务卡片仍要显示它的头像身份（CLI 品牌脸 / 专家等）。
    const known =
      teamMembers?.find((item) => item.member_id === id) ??
      departedMembers?.find((item) => item.member_id === id);
    if (!known) return undefined;
    return { role: known.role, mode: known.mode, cliAgent: known.cli_agent, agentTemplate: known.agent_template };
  }, [member, teamMembers, departedMembers]);
}

/**
 * 会话绑定了专家团时，判断这个成员是否来自该包：专家节点按 agent_template，
 * 预定义成员按 member_id。是包成员 → 带 ★ 的插画外观。
 */
function useIsAgentGroupMember(
  member: string | undefined,
  identity: TeamMemberIdentity | undefined,
): boolean {
  const activeSessionId = useOrganizationConversationKey(useChatStore((s) => s.activeSessionId));
  const groupId = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.agentGroupBinding ?? null);
  const group = useAgentGroupVisualIdentity(groupId);
  return resolveAgentGroupMember(group, { agentTemplate: identity?.agentTemplate, memberId: member?.trim() });
}

export function TeamMemberAvatar({
  member,
  className,
  imageClassName,
  alt,
  identity,
}: TeamMemberAvatarProps): JSX.Element {
  const rosterIdentity = useTeamMemberIdentity(member);
  const effectiveIdentity = identity ?? rosterIdentity;
  const packageMember = useIsAgentGroupMember(member, effectiveIdentity);
  const avatar = resolveTeamMemberAvatar(
    member,
    packageMember ? { ...effectiveIdentity, packageMember: true } : effectiveIdentity,
  );
  const defaultImageRadius = avatar.kind === 'user' ? 'rounded-xl' : 'rounded-2xl';
  const label = alt ?? `${member || 'Unknown'} avatar`;

  const face = (
    <div
      className={clsx(
        className ? null : 'h-8 w-8',
        'relative shrink-0 overflow-hidden rounded-xl bg-transparent',
        className
      )}
      style={avatar.backgroundColor ? { backgroundColor: avatar.backgroundColor } : undefined}
      data-avatar-kind={avatar.kind}
    >
      <img
        src={avatar.src}
        alt={label}
        className={clsx(
          'h-full w-full object-cover',
          defaultImageRadius,
          // 品牌图标是方形 logo，铺满会裁掉边角，缩进留白后按原比例居中。
          avatar.kind === 'cli' && 'scale-[0.72] object-contain',
          imageClassName
        )}
      />
    </div>
  );

  if (avatar.badge !== 'expert') return face;
  // 角标挂在裁切圆的外层：圆形容器 overflow-hidden 会把贴角的角标切掉大半。
  return (
    <span className="relative inline-flex shrink-0" data-testid="team-member-avatar-expert">
      {face}
      <span
        aria-hidden="true"
        data-testid="team-member-avatar-expert-badge"
        className="pointer-events-none absolute -bottom-0.5 -right-0.5 flex h-[11px] w-[11px] items-center justify-center rounded-full bg-amber-400 text-[7px] leading-none text-white ring-1 ring-card"
      >
        ★
      </span>
    </span>
  );
}
