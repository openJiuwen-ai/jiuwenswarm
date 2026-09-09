import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { FEATURE_PERSONAL_CONTEXT_UI } from '../../../../featureFlags';
import { PersonalContextSettingsPanel } from '../../../../components/PersonalContext/SettingsPanel';

/**
 * 个人上下文设置模块包装。
 *
 * 仿 ChannelsModule：设置页 custom item 的 render 只给 { disabled }，
 * 而真正面板需 isConnected —— 通过 useSettingsServices 桥接（与频道模块同模式）。
 * feature 关闭时返回 null，模块项仍在列表但内容为空，不暴露入口。
 */
export function PersonalContextSettingsModule() {
  if (!FEATURE_PERSONAL_CONTEXT_UI) return null;
  const { isConnected } = useSettingsServices();
  return <PersonalContextSettingsPanel isConnected={isConnected} />;
}
