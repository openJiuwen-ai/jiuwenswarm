import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import {
  isSkillVisibleInSourceTab,
  isTeamSkillOption,
  sortInstalledFirst,
  type SkillOption,
} from '../../features/agentManagement';
import { PickerDrawer, PickerListRegion, PageCard, SelectedCount } from '../ui';
import type { PageCardDefaultButton, PickerListStatus } from '../ui';

type SkillSourceTab = 'market' | 'local';

interface SkillPickerDrawerProps {
  title: ReactNode;
  /** testid 基名：派生 -selected-count/-item/-install 及列表区各态 */
  testId: string;
  status: PickerListStatus;
  skills: SkillOption[];
  initialSelectedIds: string[];
  onClose: () => void;
  onConfirm: (ids: string[]) => void;
  onRetry: () => void;
  onInstallSkill?: (skill: SkillOption) => void | Promise<void>;
  installingSkillId?: string | null;
  tabsAriaLabel?: string;
  errorMessage?: string;
  emptyMessage?: string;
}

/** 技能选择抽屉（"广场 / 我的"两个页签）：搜索、页签、选中草稿、底部计数、三态与
    分页全部内聚，卡片（未安装显示安装、已安装点击勾选）也固定在此；调用点只传数据
    与回调。与 ConnectorPickerDrawer 共用 PickerDrawer + PickerListRegion 底座。 */
export function SkillPickerDrawer({
  title,
  testId,
  status,
  skills,
  initialSelectedIds,
  onClose,
  onConfirm,
  onRetry,
  onInstallSkill,
  installingSkillId,
  tabsAriaLabel,
  errorMessage,
  emptyMessage,
}: SkillPickerDrawerProps) {
  const { t } = useTranslation();
  const [sourceTab, setSourceTab] = useState<SkillSourceTab>('market');
  const [query, setQuery] = useState('');
  const [selectedIds, setSelectedIds] = useState<string[]>(initialSelectedIds);

  // 与 ConnectorPickerDrawer 的 visibleItems 对齐：用 useMemo 稳定引用，
  // 避免每次渲染都产生新数组导致 PickerListRegion 的 [items] effect 重置回首批
  // （选中/取消会触发父组件重渲染，新引用会让列表从第 2 批跳回首屏 30 条）。
  const filteredSkills = useMemo(
    () =>
      sortInstalledFirst(
        skills.filter((skill) => {
          if (isTeamSkillOption(skill, sourceTab) || !isSkillVisibleInSourceTab(skill, sourceTab)) return false;
          const q = query.trim().toLocaleLowerCase();
          if (!q) return true;
          return `${skill.id} ${skill.name} ${skill.description}`.toLocaleLowerCase().includes(q);
        }),
      ),
    [skills, sourceTab, query],
  );

  function toggle(id: string) {
    setSelectedIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  return (
    <PickerDrawer
      title={title}
      onClose={onClose}
      onConfirm={() => onConfirm(selectedIds)}
      testId={testId}
      footerLeading={<SelectedCount count={selectedIds.length} testId={`${testId}-selected-count`} />}
      search={query}
      onSearchChange={setQuery}
      searchPlaceholder={t('agentManagement.form.selectionSearchPlaceholder')}
      tabs={{
        value: sourceTab,
        onChange: setSourceTab,
        ariaLabel: tabsAriaLabel ?? t('agentManagement.form.skillSourceTabsLabel'),
        items: [
          { value: 'market', label: t('agentManagement.form.skillMarket') },
          { value: 'local', label: t('agentManagement.form.mySkills') },
        ],
      }}
    >
      <PickerListRegion
        status={status}
        items={filteredSkills}
        getItemKey={(skill) => skill.id}
        testId={testId}
        loadingMessage={t('common.loading')}
        errorMessage={errorMessage ?? t('agentManagement.form.skillsError')}
        emptyMessage={emptyMessage ?? t('agentManagement.form.skillsEmpty')}
        retryLabel={t('common.retry')}
        onRetry={onRetry}
        loadMoreLabel={t('agentManagement.loadMore')}
        renderItem={(skill) => {
          const selected = selectedIds.includes(skill.id);
          const installed = skill.installed === true;
          const installing = installingSkillId === skill.id;
          const defaultButton: PageCardDefaultButton | undefined =
            !installed && onInstallSkill
              ? {
                  text: installing ? t('agentManagement.form.installingSkill') : t('agentManagement.form.installSkill'),
                  testId: `${testId}-install`,
                  variant: skill.id,
                  disabled: installing,
                  busy: installing,
                  onClick: () => void onInstallSkill?.(skill),
                }
              : undefined;
          return (
            <PageCard
              testId={`${testId}-item`}
              variant={skill.id}
              interactive={installed}
              selected={selected}
              disabled={!installed && !onInstallSkill}
              onClick={installed ? () => toggle(skill.id) : undefined}
              avatar={{ name: skill.name }}
              title={skill.name}
              description={skill.description || t('agentManagement.unknownDescription')}
              defaultButton={defaultButton}
              actionSlot={
                !installed && onInstallSkill ? null : (
                  <span className="shrink-0" aria-hidden="true">
                    {selected ? (
                      <EntityRemoveIcon className="text-[color:var(--color-chat-accent)]" />
                    ) : (
                      <EntityAddIcon className="text-text-muted" />
                    )}
                  </span>
                )
              }
            />
          );
        }}
      />
    </PickerDrawer>
  );
}
