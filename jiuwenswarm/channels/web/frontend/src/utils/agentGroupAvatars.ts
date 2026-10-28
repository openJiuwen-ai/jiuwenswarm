/**
 * 判断某成员是否属于会话绑定的专家团包：专家节点按成员名、真实预定义成员按
 * member_id。是包成员 → 头像用带 ★ 的哈希插画（包内图片一律不用——同团包图往往
 * 只有一张团级图，成员间会撞脸）；不是包成员返回 false，走普通插画。
 */
export interface AgentGroupAvatarSource {
  members: ReadonlyArray<{ id: string }>;
}

export function resolveAgentGroupMember(
  group: AgentGroupAvatarSource | null | undefined,
  key: { agentTemplate?: string | null; memberId?: string | null },
): boolean {
  const id = key.agentTemplate || key.memberId;
  if (!group || !id) return false;
  return group.members.some((item) => item.id === id);
}
