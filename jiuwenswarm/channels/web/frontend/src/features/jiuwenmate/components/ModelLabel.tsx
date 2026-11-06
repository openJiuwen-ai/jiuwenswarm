import { useTranslation } from 'react-i18next';
import deepseekIcon from '../../../assets/jiuwenmate/icons/model-deepseek.svg';
import kimiIcon from '../../../assets/jiuwenmate/icons/model-kimi.png';
import { findModel, type JiuwenMateModelCatalog } from '../jiuwenmateApi';

const MODEL_ICONS: readonly { pattern: RegExp; icon: string }[] = [
  { pattern: /deepseek/i, icon: deepseekIcon },
  { pattern: /kimi|moonshot/i, icon: kimiIcon },
];

interface ModelLabelProps {
  catalog: JiuwenMateModelCatalog;
  /** Empty means the server default model. */
  modelId: string;
}

export function ModelLabel({ catalog, modelId }: ModelLabelProps) {
  const { t } = useTranslation();
  const model = findModel(catalog, modelId);
  const label = model?.label || modelId || catalog.defaultModelLabel || t('jiuwenmate.create.defaultModel');
  const searchable = `${model?.provider ?? ''} ${label}`;
  const icon = MODEL_ICONS.find((item) => item.pattern.test(searchable))?.icon;
  return (
    <>
      {icon ? <img src={icon} alt="" aria-hidden /> : null}
      <span data-testid="jiuwenmate-model-label">{label}</span>
    </>
  );
}
