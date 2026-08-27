import type {
  FormBeforeValueChange,
  FormHookResult,
  FormInstance,
  FormRule,
  FormRuleTrigger,
  FormRules,
  FormState,
  FormValues,
  FormValidateResult,
} from '../types';

type Listener = () => void;
export type FormFieldOptions<TValues extends FormValues> = {
  disabled?: boolean;
  beforeValueChange?: readonly FormBeforeValueChange<TValues, keyof TValues>[];
  onChange?: (value: TValues[keyof TValues], values: Readonly<TValues>) => void;
  onBlur?: (value: TValues[keyof TValues], values: Readonly<TValues>) => void;
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertFormValue(value: unknown, ancestors = new Set<object>()): void {
  if (value === null || typeof value !== 'object') return;
  if (ancestors.has(value)) throw new Error('Form values must not contain circular references');
  if (!Array.isArray(value) && !isPlainObject(value))
    throw new Error('Form values must use only plain objects and arrays');
  ancestors.add(value);
  for (const entry of Object.values(value)) assertFormValue(entry, ancestors);
  ancestors.delete(value);
}

export function deepEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) || Array.isArray(right))
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => deepEqual(value, right[index]))
    );
  if (!isPlainObject(left) || !isPlainObject(right)) return false;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every((key) => Object.prototype.hasOwnProperty.call(right, key) && deepEqual(left[key], right[key]))
  );
}

function copy<TValues extends FormValues>(values: TValues): TValues {
  return { ...values };
}
function asNames<TValues extends FormValues>(
  names: keyof TValues | readonly (keyof TValues)[] | undefined,
  values: TValues,
): (keyof TValues)[] {
  if (names === undefined) return Object.keys(values) as (keyof TValues)[];
  return Array.isArray(names) ? ([...names] as (keyof TValues)[]) : [names as keyof TValues];
}

export class FormStore<TValues extends FormValues> implements FormInstance<TValues> {
  private values: TValues;
  private baseline: TValues;
  private errors: Partial<Record<keyof TValues, string>> = {};
  private touched: Partial<Record<keyof TValues, boolean>> = {};
  private pending: Partial<Record<keyof TValues, boolean>> = {};
  private rules: FormRules<TValues> = {};
  private fields: Partial<Record<keyof TValues, FormFieldOptions<TValues>>> = {};
  private listeners = new Set<Listener>();
  private revision = 0;

  constructor(initialValues: TValues) {
    assertFormValue(initialValues);
    this.values = copy(initialValues);
    this.baseline = copy(initialValues);
  }
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getRevision = (): number => this.revision;
  getState = (): FormState<TValues> => ({
    errors: { ...this.errors },
    touched: { ...this.touched },
    hasUnsavedChanges: this.hasUnsavedChanges(),
  });
  getFieldState = <K extends keyof TValues>(name: K) => ({
    value: this.values[name],
    error: this.errors[name],
    touched: Boolean(this.touched[name]),
    pending: Boolean(this.pending[name]),
    disabled: Boolean(this.fields[name]?.disabled),
  });
  configure = (rules: FormRules<TValues>, fields: Partial<Record<keyof TValues, FormFieldOptions<TValues>>>) => {
    this.rules = rules;
    this.fields = fields;
    for (const [name, field] of Object.entries(fields) as [keyof TValues, FormFieldOptions<TValues>][])
      if (field.disabled) delete this.errors[name];
  };
  getValues(): TValues {
    return copy(this.values);
  }
  getFieldValue<K extends keyof TValues>(name: K): TValues[K] {
    return this.values[name];
  }
  setFieldValue<K extends keyof TValues>(name: K, value: TValues[K]): void;
  setFieldValue<K extends keyof TValues>(
    name: K,
    value: TValues[K],
    options: { trigger: 'change' | 'blur' },
  ): Promise<FormHookResult>;
  setFieldValue<K extends keyof TValues>(
    name: K,
    value: TValues[K],
    options?: { trigger: 'change' | 'blur' },
  ): void | Promise<FormHookResult> {
    return options ? this.applyEvent(name, value, options.trigger) : this.write(name, value);
  }
  setValues(values: Partial<TValues>): void {
    const next = { ...this.values, ...values };
    assertFormValue(next);
    this.values = next;
    this.emit();
  }
  validate(names?: keyof TValues | readonly (keyof TValues)[]): FormValidateResult<TValues> {
    const targetNames = asNames(names, this.values);
    const errors: Partial<Record<keyof TValues, string>> = {};
    for (const name of targetNames) {
      const error = this.runRules(name);
      if (error) errors[name] = error;
    }
    this.errors = { ...this.errors, ...errors };
    for (const name of targetNames) if (!errors[name]) delete this.errors[name];
    this.emit();
    return Object.keys(errors).length ? { valid: false, errors } : { valid: true, values: this.getValues() };
  }
  clearValidate(names?: keyof TValues | readonly (keyof TValues)[]): void {
    for (const name of asNames(names, this.values)) delete this.errors[name];
    this.emit();
  }
  reset(nextValues?: TValues): void {
    if (nextValues) {
      assertFormValue(nextValues);
      this.values = copy(nextValues);
      this.baseline = copy(nextValues);
    } else this.values = copy(this.baseline);
    this.errors = {};
    this.touched = {};
    this.pending = {};
    this.emit();
  }
  hasUnsavedChanges(name?: keyof TValues): boolean {
    return name === undefined
      ? !deepEqual(this.values, this.baseline)
      : !deepEqual(this.values[name], this.baseline[name]);
  }

  private write<K extends keyof TValues>(name: K, value: TValues[K]): void {
    const next = { ...this.values, [name]: value };
    assertFormValue(next);
    this.values = next;
    this.emit();
  }
  private runRules(name: keyof TValues, trigger?: FormRuleTrigger): string | undefined {
    if (this.fields[name]?.disabled) return undefined;
    const rules = this.rules[name] as readonly FormRule<TValues, keyof TValues>[] | undefined;
    for (const rule of rules ?? []) {
      const triggers =
        rule.trigger === undefined ? undefined : Array.isArray(rule.trigger) ? rule.trigger : [rule.trigger];
      if (trigger !== undefined && (!triggers || !triggers.includes(trigger))) continue;
      const error = rule.validator(this.values[name], this.values);
      if (error) return error;
    }
    return undefined;
  }
  private async applyEvent<K extends keyof TValues>(
    name: K,
    value: TValues[K],
    trigger: FormRuleTrigger,
  ): Promise<FormHookResult> {
    const field = this.fields[name];
    if (field?.disabled) return { status: 'cancelled' };
    if (trigger === 'change') {
      this.pending[name] = true;
      this.emit();
      try {
        for (const hook of field?.beforeValueChange ?? []) {
          const result = await hook(value, this.values);
          if (result.status !== 'passed') {
            if (result.status === 'failed' && result.error) this.errors[name] = result.error;
            return result;
          }
        }
      } finally {
        delete this.pending[name];
      }
      this.write(name, value);
      const error = this.runRules(name, 'change');
      if (error) this.errors[name] = error;
      else delete this.errors[name];
      field?.onChange?.(value, this.getValues());
      this.emit();
      return { status: 'passed' };
    }
    this.write(name, value);
    this.touched[name] = true;
    const error = this.runRules(name, 'blur');
    if (error) this.errors[name] = error;
    else delete this.errors[name];
    field?.onBlur?.(value, this.getValues());
    this.emit();
    return { status: 'passed' };
  }
  private emit(): void {
    this.revision += 1;
    this.listeners.forEach((listener) => listener());
  }
}
