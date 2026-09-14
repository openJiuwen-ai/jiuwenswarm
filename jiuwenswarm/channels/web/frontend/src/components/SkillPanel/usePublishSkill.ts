/**
 * 技能发布抽屉：表单状态 + 校验 + 提交 hook
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { getStoredOAuthProvider, getStoredOAuthToken } from '../../utils/gitcodeOAuth';
import { DISPLAY_NAME_MAX_LEN, SKILL_NAME_MAX_LEN, SKILL_NAME_PATTERN, VERSION_PATTERN } from './skillPanelUtils';
import type { WithSessionFn } from './useHubMarketplace';
import type { SkillToastShower } from './useSkillToasts';
import type { SkillDetail } from './types';

interface UsePublishSkillParams {
  selectedSkill: SkillDetail | null;
  withSession: WithSessionFn;
  fetchSkills: (refreshMarketplaces?: boolean) => Promise<void>;
  showMessage: SkillToastShower;
  setActionTarget: (target: string | null) => void;
  setOauthLoginOpen: (open: boolean) => void;
}

export function usePublishSkill({
  selectedSkill,
  withSession,
  fetchSkills,
  showMessage,
  setActionTarget,
  setOauthLoginOpen,
}: UsePublishSkillParams) {
  const { t } = useTranslation();
  const [publishDrawerOpen, setPublishDrawerOpen] = useState(false);
  const [publishSkillName, setPublishSkillName] = useState('');
  const [publishVersion, setPublishVersion] = useState('');
  const [publishDisplayName, setPublishDisplayName] = useState('');
  const [publishNoticeVisible, setPublishNoticeVisible] = useState(true);
  const [publishVersionDesc, setPublishVersionDesc] = useState('');
  const [publishForce, setPublishForce] = useState(false);
  const [publishFieldErrors, setPublishFieldErrors] = useState<Record<string, string>>({});
  const [publishError, setPublishError] = useState<string | null>(null);

  // ── 发布表单校验（与 skillhub 对齐） ──
  const validatePublishSkillName = useCallback(
    (name: string): string | null => {
      const trimmed = name.trim();
      if (!trimmed) return null; // 空值由必填校验兜底
      if (trimmed.length > SKILL_NAME_MAX_LEN) {
        return t('skills.publishForm.errorNameTooLong', { max: SKILL_NAME_MAX_LEN });
      }
      if (!SKILL_NAME_PATTERN.test(trimmed)) {
        return t('skills.publishForm.errorInvalidName');
      }
      return null;
    },
    [t],
  );

  const validatePublishVersion = useCallback(
    (version: string): string | null => {
      const trimmed = version.trim();
      if (!trimmed) return null;
      if (!VERSION_PATTERN.test(trimmed)) {
        return t('skills.publishForm.errorInvalidVersion');
      }
      return null;
    },
    [t],
  );

  const validatePublishDisplayName = useCallback(
    (displayName: string): string | null => {
      const trimmed = displayName.trim();
      if (!trimmed) return null;
      if (trimmed.length > DISPLAY_NAME_MAX_LEN) {
        return t('skills.publishForm.errorDisplayNameTooLong', { max: DISPLAY_NAME_MAX_LEN });
      }
      return null;
    },
    [t],
  );

  const handlePublish = useCallback(async () => {
    if (!selectedSkill) return;
    const token = getStoredOAuthToken();
    if (!token) {
      setOauthLoginOpen(true);
      return;
    }
    // 提交前校验（与 skillhub 对齐）
    const errors: Record<string, string> = {};
    const nameErr = validatePublishSkillName(publishSkillName);
    if (nameErr) errors.skillName = nameErr;
    const verErr = validatePublishVersion(publishVersion);
    if (verErr) errors.version = verErr;
    const dnErr = validatePublishDisplayName(publishDisplayName);
    if (dnErr) errors.displayName = dnErr;
    if (Object.keys(errors).length > 0) {
      setPublishFieldErrors(errors);
      return;
    }
    setPublishFieldErrors({});
    setPublishError(null);
    setActionTarget('publish');
    try {
      // 1. 从 file_path 推导技能目录
      const filePath = (selectedSkill as SkillDetail).file_path || '';
      const skillDir = filePath ? filePath.replace(/[\\/][^\\/]+$/, '') : '';
      if (!skillDir) {
        setPublishError(t('skills.publishForm.pathNotFound'));
        return;
      }

      // 2. WS 打包 zip（后端打包，前端下载）
      const packResult = await webRequest<{
        success: boolean;
        path?: string;
        checksum_sha256?: string;
        detail?: string;
      }>(
        'skills.teamskillshub.pack',
        withSession({
          path: skillDir,
          output: 'out',
          version: publishVersion,
          skill_name: publishSkillName,
          display_name: publishDisplayName,
        }),
        { timeoutMs: 60000 },
      );
      if (!packResult.success || !packResult.path) {
        setPublishError(packResult.detail || t('skills.messages.packFailed'));
        return;
      }

      // 3. 下载 zip 为 Blob
      const zipResp = await fetch(`/file-api/raw-file?path=${encodeURIComponent(packResult.path)}`);
      if (!zipResp.ok) {
        setPublishError(t('skills.messages.downloadFailed'));
        return;
      }
      const zipBlob = await zipResp.blob();

      // 4. 使用后端返回的 SHA256（避免 crypto.subtle 在非安全上下文不可用）
      const checksum = packResult.checksum_sha256 || '';

      // 5. 组装 FormData（前端组装，与 skillhub 对齐）
      const formData = new FormData();
      formData.append('file', zipBlob, `${selectedSkill.name}.zip`);
      formData.append('plugin_version', publishVersion);
      if (publishVersionDesc) formData.append('version_desc', publishVersionDesc);
      if (publishForce) formData.append('force', 'true');

      // 6. POST 到 Hub（通过 Vite /hub-api/ 代理）
      const provider = getStoredOAuthProvider();
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 120000);
      const resp = await fetch('/hub-api/api/v1/plugins', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'X-OAuth-Provider': provider,
          'X-Checksum-SHA256': checksum,
        },
        body: formData,
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      const respData = await resp.json();

      if (respData.code === 200 && respData.data?.plugin_id) {
        showMessage(
          'success',
          t('skills.messages.published', {
            name: publishDisplayName || respData.data?.display_name || respData.data?.name || selectedSkill.name,
          }),
        );
        setPublishDrawerOpen(false);
        setPublishVersion('');
        setPublishVersionDesc('');
        setPublishFieldErrors({});
        setPublishError(null);
        await fetchSkills();
      } else {
        // 检查版本冲突
        const detail = respData.detail || {};
        const errorMsg = detail.message || detail.error || respData.message || '';
        setPublishError(errorMsg || t('skills.messages.publishFailed'));
      }
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : '';
      setPublishError(errMsg || t('skills.messages.publishFailed'));
    } finally {
      setActionTarget(null);
    }
  }, [
    selectedSkill,
    publishVersion,
    publishVersionDesc,
    publishSkillName,
    publishDisplayName,
    publishForce,
    withSession,
    fetchSkills,
    t,
    validatePublishSkillName,
    validatePublishVersion,
    validatePublishDisplayName,
    showMessage,
    setOauthLoginOpen,
    setActionTarget,
  ]);

  return {
    publishDrawerOpen,
    setPublishDrawerOpen,
    publishSkillName,
    setPublishSkillName,
    publishVersion,
    setPublishVersion,
    publishDisplayName,
    setPublishDisplayName,
    publishNoticeVisible,
    setPublishNoticeVisible,
    publishVersionDesc,
    setPublishVersionDesc,
    publishForce,
    setPublishForce,
    publishFieldErrors,
    setPublishFieldErrors,
    publishError,
    validatePublishSkillName,
    validatePublishVersion,
    validatePublishDisplayName,
    handlePublish,
  };
}

export type PublishSkillState = ReturnType<typeof usePublishSkill>;
