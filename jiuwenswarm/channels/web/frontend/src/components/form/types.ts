import type { ReactNode } from 'react';
import type { SelectOption, RadioOption } from '../ui';

export type FormValues = Record<string, unknown>;
export type FormRuleTrigger = 'change' | 'blur';
export type FormHookResult = { status: 'passed' } | { status: 'failed'; error?: string } | { status: 'cancelled' };

export type FormRule<TValues extends FormValues, TName extends keyof TValues> = {
  trigger?: FormRuleTrigger | readonly FormRuleTrigger[];
  validator: (value: TValues[TName], values: Readonly<TValues>) => string | undefined;
};

export type FormRules<TValues extends FormValues> = { [K in keyof TValues]?: readonly FormRule<TValues, K>[] };
export type FormBeforeValueChange<TValues extends FormValues, TName extends keyof TValues> = (
  nextValue: TValues[TName],
  values: Readonly<TValues>,
) => FormHookResult | Promise<FormHookResult>;
export type FormFieldRenderProps<TValue> = {
  id: string;
  value: TValue;
  error?: string;
  disabled: boolean;
  pending: boolean;
  onChange: (nextValue: TValue) => void;
  onBlur: () => void;
};

type FormItemBase<TValues extends FormValues, TName extends keyof TValues> = {
  name: TName;
  label: ReactNode;
  labelAction?: ReactNode;
  required?: boolean;
  helpTips?: string;
  disabled?: boolean;
  beforeValueChange?: readonly FormBeforeValueChange<TValues, TName>[];
  onChange?: (value: TValues[TName], values: Readonly<TValues>) => void;
  onBlur?: (value: TValues[TName], values: Readonly<TValues>) => void;
};

export type FormItem<TValues extends FormValues> =
  | (FormItemBase<TValues, keyof TValues> & {
      component: 'input';
      type?: 'text' | 'password' | 'number';
      placeholder?: string;
      passwordVisibilityLabels?: { show: string; hide: string };
    })
  | (FormItemBase<TValues, keyof TValues> & { component: 'textarea'; placeholder?: string; rows?: number })
  | (FormItemBase<TValues, keyof TValues> & { component: 'select'; options: readonly SelectOption[] })
  | (FormItemBase<TValues, keyof TValues> & { component: 'switch'; switchLabel: string })
  | (FormItemBase<TValues, keyof TValues> & {
      component: 'radioGroup';
      options: readonly RadioOption[];
      radioLabel: string;
    })
  | (FormItemBase<TValues, keyof TValues> & {
      component: 'custom';
      render: (props: FormFieldRenderProps<TValues[keyof TValues]>) => ReactNode;
    });

export type FormValidateResult<TValues extends FormValues> =
  { valid: true; values: TValues } | { valid: false; errors: Partial<Record<keyof TValues, string>> };
export type FormState<TValues extends FormValues> = {
  errors: Partial<Record<keyof TValues, string>>;
  touched: Partial<Record<keyof TValues, boolean>>;
  hasUnsavedChanges: boolean;
};

export interface FormInstance<TValues extends FormValues> {
  getValues(): TValues;
  getFieldValue<K extends keyof TValues>(name: K): TValues[K];
  setFieldValue<K extends keyof TValues>(name: K, value: TValues[K]): void;
  setFieldValue<K extends keyof TValues>(
    name: K,
    value: TValues[K],
    options: { trigger: 'change' | 'blur' },
  ): Promise<FormHookResult>;
  setValues(values: Partial<TValues>): void;
  validate(names?: keyof TValues | readonly (keyof TValues)[]): FormValidateResult<TValues>;
  clearValidate(names?: keyof TValues | readonly (keyof TValues)[]): void;
  reset(nextValues?: TValues): void;
  hasUnsavedChanges(name?: keyof TValues): boolean;
}
