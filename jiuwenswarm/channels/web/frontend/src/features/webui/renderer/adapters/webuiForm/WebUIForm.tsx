import { Children, createContext, useContext, useLayoutEffect, useMemo, type ReactNode } from 'react';
import { Form, type FormInstance } from 'antd';
import type { Rule } from 'antd/es/form';
import type { WebUIAdapterProps } from '../../types.ts';
import { useContentActivity } from '../../ContentActivity.tsx';

const Forms = createContext<Map<string, FormInstance> | null>(null);
export function WebUIFormsProvider({ children }: { children: ReactNode }) {
  const forms = useMemo(() => new Map<string, FormInstance>(), []);
  return <Forms.Provider value={forms}>{children}</Forms.Provider>;
}
export function useWebUIForms() { return useContext(Forms); }

export function WebUIForm({ node, context, children }: WebUIAdapterProps) {
  const forms = useWebUIForms();
  const [form] = Form.useForm();
  const active = useContentActivity();
  const name = node.props.name as string;
  useLayoutEffect(() => {
    if (!forms) throw new Error('Form requires an instance form scope');
    forms.set(name, form);
    return () => { if (forms.get(name) === form) forms.delete(name); };
  }, [forms, name, form]);
  useLayoutEffect(() => { if (!context.connected) form.resetFields(); }, [context.connected, form]);
  return <Form form={form} name={`${context.instanceKey}:${name}`} component="div"
    layout={(node.props.layout as 'vertical' | 'horizontal' | 'inline') ?? 'vertical'}
    aria-label={node.props.ariaLabel as string | undefined}
    requiredMark={node.props.requiredMark as boolean | undefined}
    disabled={!context.connected || !active || node.props.disabled === true}
    validateTrigger="onBlur" data-testid="webui-form" data-variant={name}>
    {children}
  </Form>;
}

export function WebUIFormItem({ node, children }: WebUIAdapterProps) {
  const rules = ((node.props.rules ?? []) as Record<string, unknown>[]).map((item) => ({
    ...item, type: 'string',
    ...(typeof item.pattern === 'string' ? { pattern: new RegExp(item.pattern) } : {}),
  })) as Rule[];
  const input = node.children[0];
  return <Form.Item name={[node.props.name as string]} label={node.props.label as string | undefined}
    rules={rules} initialValue={input.props.value ?? input.props.defaultValue ?? ''}
    data-testid="webui-form-item" data-variant={node.props.name as string}>
    {Children.toArray(children)[0]}
  </Form.Item>;
}
