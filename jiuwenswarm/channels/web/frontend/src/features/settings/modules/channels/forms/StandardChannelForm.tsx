import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Form } from '../../../../../components/form';
import type { FormItem, FormRules, FormValues } from '../../../../../components/form';
import type { ChannelFormController } from '../useChannelForm';

export function StandardChannelForm<TValues extends FormValues>({
  controller,
  items,
  rules,
  hint,
}: {
  controller: ChannelFormController<TValues>;
  items: readonly FormItem<TValues>[];
  rules: FormRules<TValues>;
  hint?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="settings-channel-form">
      {hint ? <div className="settings-channel-form__hint">{hint}</div> : null}
      <Form
        form={controller.form}
        items={items}
        rules={rules}
        optionalText={t('common.optional')}
        disabled={controller.saving}
        className="settings-channel-form__fields"
        testIdPrefix="settings-channels-panel-channel-config"
      />
    </div>
  );
}
