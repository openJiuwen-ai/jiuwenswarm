/**
 * 渲染树 → React 组件映射。
 *
 * 组件与 props 白名单由后端校验（extensions_v2/webui/constants.py），这里只做
 * 1:1 映射；token 到主题变量的换算集中在本文件，插件无法直接写颜色与像素。
 */

import { useState } from 'react';
import { dispatchWebUINodeEvent } from './actionBridge';
import { WebUIActionEvent, WebUIRenderNode, WebUIRenderTarget } from './types';

const MAX_DEPTH = 8;

const GAP_CLASS: Record<string, string> = { sm: 'gap-1', md: 'gap-2', lg: 'gap-3' };
const PADDING_CLASS: Record<string, string> = { none: '', sm: 'p-1', md: 'p-2', lg: 'p-3' };
const DENSITY_CLASS: Record<string, string> = { compact: 'text-xs', comfortable: 'text-sm' };
const ALIGN_CLASS: Record<string, string> = { left: 'text-left', center: 'text-center', right: 'text-right' };
const ROW_ALIGN_CLASS: Record<string, string> = {
  left: 'justify-start',
  center: 'justify-center',
  right: 'justify-end',
};
const JUSTIFY_CLASS: Record<string, string> = {
  start: 'justify-start',
  center: 'justify-center',
  end: 'justify-end',
  between: 'justify-between',
};
const SIZE_TEXT_CLASS: Record<string, string> = { sm: 'text-xs', md: 'text-sm', lg: 'text-base' };
const TONE_TEXT_CLASS: Record<string, string> = {
  neutral: 'text-text',
  info: 'text-accent',
  success: 'text-ok',
  warning: 'text-warn',
  danger: 'text-danger',
};
const BUTTON_VARIANT_CLASS: Record<string, string> = {
  default: 'bg-bg-hover text-text hover:bg-bg-muted',
  primary: 'bg-accent text-accent-foreground hover:bg-accent-hover',
  secondary: 'bg-secondary text-secondary-foreground',
  danger: 'bg-[color:var(--color-feedback-danger)] text-[color:var(--color-feedback-danger-text)]',
  ghost: 'bg-transparent text-text hover:bg-bg-hover',
};
const BUTTON_SIZE_CLASS: Record<string, string> = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-3 py-1 text-sm',
  lg: 'px-4 py-1.5 text-base',
};
const BADGE_TONE_CLASS: Record<string, string> = {
  neutral: 'bg-bg-muted text-text-muted',
  info: 'bg-accent-subtle text-accent',
  success: 'bg-ok-subtle text-ok',
  warning: 'bg-warn-subtle text-warn',
  danger: 'bg-danger-subtle text-danger',
};
const BADGE_SIZE_CLASS: Record<string, string> = {
  sm: 'px-1.5 py-0.5 text-xs',
  md: 'px-2 py-0.5 text-sm',
  lg: 'px-2.5 py-1 text-base',
};
const ICON_GLYPH: Record<string, string> = {
  check: '✓',
  close: '✕',
  info: 'ℹ',
  warning: '⚠',
  file: '📄',
  folder: '📁',
  play: '▶',
  refresh: '⟳',
};
const ICON_SIZE_CLASS: Record<string, string> = { sm: 'text-xs', md: 'text-sm', lg: 'text-base' };
const WEIGHT_CLASS: Record<string, string> = {
  regular: 'font-normal',
  medium: 'font-medium',
  bold: 'font-semibold',
};
const DIVIDER_SPACING_CLASS: Record<string, string> = { none: 'my-0', sm: 'my-1', md: 'my-2', lg: 'my-3' };
const TONE_BAR_CLASS: Record<string, string> = {
  neutral: 'bg-accent',
  info: 'bg-accent',
  success: 'bg-ok',
  warning: 'bg-warn',
  danger: 'bg-danger',
};

function readText(props: Record<string, unknown>, key: string, fallback = ''): string {
  const value = props[key];
  return typeof value === 'string' ? value : fallback;
}

function readNumber(props: Record<string, unknown>, key: string, fallback = 0): number {
  const value = props[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function readFlag(props: Record<string, unknown>, key: string, fallback = false): boolean {
  const value = props[key];
  return typeof value === 'boolean' ? value : fallback;
}

function readRows(props: Record<string, unknown>, key: string): Record<string, unknown>[] {
  const value = props[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object');
}

function readScalarList(props: Record<string, unknown>, key: string): string[] {
  const value = props[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item));
}

function tokenClass(map: Record<string, string>, props: Record<string, unknown>, key: string, fallback: string): string {
  const token = readText(props, key);
  return map[token] ?? map[fallback] ?? '';
}

export interface RenderTreeProps {
  node: WebUIRenderNode | null;
  target: WebUIRenderTarget;
  revision: number;
}

export function RenderTree({ node, target, revision }: RenderTreeProps) {
  if (!node) {
    return null;
  }
  return (
    <div className="webui-render-tree flex flex-col gap-2" data-testid="webui-render-tree">
      <RenderNodeView node={node} target={target} revision={revision} nodeKey="0" depth={1} />
    </div>
  );
}

interface RenderNodeProps {
  node: WebUIRenderNode;
  target: WebUIRenderTarget;
  revision: number;
  nodeKey: string;
  depth: number;
}

function RenderNodeView({ node, target, revision, nodeKey, depth }: RenderNodeProps) {
  const props = node.props ?? {};
  const children = node.children ?? [];
  const event: WebUIActionEvent = node.events
    ? node.component === 'Button' ? 'ui.click' : 'ui.change'
    : node.component === 'TextField' ? 'ui.input' : node.component === 'CheckBox' ? 'ui.toggle'
      : node.component === 'MultipleChoice' || node.component === 'Slider' ? 'ui.select' : 'ui.press';
  const actionId = (node.events ? node.events[event] : node.action)?.id ?? '';
  const dispatch = (data: Record<string, unknown> = {}) => {
    const eventData = node.events && node.component === 'Slider' ? { value: data.value } : data;
    void dispatchWebUINodeEvent(node, event, target, revision, nodeKey, eventData);
  };

  if (depth > MAX_DEPTH) {
    return null;
  }

  const renderChildren = () =>
    children.map((child, index) => (
      <RenderNodeView key={`${child.component}-${index}`} node={child} target={target}
        revision={revision} nodeKey={`${nodeKey}.${index}`} depth={depth + 1} />
    ));

  switch (node.component) {
    case 'Column':
      return (
        <div
          className={`flex flex-col ${tokenClass(GAP_CLASS, props, 'gap', 'md')} ${tokenClass(
            ALIGN_CLASS,
            props,
            'align',
            'left',
          )} ${tokenClass(JUSTIFY_CLASS, props, 'justify', 'start')} ${tokenClass(
            PADDING_CLASS,
            props,
            'padding',
            'none',
          )} ${readFlag(props, 'wrap') ? 'flex-wrap' : ''}`}
          data-testid="webui-column"
        >
          {renderChildren()}
        </div>
      );
    case 'Row': {
      const justify = readText(props, 'justify')
        ? tokenClass(JUSTIFY_CLASS, props, 'justify', 'start')
        : tokenClass(ROW_ALIGN_CLASS, props, 'align', 'left');
      return (
        <div
          className={`flex flex-row items-center ${tokenClass(GAP_CLASS, props, 'gap', 'md')} ${justify} ${tokenClass(
            PADDING_CLASS,
            props,
            'padding',
            'none',
          )} ${readFlag(props, 'wrap') ? 'flex-wrap' : ''}`}
          data-testid="webui-row"
        >
          {renderChildren()}
        </div>
      );
    }
    case 'Card':
      return (
        <div
          className={`rounded-lg border border-border bg-card ${tokenClass(PADDING_CLASS, props, 'padding', 'md')} ${
            DENSITY_CLASS[readText(props, 'density')] ?? ''
          }`}
          data-testid="webui-card"
        >
          {readText(props, 'title') ? (
            <div className={`mb-1 font-medium ${tokenClass(TONE_TEXT_CLASS, props, 'tone', 'neutral')}`}>
              {readText(props, 'title')}
            </div>
          ) : null}
          <div className="flex flex-col gap-2">{renderChildren()}</div>
        </div>
      );
    case 'Tabs': {
      const items = readRows(props, 'items');
      const selected = readText(props, 'selected');
      return (
        <div className="flex flex-col gap-2" data-testid="webui-tabs">
          <div className="flex flex-row gap-3 border-b border-border">
            {items.map((item) => {
              const key = String(item.key ?? '');
              return (
                <span
                  key={key}
                  className={`pb-1 text-sm ${
                    key === selected ? 'border-b-2 border-accent text-text' : 'text-text-muted'
                  }`}
                  data-testid="webui-tabs-item"
                  data-variant={key}
                >
                  {String(item.label ?? key)}
                </span>
              );
            })}
          </div>
          {renderChildren()}
        </div>
      );
    }
    case 'Divider':
      return (
        <hr
          className={`border-border ${tokenClass(DIVIDER_SPACING_CLASS, props, 'spacing', 'md')} ${
            readText(props, 'axis') === 'vertical' ? 'h-full w-px border-l' : 'w-full border-t'
          }`}
          data-testid="webui-divider"
        />
      );
    case 'Text': {
      const maxLines = readNumber(props, 'max_lines', 0);
      return (
        <span
          className={`${tokenClass(SIZE_TEXT_CLASS, props, 'size', 'md')} ${tokenClass(
            TONE_TEXT_CLASS,
            props,
            'tone',
            'neutral',
          )} ${WEIGHT_CLASS[readText(props, 'weight')] ?? ''} ${
            readText(props, 'variant') === 'secondary' ? 'text-text-muted' : ''
          } ${readFlag(props, 'monospace') ? 'font-mono' : ''}`}
          style={maxLines > 0 ? { display: '-webkit-box', WebkitLineClamp: maxLines, WebkitBoxOrient: 'vertical', overflow: 'hidden' } : undefined}
          data-testid="webui-text"
        >
          {readText(props, 'value')}
        </span>
      );
    }
    case 'Icon': {
      const name = readText(props, 'name');
      return (
        <span
          className={`inline-flex shrink-0 items-center ${tokenClass(ICON_SIZE_CLASS, props, 'size', 'md')} ${tokenClass(
            TONE_TEXT_CLASS,
            props,
            'tone',
            'neutral',
          )}`}
          aria-label={name}
          data-testid="webui-icon"
          data-variant={name}
        >
          {ICON_GLYPH[name] ?? '•'}
        </span>
      );
    }
    case 'Badge':
      return (
        <span
          className={`inline-flex shrink-0 items-center rounded-full font-medium ${tokenClass(
            BADGE_SIZE_CLASS,
            props,
            'size',
            'sm',
          )} ${tokenClass(BADGE_TONE_CLASS, props, 'tone', 'neutral')}`}
          data-testid="webui-badge"
          data-variant={readText(props, 'tone') || 'neutral'}
        >
          {readText(props, 'label')}
        </span>
      );
    case 'Button':
      return (
        <button
          type="button"
          disabled={readFlag(props, 'disabled')}
          onClick={
            actionId
              ? () => dispatch()
              : undefined
          }
          className={`rounded ${tokenClass(BUTTON_VARIANT_CLASS, props, 'variant', 'default')} ${tokenClass(
            BUTTON_SIZE_CLASS,
            props,
            'size',
            'md',
          )} disabled:cursor-not-allowed disabled:opacity-50`}
          data-testid="webui-button"
          data-variant={actionId || 'no-action'}
        >
          {readText(props, 'label')}
        </button>
      );
    case 'TextField':
      return (
        <TextFieldNode
          key={`${readText(props, 'name')}:${readText(props, 'value')}`}
          name={readText(props, 'name')}
          placeholder={readText(props, 'placeholder')}
          initialValue={readText(props, 'value')}
          maxLength={readNumber(props, 'max_length', 0)}
          disabled={readFlag(props, 'disabled')}
          actionId={actionId}
          dispatch={dispatch}
        />
      );
    case 'CheckBox':
      return (
        <CheckBoxNode
          name={readText(props, 'name')}
          label={readText(props, 'label')}
          checked={readFlag(props, 'checked')}
          disabled={readFlag(props, 'disabled')}
          actionId={actionId}
          dispatch={dispatch}
        />
      );
    case 'MultipleChoice':
      return (
        <MultipleChoiceNode
          name={readText(props, 'name')}
          options={readRows(props, 'options')}
          selected={[...readScalarList(props, 'value')]}
          multiple={readFlag(props, 'multiple')}
          disabled={readFlag(props, 'disabled')}
          actionId={actionId}
          dispatch={dispatch}
        />
      );
    case 'Slider':
      return (
        <SliderNode
          key={`${readText(props, 'name')}:${readNumber(props, 'value', 0)}`}
          name={readText(props, 'name')}
          min={readNumber(props, 'min', 0)}
          max={readNumber(props, 'max', 100)}
          step={readNumber(props, 'step', 1)}
          initialValue={readNumber(props, 'value', 0)}
          disabled={readFlag(props, 'disabled')}
          actionId={actionId}
          dispatch={dispatch}
        />
      );
    case 'List': {
      const items = readRows(props, 'items');
      const ordered = readFlag(props, 'ordered');
      const selected = readScalarList(props, 'selected');
      const ListTag = ordered ? 'ol' : 'ul';
      return (
        <ListTag className="flex flex-col gap-1" data-testid="webui-list">
          {items.map((item) => {
            const key = String(item.key ?? '');
            const tone = tokenClass(TONE_TEXT_CLASS, item, 'tone', 'neutral');
            return (
              <li
                key={key}
                className={`flex flex-row items-start gap-2 rounded px-1 py-0.5 ${
                  selected.includes(key) ? 'bg-bg-hover' : ''
                } ${actionId ? 'cursor-pointer hover:bg-bg-hover' : ''}`}
                onClick={
                  actionId
                    ? () => dispatch({ key })
                    : undefined
                }
                data-testid="webui-list-item"
                data-variant={key}
              >
                {readText(item, 'icon') ? (
                  <span className={`inline-flex shrink-0 items-center ${tone}`} aria-hidden="true">
                    {ICON_GLYPH[readText(item, 'icon')] ?? '•'}
                  </span>
                ) : null}
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className={`text-sm ${tone} ${readFlag(item, 'monospace') ? 'font-mono' : ''}`}>
                    {String(item.title ?? key)}
                  </span>
                  {item.subtitle ? <span className="text-xs text-text-muted">{String(item.subtitle)}</span> : null}
                </span>
                {item.meta ? (
                  <span className="shrink-0 font-mono text-xs text-text-meta">{String(item.meta)}</span>
                ) : null}
              </li>
            );
          })}
        </ListTag>
      );
    }
    case 'Progress': {
      const value = Math.max(0, Math.min(100, readNumber(props, 'value', 0)));
      return (
        <div className="flex flex-col gap-1" data-testid="webui-progress" data-variant={String(value)}>
          {readText(props, 'label') ? <span className="text-xs text-text-muted">{readText(props, 'label')}</span> : null}
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-bg-muted">
            <div
              className={`h-full rounded-full ${tokenClass(TONE_BAR_CLASS, props, 'tone', 'neutral')}`}
              style={{ width: `${value}%` }}
            />
          </div>
        </div>
      );
    }
    default:
      // 后端 schema 只放行白名单组件；未知组件静默忽略，避免旧前端被新组件打崩。
      return null;
  }
}

interface InteractiveProps {
  name: string;
  disabled: boolean;
  actionId: string;
  dispatch: (data: Record<string, unknown>) => void;
}

function TextFieldNode({
  name,
  placeholder,
  initialValue,
  maxLength,
  disabled,
  actionId,
  dispatch,
}: InteractiveProps & { placeholder: string; initialValue: string; maxLength: number }) {
  const [value, setValue] = useState(initialValue);
  const submit = () => {
    if (actionId) {
      dispatch({ name, value });
    }
  };
  return (
    <input
      type="text"
      value={value}
      placeholder={placeholder}
      maxLength={maxLength > 0 ? maxLength : undefined}
      disabled={disabled}
      onChange={(event) => setValue(event.target.value)}
      onBlur={submit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          submit();
        }
      }}
      className="rounded border border-border bg-bg-content px-2 py-1 text-sm text-text disabled:opacity-50"
      data-testid="webui-text-field"
      data-variant={name || 'unnamed'}
    />
  );
}

function CheckBoxNode({
  name,
  label,
  checked,
  disabled,
  actionId,
  dispatch,
}: InteractiveProps & { label: string; checked: boolean }) {
  return (
    <label className="inline-flex items-center gap-2 text-sm text-text" data-testid="webui-checkbox" data-variant={name || 'unnamed'}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => {
          if (actionId) {
            dispatch({
              name,
              checked: event.target.checked,
            });
          }
        }}
      />
      <span>{label}</span>
    </label>
  );
}

function MultipleChoiceNode({
  name,
  options,
  selected,
  multiple,
  disabled,
  actionId,
  dispatch,
}: InteractiveProps & {
  options: Record<string, unknown>[];
  selected: string[];
  multiple: boolean;
}) {
  const commit = (value: string) => {
    if (!actionId) {
      return;
    }
    const next = multiple
      ? selected.includes(value)
        ? selected.filter((item) => item !== value)
        : [...selected, value]
      : [value];
    dispatch({ name, value: next });
  };

  if (!multiple) {
    return (
      <select
        value={selected[0] ?? ''}
        disabled={disabled}
        onChange={(event) => commit(event.target.value)}
        className="rounded border border-border bg-bg-content px-2 py-1 text-sm text-text"
        data-testid="webui-multiple-choice"
        data-variant={name || 'unnamed'}
      >
        <option value="" />
        {options.map((option) => {
          const key = String(option.key ?? '');
          return (
            <option key={key} value={key}>
              {String(option.label ?? key)}
            </option>
          );
        })}
      </select>
    );
  }

  return (
    <div className="flex flex-col gap-1" data-testid="webui-multiple-choice" data-variant={name || 'unnamed'}>
      {options.map((option) => {
        const key = String(option.key ?? '');
        return (
          <label key={key} className="inline-flex items-center gap-2 text-sm text-text">
            <input
              type="checkbox"
              checked={selected.includes(key)}
              disabled={disabled}
              onChange={() => commit(key)}
              data-testid="webui-multiple-choice-option"
              data-variant={key}
            />
            <span>{String(option.label ?? key)}</span>
          </label>
        );
      })}
    </div>
  );
}

function SliderNode({
  name,
  min,
  max,
  step,
  initialValue,
  disabled,
  actionId,
  dispatch,
}: InteractiveProps & { min: number; max: number; step: number; initialValue: number }) {
  const [value, setValue] = useState(initialValue);
  const commit = (next: number) => {
    if (actionId) {
      dispatch({ name, value: next });
    }
  };
  return (
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      onChange={(event) => setValue(Number(event.target.value))}
      onPointerUp={() => commit(value)}
      onKeyUp={() => commit(value)}
      className="w-full"
      data-testid="webui-slider"
      data-variant={name || 'unnamed'}
    />
  );
}

