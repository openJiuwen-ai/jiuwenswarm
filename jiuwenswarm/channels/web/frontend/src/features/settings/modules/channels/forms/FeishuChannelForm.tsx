import { forwardRef, useEffect, useImperativeHandle, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, useForm, useFormValue } from '../../../../../components/form';
import { createFeishuAppFormItems } from '../channelFormItems';
import { createChannelFormRules } from '../channelRequirements';
import type { FeishuAppDraft, FeishuFormValues } from '../channelTypes';
import type { ChannelFormController } from '../useChannelForm';

export type FeishuChannelFormHandle = {
  validate: () => boolean;
};

type FeishuAppFormProps = {
  controller: ChannelFormController<FeishuFormValues>;
  apps: FeishuAppDraft[];
  app: FeishuAppDraft;
  appIndex: number;
};

const FeishuAppForm = forwardRef<FeishuChannelFormHandle, FeishuAppFormProps>(function FeishuAppForm(
  { controller, apps, app, appIndex },
  ref,
) {
  const { t } = useTranslation();
  const form = useForm({ initialValues: app });
  const rules = useMemo(() => createChannelFormRules('feishu', t('settingsPanel.validation.required')), [t]);
  const items = useMemo(
    () =>
      createFeishuAppFormItems(t).map((item) => ({
        ...item,
        onChange: (_value: FeishuAppDraft[keyof FeishuAppDraft], values: Readonly<FeishuAppDraft>) => {
          controller.form.setFieldValue(
            'apps',
            apps.map((current, index) => (index === appIndex ? { ...current, ...values } : current)),
          );
        },
      })),
    [appIndex, apps, controller.form, t],
  );

  useEffect(() => {
    form.reset(app);
  }, [app, form]);

  useImperativeHandle(ref, () => ({ validate: () => form.validate().valid }), [form]);

  return (
    <div className="settings-channel-form" data-testid="settings-channels-panel-feishu-app-form">
      <Form
        form={form}
        items={items}
        rules={rules}
        optionalText={t('common.optional')}
        disabled={controller.saving}
        className="settings-channel-form__fields"
        testIdPrefix="settings-channels-panel-feishu-app"
      />
    </div>
  );
});

export const FeishuChannelForm = forwardRef<
  FeishuChannelFormHandle,
  { controller: ChannelFormController<FeishuFormValues>; appIndex: number }
>(function FeishuChannelForm({ controller, appIndex }, ref) {
  const apps = useFormValue(controller.form, 'apps');
  const app = apps[appIndex];

  if (!app) return null;
  return <FeishuAppForm ref={ref} controller={controller} apps={apps} app={app} appIndex={appIndex} />;
});
