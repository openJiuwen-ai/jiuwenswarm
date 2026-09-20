import ExtensionAsset from '../../assets/agent-management/extension.svg?react';
import NewConversationAsset from '../../assets/agent-management/new-conversation.svg?react';

export function ExtensionIcon({ size = 16, className }: { size?: number; className?: string }) {
  return <ExtensionAsset width={size} height={size} className={className} aria-hidden="true" />;
}

export function NewConversationIcon({ size = 14, color = 'currentColor' }: { size?: number; color?: string }) {
  return <NewConversationAsset width={size} height={size} color={color} aria-hidden="true" />;
}
