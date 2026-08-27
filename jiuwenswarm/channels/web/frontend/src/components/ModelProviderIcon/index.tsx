import './index.css';
import { getModelLogoUrl } from '../../assets/providers';

/**
 * 模型厂商图标组件
 *
 * 与模型设置页共用同一套确定性分类：免费模型和没有 vendor_key 的配置显示自定义模型图标；
 * OpenAI 账号显示 OpenAI 图标；明确绑定厂商预设的模型显示 vendor_key 对应的厂商图标。
 */

export type ModelLike = {
  model_name: string;
  model_provider?: string;
  alias?: string;
  vendor_key?: string;
  is_free?: boolean;
};

/** 获取与模型设置页一致的模型图标。 */
export function getProviderIconUrl(model: ModelLike): string {
  return getModelLogoUrl(model);
}

interface ModelProviderIconProps {
  model: ModelLike;
  className?: string;
}

/** 使用统一模型身份规则渲染对应图标。 */
export function ModelProviderIcon({ model, className }: ModelProviderIconProps) {
  const iconUrl = getProviderIconUrl(model);

  return (
    <img
      className={`model-provider-icon model-provider-icon--img${className ? ` ${className}` : ''}`}
      src={iconUrl}
      alt=""
      aria-hidden="true"
      data-testid="model-provider-icon"
      data-variant="image"
    />
  );
}
