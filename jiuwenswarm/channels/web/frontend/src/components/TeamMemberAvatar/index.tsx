import clsx from 'clsx';
import { useMemo } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
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
 * 从当前会话的成员名册里取该成员的身份（人类 / 外部 CLI / 普通队友）。
 *
 * 调用方手上只有 member_id，身份信息在名册里；在组件里订阅而不是让工具函数直接
 * 读 store，是为了名册更新时头像能跟着重渲染——成员刚建出来时 cli_agent 可能还没到。
 */
function useTeamMemberIdentity(member?: string): TeamMemberIdentity | undefined {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const teamMembers = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamMembers);
  return useMemo(() => {
    const id = member?.trim();
    if (!id || !teamMembers) return undefined;
    const known = teamMembers.find((item) => item.member_id === id);
    if (!known) return undefined;
    return { role: known.role, mode: known.mode, cliAgent: known.cli_agent };
  }, [member, teamMembers]);
}

export function TeamMemberAvatar({
  member,
  className,
  imageClassName,
  alt,
  identity,
}: TeamMemberAvatarProps): JSX.Element {
  const rosterIdentity = useTeamMemberIdentity(member);
  const avatar = resolveTeamMemberAvatar(member, identity ?? rosterIdentity);
  const defaultImageRadius = avatar.kind === 'user' ? 'rounded-xl' : 'rounded-2xl';

  return (
    <div
      className={clsx(
        className ? null : 'h-8 w-8',
        'shrink-0 overflow-hidden rounded-xl bg-transparent',
        className
      )}
      style={avatar.backgroundColor ? { backgroundColor: avatar.backgroundColor } : undefined}
    >
      <img
        src={avatar.src}
        alt={alt ?? `${member || 'Unknown'} avatar`}
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
}
