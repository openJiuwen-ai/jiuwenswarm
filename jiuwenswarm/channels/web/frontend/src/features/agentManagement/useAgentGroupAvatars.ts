import { useEffect, useState } from 'react';
import { createAgentGroupManagementClient } from './groupClient';
import type { AgentGroupIdentity } from './types';
import type { AgentGroupAvatarSource } from '../../utils/agentGroupAvatars';

export interface AgentGroupVisualIdentity extends AgentGroupAvatarSource {
  /** 团级身份：聊天区 Leader / 群头像用，不借给成员。 */
  identity: AgentGroupIdentity;
}

// 每个专家团只拉一次详情：头像位、聊天区共用；拉失败也只记一次 null 不再重试——
// 头像是装饰，没图退回插画 / 首字母，不值得为它重复打接口。
const pending = new Map<string, Promise<AgentGroupVisualIdentity | null>>();
// 已解析结果同步可读：切会话时渲染期直接命中，不经过一帧 null，聊天区 Leader 头像不跳。
const resolved = new Map<string, AgentGroupVisualIdentity | null>();

function loadAgentGroupVisualIdentity(groupId: string): Promise<AgentGroupVisualIdentity | null> {
  let task = pending.get(groupId);
  if (!task) {
    task = createAgentGroupManagementClient()
      .getGroup(groupId)
      .then((detail) => ({
        // 团级身份给聊天区 Leader / 群头像；成员表只用来判定归属（★），运行态不渲染包图
        identity: {
          id: detail.id,
          name: detail.name,
          displayName: detail.displayName,
          avatarUrl: detail.avatarUrl,
        },
        members: detail.members.map((member) => ({ id: member.id })),
      }))
      .catch(() => null)
      .then((loaded) => {
        resolved.set(groupId, loaded);
        return loaded;
      });
    pending.set(groupId, task);
  }
  return task;
}

/** 会话绑定的专家团视觉身份（团级身份 + 成员头像表）；未绑定或未加载完返回 null。 */
export function useAgentGroupVisualIdentity(groupId: string | null | undefined): AgentGroupVisualIdentity | null {
  const id = groupId?.trim() || '';
  const [, bump] = useState(0);
  useEffect(() => {
    if (!id || resolved.has(id)) return;
    let active = true;
    void loadAgentGroupVisualIdentity(id).then(() => {
      if (active) bump((n) => n + 1);
    });
    return () => {
      active = false;
    };
  }, [id]);
  return id ? resolved.get(id) ?? null : null;
}

