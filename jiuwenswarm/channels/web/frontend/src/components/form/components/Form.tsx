import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useSyncExternalStore,
} from 'react';
import { HelpTips, Input, RadioGroup, Select, Switch, Textarea } from '../../ui';
import type { FormItem, FormRules, FormValues } from '../types';
import type { FormFieldOptions, FormStore } from '../core/FormStore';
import './Form.css';

function FormItemRenderer<TValues extends FormValues>({
  form,
  item,
  optionalText,
  showOptional,
  disabled,
  testIdPrefix,
}: {
  form: FormStore<TValues>;
  item: FormItem<TValues>;
  optionalText: string;
  showOptional: boolean;
  disabled: boolean;
  testIdPrefix?: string;
}) {
  useSyncExternalStore(form.subscribe, form.getRevision, form.getRevision);
  const controlId = useId();
  const field = form.getFieldState(item.name);
  const finalDisabled = disabled || Boolean(item.disabled) || field.pending;
  const label = (
    <label
      className="form-item__heading"
      htmlFor={controlId}
      data-testid={testIdPrefix ? `${testIdPrefix}-field-label` : undefined}
      data-variant={testIdPrefix ? String(item.name) : undefined}
    >
      <span className="form-item__label">
        {item.label}
        {showOptional && !item.required ? <span className="form-item__optional">{optionalText}</span> : null}
        {item.helpTips ? <HelpTips content={item.helpTips} /> : null}
      </span>
    </label>
  );
  const onChange = (value: TValues[keyof TValues]) => {
    void form.setFieldValue(item.name, value, { trigger: 'change' });
  };
  const onBlur = useCallback(() => {
    void form.setFieldValue(item.name, form.getFieldValue(item.name), { trigger: 'blur' });
  }, [form, item.name]);
  let control: ReactNode;
  if (item.component === 'input')
    control = (
      <Input
        id={controlId}
        type={item.type}
        value={String(field.value ?? '')}
        placeholder={item.placeholder}
        passwordVisibilityLabels={item.passwordVisibilityLabels}
        disabled={finalDisabled}
        invalid={Boolean(field.error)}
        data-testid={testIdPrefix ? `${testIdPrefix}-field-input` : undefined}
        data-variant={testIdPrefix ? String(item.name) : undefined}
        onBlur={onBlur}
        onChange={(value) => onChange(value as TValues[keyof TValues])}
      />
    );
  else if (item.component === 'textarea')
    control = (
      <Textarea
        id={controlId}
        value={String(field.value ?? '')}
        placeholder={item.placeholder}
        rows={item.rows}
        disabled={finalDisabled}
        invalid={Boolean(field.error)}
        data-testid={testIdPrefix ? `${testIdPrefix}-field-textarea` : undefined}
        data-variant={testIdPrefix ? String(item.name) : undefined}
        onBlur={onBlur}
        onChange={(value) => onChange(value as TValues[keyof TValues])}
      />
    );
  else if (item.component === 'select')
    control = (
      <Select
        id={controlId}
        value={String(field.value ?? '')}
        options={item.options}
        disabled={finalDisabled}
        invalid={Boolean(field.error)}
        data-testid={testIdPrefix ? `${testIdPrefix}-field-input` : undefined}
        data-variant={testIdPrefix ? String(item.name) : undefined}
        onBlur={onBlur}
        onChange={(value) => onChange(value as TValues[keyof TValues])}
      />
    );
  else if (item.component === 'switch')
    control = (
      <Switch
        id={controlId}
        checked={Boolean(field.value)}
        aria-label={item.switchLabel}
        disabled={finalDisabled}
        data-testid={testIdPrefix ? `${testIdPrefix}-field-toggle` : undefined}
        data-variant={testIdPrefix ? String(item.name) : undefined}
        onChange={(value) => onChange(value as TValues[keyof TValues])}
      />
    );
  else if (item.component === 'radioGroup')
    control = (
      <RadioGroup
        value={String(field.value ?? '')}
        options={item.options}
        aria-label={item.radioLabel}
        disabled={finalDisabled}
        onChange={(value) => onChange(value as TValues[keyof TValues])}
      />
    );
  else
    control = item.render({
      id: controlId,
      value: field.value,
      error: field.error,
      disabled: finalDisabled,
      pending: field.pending,
      onChange,
      onBlur,
    });
  return (
    <div
      className="form-item"
      data-testid={testIdPrefix ? `${testIdPrefix}-field` : undefined}
      data-variant={testIdPrefix ? String(item.name) : undefined}
    >
      {item.labelAction ? (
        <div className="form-item__heading-row">
          {label}
          <div className="form-item__label-action">{item.labelAction}</div>
        </div>
      ) : (
        label
      )}
      <div className="form-item__control">{control}</div>
      {field.error ? (
        <div className="form-item__error" role="alert">
          {field.error}
        </div>
      ) : null}
    </div>
  );
}

export function Form<TValues extends FormValues>({
  form,
  items,
  rules = {},
  optionalText,
  showOptional = true,
  disabled = false,
  className,
  testIdPrefix,
}: {
  form: FormStore<TValues>;
  items: readonly FormItem<TValues>[];
  rules?: FormRules<TValues>;
  optionalText: string;
  showOptional?: boolean;
  disabled?: boolean;
  className?: string;
  testIdPrefix?: string;
}) {
  const fields = useMemo(
    () =>
      Object.fromEntries(
        items.map((item) => [
          item.name,
          {
            disabled: disabled || item.disabled,
            beforeValueChange: item.beforeValueChange,
            onChange: item.onChange,
            onBlur: item.onBlur,
          },
        ]),
      ),
    [disabled, items],
  );
  useEffect(() => {
    form.configure(rules, fields as Partial<Record<keyof TValues, FormFieldOptions<TValues>>>);
  }, [fields, form, rules]);
  return (
    <form
      autoComplete="off"
      className={`form${className ? ` ${className}` : ''}`}
      data-testid={testIdPrefix ? `${testIdPrefix}-fields` : undefined}
      onSubmit={(event) => event.preventDefault()}
    >
      {items.map((item) => (
        <FormItemRenderer
          key={String(item.name)}
          form={form}
          item={item}
          optionalText={optionalText}
          showOptional={showOptional}
          disabled={disabled}
          testIdPrefix={testIdPrefix}
        />
      ))}
    </form>
  );
}
