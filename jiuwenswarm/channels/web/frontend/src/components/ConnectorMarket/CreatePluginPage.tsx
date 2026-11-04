import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ImagePlus, Plus } from 'lucide-react';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { createAgentManagementClient, type McpOption, type SkillOption } from '../../features/agentManagement';
import { CONNECTOR_ERROR_TOAST_ID } from '../../stores/connectorStore';
import { Input } from '../ui/Input/Input';
import { Textarea } from '../ui/Textarea/Textarea';
import { PageCard, toast, type PickerListStatus } from '../ui';
import { DeleteCardIcon } from '../AgentManagementPanel/cardActions';
import { SkillPickerDrawer } from '../AgentManagementPanel/SkillPickerDrawer';
import { McpPickerDrawer } from './McpPickerDrawer';
import { FormPageLayout } from './FormPageLayout';

const DESCRIPTION_MAX = 512;

// 头像上传入口暂时隐藏：后端 plugin_packages.create 没有头像/图标字段
// （backend-requests.md 需求9），选中的图片只能本地预览、保存不了。等后端支持后把这个
// 常量翻成 true 即可恢复整段 UI（相关 state / handleAvatarSelect / revoke effect 都保留着）。
const AVATAR_UPLOAD_ENABLED = false;

type RequiredFieldKey = 'id' | 'name' | 'description';

interface CreatePluginPageProps {
  onBack: () => void;
  onCreated: () => void;
}

// 对应高保真 3.1 手动创建插件。提交调 pluginPackageStore.create——2026-08-07 对齐专家与
// 插件装备-前端接口(3).md §3.3 真实参数形状 {id, name, description, skills}：id 是必填目录名，
// 手动输入或按名称自动建议一个 slug，用户可编辑；name/description 是纯字符串，不再是双语对象。
// 2026-08-21：后端 create_plugin_package 补上了 mcps 参数（extension_package_manager.py
// _require_mcp_names，connector 名称数组，可选），mcpIds 选择现在会真的带进 create() 提交
// ——之前这里没有承载位，选了也是纯本地展示，backend-requests.md 需求 2 已解决。
// 头像选择：点击可以真的打开文件选择器并本地预览（上一轮这里只是个纯静态图标，点了没反应），
// 但 plugin_packages.* 完全没有图标字段（backend-requests.md 需求9），选中的图片选不进
// create() 的参数里，只能停留在本地预览——选好图片后额外提示一句，不让用户误以为真的保存了。
// "选择技能"/"选择MCP"抽屉（2026-10-09 起）与专家编辑器共用同一对业务组件
// SkillPickerDrawer / McpPickerDrawer（agent-editor-skill-picker / agent-editor-mcp-picker
// 同款渲染：广场/我的页签、安装/连接按钮、已装优先排序），数据也走同一个
// agentManagement client（listSkillOptions / installSkill / listMcpOptions），
// ids 即技能名 / 连接器 runtime 名，直接进 create() 提交。
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
  const [skillOptions, setSkillOptions] = useState<SkillOption[]>([]);
  const [skillsStatus, setSkillsStatus] = useState<PickerListStatus>('idle');
  const [installingSkillId, setInstallingSkillId] = useState<string | null>(null);
  const [mcpOptions, setMcpOptions] = useState<McpOption[]>([]);
  const [mcpStatus, setMcpStatus] = useState<PickerListStatus>('idle');
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

  const client = useMemo(() => createAgentManagementClient(), []);
  const createPlugin = usePluginPackageStore((s) => s.create);

  // 弹窗每次打开都重拉数据（2026-08-19 用户要求：选择弹窗要真的向后端拉数据，不能只在
  // 页面挂载时统一拉一次）。onRetry 需稳定引用，内联箭头会让错误态的"重试"每次渲染都换新。
  const loadSkills = useCallback(() => {
    setSkillsStatus('loading');
    client
      .listSkillOptions()
      .then((options) => {
        // 本弹窗默认只列"已启用"技能：沿用 2026-08-25 与技能管理页"我的技能" tab 同一口径
        // （utils/mySkills.ts filterEnabledMySkills 的 enabled !== false），收敛到共用的
        // SkillPickerDrawer 后在数据侧保留这道过滤——已停用的技能不能再次被勾进插件。
        setSkillOptions(options.filter((skill) => skill.enabled !== false));
        setSkillsStatus('success');
      })
      .catch(() => setSkillsStatus('error'));
  }, [client]);

  const loadMcps = useCallback(() => {
    setMcpStatus('loading');
    client
      .listMcpOptions()
      .then((options) => {
        setMcpOptions(options);
        setMcpStatus('success');
      })
      .catch(() => setMcpStatus('error'));
  }, [client]);

  const handleInstallSkill = useCallback(
    async (skill: SkillOption) => {
      setInstallingSkillId(skill.id);
      try {
        await client.installSkill(skill);
        loadSkills();
      } catch (error) {
        const message = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
        toast.open({
          // 与页壳/MCP 抽屉的 action error 共用同一 id（CONNECTOR_ERROR_TOAST_ID）：同一次
          // 操作链上的失败原地替换上一条红 Toast，不叠两条。
          id: CONNECTOR_ERROR_TOAST_ID,
          content: message || t('connectorMarket.picker.actionError'),
          variant: 'error',
        });
      } finally {
        setInstallingSkillId(null);
      }
    },
    [client, loadSkills, t],
  );

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

  const selectedSkills = skillOptions.filter((skill) => skillIds.includes(skill.id));
  const selectedMcps = mcpOptions.filter((mcp) => mcpIds.includes(mcp.id));

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
              className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
              data-testid="connector-market-create-plugin-add-skill"
            >
              <Plus size={14} />
              {t('connectorMarket.create.addSkill')}
            </button>
          }
        >
          <div className="card-grid-auto">
            {selectedSkills.map((skill) => (
              <PageCard
                key={skill.id}
                testId="connector-market-create-plugin-skill-item"
                variant={skill.id}
                avatar={{ name: skill.name }}
                title={skill.name}
                description={skill.description}
                actionsHover
                action={{
                  icon: <DeleteCardIcon />,
                  onClick: () => setSkillIds((prev) => prev.filter((id) => id !== skill.id)),
                }}
              />
            ))}
          </div>
        </Section>

        <Section
          title={t('connectorMarket.create.mcpOptional')}
          action={
            <button
              type="button"
              onClick={() => {
                setPicker('mcp');
                loadMcps();
              }}
              className="flex items-center gap-1 rounded-full px-2.5 py-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)]"
              data-testid="connector-market-create-plugin-add-mcp"
            >
              <Plus size={14} />
              {t('connectorMarket.create.addMcp')}
            </button>
          }
        >
          <div className="card-grid-auto">
            {selectedMcps.map((mcp) => (
              <PageCard
                key={mcp.id}
                testId="connector-market-create-plugin-mcp-item"
                variant={mcp.id}
                avatar={{ name: mcp.name, iconUrl: mcp.icon || undefined }}
                title={mcp.name}
                description={mcp.description}
                actionsHover
                action={{
                  icon: <DeleteCardIcon />,
                  onClick: () => setMcpIds((prev) => prev.filter((id) => id !== mcp.id)),
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
        <SkillPickerDrawer
          title={t('connectorMarket.create.pickSkillTitle')}
          testId="connector-market-skill-picker"
          status={skillsStatus}
          skills={skillOptions}
          initialSelectedIds={skillIds}
          onClose={() => setPicker(null)}
          onConfirm={(ids) => {
            setSkillIds(ids);
            setPicker(null);
          }}
          onRetry={loadSkills}
          onInstallSkill={handleInstallSkill}
          installingSkillId={installingSkillId}
        />
      )}

      {picker === 'mcp' && (
        <McpPickerDrawer
          title={t('connectorMarket.create.pickMcpTitle')}
          testId="connector-market-mcp-picker"
          status={mcpStatus}
          items={mcpOptions}
          initialSelectedIds={mcpIds}
          onClose={() => setPicker(null)}
          onConfirm={(ids) => {
            setMcpIds(ids);
            setPicker(null);
          }}
          onRetry={loadMcps}
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
