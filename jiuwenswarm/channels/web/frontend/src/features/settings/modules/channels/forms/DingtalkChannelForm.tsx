import { forwardRef, useEffect, useImperativeHandle, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, useForm, useFormValue } from '../../../../../components/form';
import { createDingtalkAppFormItems } from '../channelFormItems';
import { createChannelFormRules } from '../channelRequirements';
import type { DingtalkAppDraft, DingtalkFormValues } from '../channelTypes';
import type { ChannelFormController } from '../useChannelForm';

export type DingtalkChannelFormHandle = {
  validate: () => boolean;
};

type DingtalkAppFormProps = {
  controller: ChannelFormController<DingtalkFormValues>;
  apps: DingtalkAppDraft[];
  app: DingtalkAppDraft;
  appIndex: number;
};

const DingtalkAppForm = forwardRef<DingtalkChannelFormHandle, DingtalkAppFormProps>(function DingtalkAppForm(
  { controller, apps, app, appIndex },
  ref,
) {
  const { t } = useTranslation();
  const form = useForm({ initialValues: app });
  const bound = Boolean(app.persistent_agent_id);
  const allowFromInvalid = app.allow_from_invalid;
  const rules = useMemo(() => createChannelFormRules('dingtalk', t('settingsPanel.validation.required')), [t]);
  const items = useMemo(
    () =>
      createDingtalkAppFormItems(t, { bound, allowFromInvalid }).map((item) => ({
        ...item,
        onChange: (_value: DingtalkAppDraft[keyof DingtalkAppDraft], values: Readonly<DingtalkAppDraft>) => {
          // The malformed-allowlist flag belongs to the stored app; only entering a new allowlist clears it.
          const { allow_from_invalid: _stale, ...edited } = values;
          const replaced = item.name === 'allow_from' ? { allow_from_invalid: false } : {};
          controller.form.setFieldValue(
            'apps',
            apps.map((current, index) => (index === appIndex ? { ...current, ...edited, ...replaced } : current)),
          );
        },
      })),
    [allowFromInvalid, appIndex, apps, bound, controller.form, t],
  );

  useEffect(() => {
    form.reset(app);
  }, [app, form]);

  useImperativeHandle(ref, () => ({ validate: () => form.validate().valid }), [form]);

  return (
    <div className="settings-channel-form" data-testid="settings-channels-panel-dingtalk-app-form">
      <div className="settings-channel-form__hint" data-testid="settings-channels-panel-dingtalk-app-hint">
        {bound ? t('channels.dingtalkApps.boundHint') : t('channels.dingtalkApps.ordinaryHint')}
      </div>
      {allowFromInvalid ? (
        <div
          className="settings-channel-form__hint"
          role="alert"
          data-testid="settings-channels-panel-dingtalk-allow-from-invalid"
        >
          {t('channels.dingtalkApps.invalidAllowFrom')}
        </div>
      ) : null}
      <Form
        form={form}
        items={items}
        rules={rules}
        optionalText={t('common.optional')}
        disabled={controller.saving}
        className="settings-channel-form__fields"
        testIdPrefix="settings-channels-panel-dingtalk-app"
      />
    </div>
  );
});

export const DingtalkChannelForm = forwardRef<
  DingtalkChannelFormHandle,
  { controller: ChannelFormController<DingtalkFormValues>; appIndex: number }
>(function DingtalkChannelForm({ controller, appIndex }, ref) {
  const apps = useFormValue(controller.form, 'apps');
  const app = apps[appIndex];

  if (!app) return null;
  return <DingtalkAppForm ref={ref} controller={controller} apps={apps} app={app} appIndex={appIndex} />;
});
