import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ImagePlus, Trash2, Plus } from 'lucide-react';
import { webRequest } from '../../services/webClient';
import { useConnectorStore } from '../../stores/connectorStore';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { computeMySkills, buildInstalledSkillNames, filterEnabledMySkills } from '../../utils/mySkills';
import { Input } from '../ui/Input/Input';
import { Textarea } from '../ui/Textarea/Textarea';
import { PageCard } from '../ui';
import { PickerModal, type PickerItem } from './PickerModal';
import { FormPageLayout } from './FormPageLayout';

const DESCRIPTION_MAX = 512;

// 头像上传入口暂时隐藏：后端 plugin_packages.create 没有头像/图标字段
// （backend-requests.md 需求9），选中的图片只能本地预览、保存不了。等后端支持后把这个
// 常量翻成 true 即可恢复整段 UI（相关 state / handleAvatarSelect / revoke effect 都保留着）。
const AVATAR_UPLOAD_ENABLED = false;

type RequiredFieldKey = 'id' | 'name' | 'description';

interface SkillItem {
  name: string;
  display_name?: string;
  description: string;
  // computeMySkills（utils/mySkills.ts）判定"我的技能"要用到的字段，跟 SkillPanel/index.tsx
  // 的字段语义一致：source==='local' 是用户自己的技能，is_builtin(_source) 是内置/广场技能。
  source?: string;
  is_builtin?: boolean;
  is_builtin_source?: boolean;
  // filterEnabledMySkills 判定"已启用"要用到——跟技能管理页"我的技能"tab 默认只显示已启用
  // 技能同一份规则，2026-08-25 之前这里没有这个字段，弹窗里会把已停用的技能也列出来。
  enabled?: boolean;
}

/** skills.list 响应里的 plugins 字段——只取 computeMySkills/buildInstalledSkillNames 需要的
 * skills 名单，跟 SkillPanel/index.tsx 的 InstalledPluginItem 是同一个后端形状，这里不需要
 * 其余字段。skills 里每一项可能是纯字符串，也可能是 `{name, version}` 对象，两种形状都要处理
 * （2026-08-25 之前这里声明成 string[]，跟实际形状不符，buildInstalledSkillNames 因此漏判）。 */
interface InstalledPluginItem {
  skills: (string | { name: string; version?: string | null })[];
}

interface CreatePluginPageProps {
  onBack: () => void;
  onCreated: () => void;
}

// 对应高保真 3.1 手动创建插件。"选择技能"复用现有 skills.list 接口（不需要问后端新增，
// 见 backend-requests.md 附注），"选择MCP"用 connectorStore 已加载的市场列表。
// 提交调 pluginPackageStore.create——2026-08-07 对齐专家与插件装备-前端接口(3).md §3.3 真实参数
// 形状 {id, name, description, skills}：id 是必填目录名，手动输入或按名称自动建议一个 slug，
// 用户可编辑；name/description 是纯字符串，不再是双语对象。
// 2026-08-21：后端 create_plugin_package 补上了 mcps 参数（extension_package_manager.py
// _require_mcp_names，connector 名称数组，可选），mcpIds 选择现在会真的带进 create() 提交
// ——之前这里没有承载位，选了也是纯本地展示，backend-requests.md 需求 2 已解决。
// 头像选择：点击可以真的打开文件选择器并本地预览（上一轮这里只是个纯静态图标，点了没反应），
// 但 plugin_packages.* 完全没有图标字段（backend-requests.md 需求9），选中的图片选不进
// create() 的参数里，只能停留在本地预览——选好图片后额外提示一句，不让用户误以为真的保存了。
// 选择弹窗内的图标沿用 getSkillAvatar（跟技能面板同一套头像样式，2026-08-19 整合，
// 见 utils/skillAvatar.ts 头部注释），但用户要求整体放大——从原来的 h-6 w-6 提到 h-8 w-8。
function toSkillPickerItems(items: SkillItem[]): PickerItem[] {
  return items.map((skill): PickerItem => {
    const label = skill.display_name || skill.name;
    return {
      id: skill.name,
      name: label,
      description: skill.description,
    };
  });
}

function toMcpPickerItem(mcp: {
  name: string;
  displayName: string;
  description: string;
  icon?: string | null;
}): PickerItem {
  return {
    id: mcp.name,
    name: mcp.displayName,
    description: mcp.description,
    iconUrl: mcp.icon ?? undefined,
  };
}

function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9一-龥]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

export function CreatePluginPage({ onBack, onCreated }: CreatePluginPageProps) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [id, setId] = useState('');
  const [idTouched, setIdTouched] = useState(false);
  const [description, setDescription] = useState('');
  const [avatarPreviewUrl, setAvatarPreviewUrl] = useState<string | null>(null);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [installedSkillNames, setInstalledSkillNames] = useState<Set<string>>(new Set());
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [mcpIds, setMcpIds] = useState<string[]>([]);
  const [picker, setPicker] = useState<'skill' | 'mcp' | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 必填项前端拦截：后端 create_plugin_package 对 id/name/description 都做非空校验
  // （extension_package_manager.py _require_nonempty_str），留空提交会被后端拒。这里在
  // 点"确定"时先本地逐项校验，命中的项红框 + 下方提示，不再依赖按钮置灰。
  const [fieldErrors, setFieldErrors] = useState<Record<RequiredFieldKey, boolean>>({
    id: false,
    name: false,
    description: false,
  });

  const clearFieldError = (key: RequiredFieldKey) =>
    setFieldErrors((prev) => (prev[key] ? { ...prev, [key]: false } : prev));

  const connectors = useConnectorStore((s) => s.connectors);
  const myConnectors = useConnectorStore((s) => s.myConnectors);
  const connectorLoading = useConnectorStore((s) => s.isLoading);
  const loadConnectorList = useConnectorStore((s) => s.loadList);
  const createPlugin = usePluginPackageStore((s) => s.create);

  // 2026-08-19 用户明确要求："选择技能"/"选择MCP"弹窗要真的向后端拉数据，不能只在
  // CreatePluginPage 挂载时统一拉一次、之后弹窗里所有交互都是纯前端过滤旧数据。
  // 2026-08-21 用户明确要求去掉"我的/广场"两个 tab，只展示"我的"——不用再拉广场那份数据，
  // "选择MCP"只调 loadConnectorList('local')（见下面按钮 onClick），"选择技能"这里的
  // skills.list 本来就是一次性把 skills+plugins 都拿回来（跟 SkillPanel/index.tsx 的
  // fetchSkills 同一个接口/同一次调用），之前只取了 payload.skills、没接 payload.plugins，
  // 导致 computeMySkills 缺了"installedSkillNames"这个候选条件，"我的技能"少算了通过插件
  // 装进来的那些（用户反馈的根因）。
  function loadSkills() {
    setSkillsLoading(true);
    webRequest<{ skills?: SkillItem[]; plugins?: InstalledPluginItem[] }>('skills.list', { with_installed: true })
      .then((payload) => {
        setSkills(payload.skills ?? []);
        setInstalledSkillNames(buildInstalledSkillNames(payload.plugins ?? []));
      })
      .catch(() => {
        setSkills([]);
        setInstalledSkillNames(new Set());
      })
      .finally(() => setSkillsLoading(false));
  }

  useEffect(() => {
    return () => {
      if (avatarPreviewUrl) URL.revokeObjectURL(avatarPreviewUrl);
    };
  }, [avatarPreviewUrl]);

  function handleAvatarSelect(file: File | undefined) {
    if (!file) return;
    setAvatarPreviewUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return URL.createObjectURL(file);
    });
  }

  const selectedSkills = skills.filter((s) => skillIds.includes(s.name));
  const selectedMcps = connectors.filter((c) => mcpIds.includes(c.name));

  // 复用 SkillPanel/index.tsx"我的技能"tab 同一套判定规则（含默认只显示已启用），见
  // utils/mySkills.ts 头注释。
  const myPickerSkills = useMemo(
    () => filterEnabledMySkills(computeMySkills(skills, installedSkillNames), installedSkillNames),
    [skills, installedSkillNames],
  );

  async function handleSubmit() {
    const nextErrors: Record<RequiredFieldKey, boolean> = {
      id: !id.trim(),
      name: !name.trim(),
      description: !description.trim(),
    };
    if (nextErrors.id || nextErrors.name || nextErrors.description) {
      setFieldErrors(nextErrors);
      return;
    }
    setFieldErrors({ id: false, name: false, description: false });
    setSubmitting(true);
    setSubmitError(null);
    const ok = await createPlugin({
      id,
      name,
      description,
      skills: skillIds,
      mcps: mcpIds,
    });
    setSubmitting(false);
    if (ok) {
      onCreated();
    } else {
      setSubmitError(t('connectorMarket.create.submitError'));
    }
  }

  return (
    <>
      <FormPageLayout
        onBack={onBack}
        title={t('connectorMarket.create.manual')}
        testId="connector-market-create-plugin-page"
        onConfirm={handleSubmit}
        cancelLabel={t('connectorMarket.common.cancel')}
        confirmLabel={t('connectorMarket.common.confirm')}
        confirmLoading={submitting}
      >
      <Section title={t('connectorMarket.create.basicInfo')}>
        {AVATAR_UPLOAD_ENABLED && (
          <div className="mb-4 flex items-center gap-3">
            <label
              className="flex h-14 w-14 shrink-0 cursor-pointer items-center justify-center overflow-hidden rounded-2xl bg-bg-muted text-text-muted hover:bg-bg"
              data-testid="connector-market-create-plugin-avatar"
            >
              {avatarPreviewUrl ? (
                <img src={avatarPreviewUrl} alt="" className="h-full w-full object-cover" />
              ) : (
                <ImagePlus size={22} />
              )}
              <input
                type="file"
                accept="image/png,image/jpeg,image/gif"
                className="hidden"
                onChange={(event) => handleAvatarSelect(event.target.files?.[0])}
              />
            </label>
            <div>
              <p className="text-[12px] leading-[18px] text-text-muted">{t('connectorMarket.create.uploadHint')}</p>
              {avatarPreviewUrl && (
                <p className="mt-0.5 text-[11px] leading-4 text-[color:var(--color-text-placeholder)]">
                  {t('connectorMarket.create.avatarNotPersisted')}
                </p>
              )}
            </div>
          </div>
        )}

        <div className="mb-4">
          <label className="mb-1.5 block text-[13px] font-medium text-text">{t('connectorMarket.create.name')}</label>
          <Input
            value={name}
            onChange={(nextName) => {
              setName(nextName);
              clearFieldError('name');
              if (!idTouched) {
                const nextId = slugify(nextName);
                setId(nextId);
                if (nextId) clearFieldError('id');
              }
            }}
            invalid={fieldErrors.name}
            data-testid="connector-market-create-plugin-name"
          />
          {fieldErrors.name && (
            <p
              className="mt-1 text-[11px] leading-4 text-danger"
              data-testid="connector-market-create-plugin-field-error"
              data-variant="name"
            >
              {t('connectorMarket.create.fieldRequired')}
            </p>
          )}
        </div>

        <div className="mb-4">
          <label className="mb-1.5 block text-[13px] font-medium text-text">{t('connectorMarket.create.id')}</label>
          <Input
            value={id}
            onChange={(nextId) => {
              setIdTouched(true);
              setId(nextId);
              clearFieldError('id');
            }}
            placeholder={t('connectorMarket.create.idPlaceholder')}
            invalid={fieldErrors.id}
            data-testid="connector-market-create-plugin-id"
          />
          <p className="text-[11px] leading-4 text-[color:var(--color-text-placeholder)]">
            {t('connectorMarket.create.idHint')}
          </p>
          {fieldErrors.id && (
            <p
              className="mt-1 text-[11px] leading-4 text-danger"
              data-testid="connector-market-create-plugin-field-error"
              data-variant="id"
            >
              {t('connectorMarket.create.fieldRequired')}
            </p>
          )}
        </div>

        <div className="mb-4">
          <label className="mb-1.5 block text-[13px] font-medium text-text">
            {t('connectorMarket.create.description')}
          </label>
          <Textarea
            value={description}
            maxLength={DESCRIPTION_MAX}
            onChange={(nextDescription) => {
              setDescription(nextDescription);
              clearFieldError('description');
            }}
            rows={3}
            invalid={fieldErrors.description}
            data-testid="connector-market-create-plugin-description"
            showCounter
            counterTestId="connector-market-create-plugin-description-counter"
          />
          {fieldErrors.description && (
            <p
              className="mt-1 text-[11px] leading-4 text-danger"
              data-testid="connector-market-create-plugin-field-error"
              data-variant="description"
            >
              {t('connectorMarket.create.fieldRequired')}
            </p>
          )}
        </div>
      </Section>

      <Section
        title={t('connectorMarket.create.skillsOptional')}
        action={
          <button
            type="button"
            onClick={() => {
              setPicker('skill');
              loadSkills();
            }}
            className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
            data-testid="connector-market-create-plugin-add-skill"
          >
            <Plus size={14} />
            {t('connectorMarket.create.addSkill')}
          </button>
        }
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {selectedSkills.map((skill) => {
            const label = skill.display_name || skill.name;
            return (
              <PageCard
                key={skill.name}
                testId="connector-market-create-plugin-skill-item"
                variant={skill.name}
                avatar={{ name: label }}
                title={label}
                description={skill.description}
                actionsHover
                action={{
                  icon: <Trash2 size={15} />,
                  onClick: () => setSkillIds((prev) => prev.filter((id) => id !== skill.name)),
                }}
              />
            );
          })}
        </div>
      </Section>

      <Section
        title={t('connectorMarket.create.mcpOptional')}
        action={
          <button
            type="button"
            onClick={() => {
              setPicker('mcp');
              loadConnectorList('local');
            }}
            className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
            data-testid="connector-market-create-plugin-add-mcp"
          >
            <Plus size={14} />
            {t('connectorMarket.create.addMcp')}
          </button>
        }
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {selectedMcps.map((mcp) => (
            <PageCard
              key={mcp.name}
              testId="connector-market-create-plugin-mcp-item"
              variant={mcp.name}
              avatar={{ name: mcp.displayName }}
              title={mcp.displayName}
              description={mcp.description}
              actionsHover
              action={{
                icon: <Trash2 size={15} />,
                onClick: () => setMcpIds((prev) => prev.filter((id) => id !== mcp.name)),
              }}
            />
          ))}
        </div>
      </Section>

      {submitError && (
        <p className="mb-3 text-[12px] text-danger" data-testid="connector-market-create-plugin-submit-error">
          {submitError}
        </p>
      )}
    </FormPageLayout>

    {picker === 'skill' && (
      <PickerModal
        title={t('connectorMarket.create.pickSkillTitle')}
        initialSelectedIds={skillIds}
        items={toSkillPickerItems(myPickerSkills)}
        loading={skillsLoading}
        onCancel={() => setPicker(null)}
        onConfirm={(ids) => {
          setSkillIds(ids);
          setPicker(null);
        }}
      />
    )}

    {picker === 'mcp' && (
      <PickerModal
        title={t('connectorMarket.create.pickMcpTitle')}
        initialSelectedIds={mcpIds}
        items={myConnectors.map(toMcpPickerItem)}
        loading={connectorLoading}
        onCancel={() => setPicker(null)}
        onConfirm={(ids) => {
          setMcpIds(ids);
          setPicker(null);
        }}
      />
    )}
    </>
  );
}

function Section({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="mb-6">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[14px] font-semibold leading-[22px] text-text">{title}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}
