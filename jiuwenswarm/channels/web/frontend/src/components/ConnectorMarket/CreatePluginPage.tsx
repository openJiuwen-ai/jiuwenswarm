import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ImagePlus, Trash2, Plus } from 'lucide-react';
import { webRequest } from '../../services/webClient';
import { useConnectorStore } from '../../stores/connectorStore';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { computeMySkills, buildInstalledSkillNames, filterEnabledMySkills } from '../../utils/mySkills';
import { EntityAvatar } from './EntityAvatar';
import { PickerModal, type PickerItem } from './PickerModal';

const DESCRIPTION_MAX = 226;

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
// 选择弹窗内的图标沿用 getSkillAvatar（跟技能面板同一套实心圆+白字母头像，2026-08-19 整合，
// 见 utils/skillAvatar.ts 头部注释），但用户要求整体放大——从原来的 h-6 w-6 提到 h-8 w-8。
function toSkillPickerItems(items: SkillItem[]): PickerItem[] {
  return items.map((skill): PickerItem => {
    const label = skill.display_name || skill.name;
    const avatar = getSkillAvatar(label);
    return {
      id: skill.name,
      name: label,
      description: skill.description,
      render: () => (
        <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[14px] font-semibold text-text-inverse ${avatar.color}`}>
          {avatar.firstChar}
        </span>
      ),
    };
  });
}

function toMcpPickerItem(mcp: { name: string; displayName: string; description: string; icon?: string | null }): PickerItem {
  const avatar = getSkillAvatar(mcp.displayName);
  return {
    id: mcp.name,
    name: mcp.displayName,
    // mcp.list 恒下发 description（需求14已解决，见 types/connector.ts），之前这里漏接、硬编码成
    // 空字符串，导致 MCP 选择卡片一直没有描述文字。
    description: mcp.description,
    // 2026-08-19：MCP 有真实图标时（connector.icon）要优先展示，之前这里没接 iconUrl，恒渲染成
    // 生成的字母头像——跟广场卡片/详情页（都走 EntityAvatar + iconUrl）不一致。改用 EntityAvatar
    // 复用同一套"有真图标优先、没有/加载失败才回退字母色块"的逻辑。
    render: () => (
      <EntityAvatar
        iconUrl={mcp.icon ?? undefined}
        avatar={avatar}
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[13px] font-semibold"
      />
    ),
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
    [skills, installedSkillNames]
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
    <div className="relative h-full overflow-y-auto bg-card px-8 py-6" data-testid="connector-market-create-plugin-page">
      {/* 返回样式跟详情页（McpDetailPage.tsx/PluginDetailPage.tsx）保持一致：ChevronLeft
          纯尖角图标 + 黑色文字，用户明确要求这个页面也照这个样式改。 */}
      <button type="button" onClick={onBack} className="mb-4 flex items-center gap-1 text-[14px] leading-[22px] text-text hover:opacity-70" data-testid="connector-market-create-plugin-back">
        <ChevronLeft size={16} />
        {t('connectorMarket.common.back')}
      </button>

      <h1 className="mb-6 text-[18px] font-semibold leading-7 text-text" data-testid="connector-market-create-plugin-title">{t('connectorMarket.create.manual')}</h1>

      <Section title={t('connectorMarket.create.basicInfo')}>
        {AVATAR_UPLOAD_ENABLED && (
          <div className="mb-4 flex items-center gap-3">
            <label className="flex h-14 w-14 shrink-0 cursor-pointer items-center justify-center overflow-hidden rounded-2xl bg-bg-muted text-text-muted hover:bg-bg" data-testid="connector-market-create-plugin-avatar">
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
          <label className="mb-1.5 block text-[13px] font-medium text-text">
            {t('connectorMarket.create.name')}
            <span className="text-danger"> *</span>
          </label>
          <input
            value={name}
            onChange={(event) => {
              const nextName = event.target.value;
              setName(nextName);
              clearFieldError('name');
              if (!idTouched) {
                const nextId = slugify(nextName);
                setId(nextId);
                if (nextId) clearFieldError('id');
              }
            }}
            className={`h-9 w-full rounded-lg border bg-card px-3 text-[13px] text-text outline-none focus:border-border-hover ${
              fieldErrors.name ? 'border-danger' : 'border-border'
            }`}
            data-testid="connector-market-create-plugin-name"
          />
          {fieldErrors.name && (
            <p className="mt-1 text-[11px] leading-4 text-danger" data-testid="connector-market-create-plugin-field-error" data-variant="name">{t('connectorMarket.create.fieldRequired')}</p>
          )}
        </div>

        <div className="mb-4">
          <label className="mb-1.5 block text-[13px] font-medium text-text">
            {t('connectorMarket.create.id')}
            <span className="text-danger"> *</span>
          </label>
          <input
            value={id}
            onChange={(event) => {
              setIdTouched(true);
              setId(event.target.value);
              clearFieldError('id');
            }}
            placeholder={t('connectorMarket.create.idPlaceholder')}
            className={`mb-1.5 h-9 w-full rounded-lg border bg-card px-3 text-[13px] text-text outline-none focus:border-border-hover ${
              fieldErrors.id ? 'border-danger' : 'border-border'
            }`}
            data-testid="connector-market-create-plugin-id"
          />
          <p className="text-[11px] leading-4 text-[color:var(--color-text-placeholder)]">{t('connectorMarket.create.idHint')}</p>
          {fieldErrors.id && (
            <p className="mt-1 text-[11px] leading-4 text-danger" data-testid="connector-market-create-plugin-field-error" data-variant="id">{t('connectorMarket.create.fieldRequired')}</p>
          )}
        </div>

        <div className="mb-4">
          <label className="mb-1.5 block text-[13px] font-medium text-text">
            {t('connectorMarket.create.description')}
            <span className="text-danger"> *</span>
          </label>
          <div className="relative">
            <textarea
              value={description}
              maxLength={DESCRIPTION_MAX}
              onChange={(event) => {
                setDescription(event.target.value);
                clearFieldError('description');
              }}
              rows={3}
              className={`w-full resize-none rounded-lg border bg-card px-3 py-2 text-[13px] leading-5 text-text outline-none focus:border-border-hover ${
                fieldErrors.description ? 'border-danger' : 'border-border'
              }`}
              data-testid="connector-market-create-plugin-description"
            />
            <span className="absolute bottom-2 right-3 text-[11px] text-text-muted">
              {description.length}/{DESCRIPTION_MAX}
            </span>
          </div>
          {fieldErrors.description && (
            <p className="mt-1 text-[11px] leading-4 text-danger" data-testid="connector-market-create-plugin-field-error" data-variant="description">{t('connectorMarket.create.fieldRequired')}</p>
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
            const avatar = getSkillAvatar(label);
            return (
              <div key={skill.name} className="relative rounded-xl border border-border bg-card p-4" data-testid="connector-market-create-plugin-skill-item" data-variant={skill.name}>
                <button type="button" onClick={() => setSkillIds((prev) => prev.filter((id) => id !== skill.name))} className="absolute right-4 top-4 text-text-muted hover:text-danger" data-testid="connector-market-create-plugin-skill-remove" data-variant={skill.name}>
                  <Trash2 size={15} />
                </button>
                <div className="mb-1.5 flex items-center gap-2.5 pr-6">
                  <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] text-[16px] font-black text-text-inverse ${avatar.color}`}>
                    {avatar.firstChar}
                  </span>
                  <span className="text-[14px] font-semibold leading-[22px] text-text">{label}</span>
                </div>
                <p className="line-clamp-2 min-h-[40px] text-[13px] leading-5 text-[color:var(--color-text-placeholder)]">{skill.description}</p>
              </div>
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
          {selectedMcps.map((mcp) => {
            const avatar = getSkillAvatar(mcp.displayName);
            return (
              <div key={mcp.name} className="relative rounded-xl border border-border bg-card p-4" data-testid="connector-market-create-plugin-mcp-item" data-variant={mcp.name}>
                <button type="button" onClick={() => setMcpIds((prev) => prev.filter((id) => id !== mcp.name))} className="absolute right-4 top-4 text-text-muted hover:text-danger" data-testid="connector-market-create-plugin-mcp-remove" data-variant={mcp.name}>
                  <Trash2 size={15} />
                </button>
                <div className="mb-1.5 flex items-center gap-2.5 pr-6">
                  <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[13px] font-semibold text-text-inverse ${avatar.color}`}>
                    {avatar.firstChar}
                  </span>
                  <span className="text-[14px] font-semibold leading-[22px] text-text">{mcp.displayName}</span>
                </div>
                {/* 之前这里漏渲染了描述——上面技能卡片有 <p>，MCP 卡片当时照抄整个 div 结构时
                    少拷了这一行，导致 MCP 卡片只有 icon+名字（2026-08-21 用户反馈）。min-h-[40px]
                    跟技能卡片同一个值（text-[13px] leading-5 两行 = 20px*2），描述不管几行/有没有
                    卡片高度都固定，不会有的高有的矮。 */}
                <p className="line-clamp-2 min-h-[40px] text-[13px] leading-5 text-[color:var(--color-text-placeholder)]">{mcp.description}</p>
              </div>
            );
          })}
        </div>
      </Section>

      {submitError && <p className="mb-3 text-[12px] text-danger" data-testid="connector-market-create-plugin-submit-error">{submitError}</p>}

      <div className="flex justify-end gap-2 border-t border-border pt-4">
        <button type="button" onClick={onBack} className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text hover:border-border-hover" data-testid="connector-market-create-plugin-cancel">
          {t('connectorMarket.common.cancel')}
        </button>
        <button type="button" onClick={handleSubmit} disabled={submitting} className="rounded-lg bg-text px-4 py-1.5 text-[13px] text-text-inverse disabled:opacity-60" data-testid="connector-market-create-plugin-confirm">
          {t('connectorMarket.common.confirm')}
        </button>
      </div>

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
    </div>
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
