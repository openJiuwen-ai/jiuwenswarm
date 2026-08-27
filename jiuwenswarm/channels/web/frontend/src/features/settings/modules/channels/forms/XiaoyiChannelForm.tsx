import { useMemo, useState } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useFormValue } from '../../../../../components/form';
import { Button } from '../../../../../components/ui';
import { createXiaoyiFormItems } from '../channelFormItems';
import { createChannelFormRules } from '../channelRequirements';
import type { XiaoyiFormValues } from '../channelTypes';
import type { ChannelFormController } from '../useChannelForm';
import { StandardChannelForm } from './StandardChannelForm';

export function XiaoyiChannelForm({ controller }: { controller: ChannelFormController<XiaoyiFormValues> }) {
  const { t } = useTranslation();
  const enabled = useFormValue(controller.form, 'enabled');
  const apiId = useFormValue(controller.form, 'api_id');
  const [hintDismissed, setHintDismissed] = useState(false);
  const items = useMemo(() => createXiaoyiFormItems(t), [t]);
  const rules = useMemo(() => createChannelFormRules('xiaoyi', t('settingsPanel.validation.required')), [t]);
  const showApiIdHint = enabled && !apiId.trim() && !hintDismissed;

  return (
    <StandardChannelForm
      controller={controller}
      items={items}
      rules={rules}
      hint={
        showApiIdHint ? (
          <div className="settings-channel-form__warning">
            <p>{t('channels.placeholders.xiaoyiApiIdRequiredForCron')}</p>
            <Button
              variant="quiet"
              size="sm"
              icon={<X size={14} aria-hidden />}
              aria-label={t('common.close')}
              onClick={() => setHintDismissed(true)}
            />
          </div>
        ) : null
      }
    />
  );
}
