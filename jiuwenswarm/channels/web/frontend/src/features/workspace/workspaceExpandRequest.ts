/**
 * 跨面板发起「申请扩容」：先切到工作空间页，再打开扩容对话框。
 * WorkspacePanel 仅在 activeNav=workspace 时挂载，因此用 pending 兜底首挂载。
 */
export const WORKSPACE_EXPAND_EVENT = 'jiuwen:workspace-expand';

let pendingOpenExpand = false;

export function requestOpenWorkspaceExpand(): void {
  pendingOpenExpand = true;
  window.dispatchEvent(new CustomEvent<string>('jiuwen:nav', { detail: 'workspace' }));
  window.dispatchEvent(new CustomEvent(WORKSPACE_EXPAND_EVENT));
}

export function consumePendingWorkspaceExpand(): boolean {
  if (!pendingOpenExpand) return false;
  pendingOpenExpand = false;
  return true;
}

export function clearPendingWorkspaceExpand(): void {
  pendingOpenExpand = false;
}
