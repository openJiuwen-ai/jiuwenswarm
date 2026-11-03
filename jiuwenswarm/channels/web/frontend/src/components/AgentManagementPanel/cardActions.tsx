import { Trash2 } from 'lucide-react';
import LeaderSwitchIcon from '../../assets/agent-management/switch.svg?react';

/** 卡片删除键图标：默认 #808080（--color-text-meta），hover 跟随 .page-card-action 提亮为 accent 色 */
export function DeleteCardIcon() {
  return <Trash2 size={15} className="text-text-meta" />;
}

/** 卡片切换群主图标：默认 #808080（--color-text-meta），hover 跟随 .page-card-action 提亮为 accent 色 */
export function SwitchCardIcon() {
  return <LeaderSwitchIcon className="text-text-meta" />;
}
