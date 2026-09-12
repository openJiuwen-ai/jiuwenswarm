/**
 * UserMgmtPanel — 用户管理面板。
 *
 * 数据来源：web RPC `users.list` / `users.create`（后端读写
 * ~/.jiuwenswarm/users.json，与三方 Agent 安装共用同一存储）。
 * 密码与 API Key 由后端脱敏返回，前端只展示掩码，接触不到明文。
 * 点击「创建用户」弹出对话框，填写用户名、密码、模型 API Key、Base URL、模型名。
 */

import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Trash2 } from 'lucide-react';
import { webRequest } from '../../services/webClient';

interface UserInfo {
  username: string;
  passwordMasked: string;
  apiKeyMasked: string;
  apiBase: string;
  model: string;
  createdAt: string | null;
}

interface UsersListResponse {
  users?: unknown[];
}

interface UserCreateResponse {
  success?: boolean;
  detail?: string;
}

interface UserDeleteResponse {
  success?: boolean;
  detail?: string;
}

interface CreateUserForm {
  username: string;
  password: string;
  apiKey: string;
  apiBase: string;
  model: string;
}

const EMPTY_FORM: CreateUserForm = {
  username: '',
  password: '',
  apiKey: '',
  apiBase: '',
  model: '',
};

function toStr(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function toUserInfos(raw: unknown[]): UserInfo[] {
  const rows: UserInfo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    if (typeof rec.username !== 'string' || !rec.username) continue;
    rows.push({
      username: rec.username,
      passwordMasked: toStr(rec.password_masked),
      apiKeyMasked: toStr(rec.api_key_masked),
      apiBase: toStr(rec.api_base),
      model: toStr(rec.model),
      createdAt: typeof rec.created_at === 'string' && rec.created_at ? rec.created_at : null,
    });
  }
  return rows.sort((a, b) => a.username.localeCompare(b.username));
}

function formatTime(iso: string | null): string {
  if (!iso) return '-';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString();
}

export function UserMgmtPanel() {
  const { t } = useTranslation();
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [form, setForm] = useState<CreateUserForm>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  const load = useCallback(async () => {
    try {
      const resp = await webRequest<UsersListResponse>('users.list', {});
      setUsers(toUserInfos(resp?.users ?? []));
      setLoadError('');
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
      setUsers([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openDialog = () => {
    setForm(EMPTY_FORM);
    setSubmitError('');
    setDialogOpen(true);
  };

  const updateField = (field: keyof CreateUserForm) =>
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setForm((prev) => ({ ...prev, [field]: e.target.value }));
    };

  const handleSubmit = async () => {
    if (!form.username.trim() || !form.password) {
      setSubmitError(t('usermgmt.requiredHint'));
      return;
    }
    setSubmitting(true);
    setSubmitError('');
    try {
      const resp = await webRequest<UserCreateResponse>('users.create', {
        username: form.username.trim(),
        password: form.password,
        api_key: form.apiKey,
        api_base: form.apiBase.trim(),
        model: form.model.trim(),
      });
      if (resp?.success === false) {
        setSubmitError(resp.detail || t('usermgmt.createFailed'));
        return;
      }
      setDialogOpen(false);
      await load();
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const openDeleteConfirm = (username: string) => {
    setDeleteTarget(username);
    setDeleteError('');
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError('');
    try {
      const resp = await webRequest<UserDeleteResponse>('users.delete', {
        username: deleteTarget,
      });
      if (resp?.success === false) {
        setDeleteError(resp.detail || t('usermgmt.deleteFailed'));
        return;
      }
      setDeleteTarget(null);
      await load();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  const inputClass =
    'w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text outline-none focus:border-accent';
  const labelClass = 'mb-1 block text-xs text-text-muted';

  return (
    <div className="flex-1 min-h-0 relative overflow-y-auto" data-testid="usermgmt-panel">
      <div className="mx-auto max-w-5xl px-6 py-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text">{t('usermgmt.title')}</h2>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => void load()}
              className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text hover:bg-bg-hover"
            >
              {t('usermgmt.refresh')}
            </button>
            <button
              type="button"
              onClick={openDialog}
              className="rounded-md border border-border bg-accent px-3 py-1.5 text-sm text-accent-foreground hover:opacity-90"
              data-testid="usermgmt-create-btn"
            >
              {t('usermgmt.create')}
            </button>
          </div>
        </div>

        {loading && users.length === 0 && !loadError && (
          <div className="mt-10 text-center text-sm text-text-muted">{t('usermgmt.loading')}</div>
        )}

        {loadError && (
          <div className="mt-10 rounded-lg border border-dashed border-border px-6 py-10 text-center">
            <div className="text-sm text-text-muted">{t('usermgmt.loadFailed')}</div>
            <div className="mt-2 text-xs text-text-muted">{loadError}</div>
          </div>
        )}

        {!loadError && !loading && users.length === 0 && (
          <div className="mt-10 rounded-lg border border-dashed border-border px-6 py-10 text-center">
            <div className="text-sm text-text-muted">{t('usermgmt.empty')}</div>
            <div className="mt-2 text-xs text-text-muted">{t('usermgmt.emptyHint')}</div>
          </div>
        )}

        {users.length > 0 && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {users.map((user) => (
              <div
                key={user.username}
                className="rounded-xl border border-border bg-card p-4"
                data-testid={`user-card-${user.username}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-base font-medium text-text break-all">{user.username}</span>
                  <button
                    type="button"
                    onClick={() => openDeleteConfirm(user.username)}
                    className="shrink-0 rounded-md p-1.5 text-text-muted hover:bg-bg-hover hover:text-red-500"
                    title={t('usermgmt.delete')}
                    data-testid={`user-delete-btn-${user.username}`}
                  >
                    <Trash2 size={16} />
                  </button>
                </div>

                <div className="mt-3 grid grid-cols-1 gap-2 text-sm">
                  <div>
                    <div className="text-xs text-text-muted">{t('usermgmt.model')}</div>
                    <div className="mt-0.5 text-text break-all">{user.model || '-'}</div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('usermgmt.apiBase')}</div>
                    <div className="mono mt-0.5 text-xs text-text break-all">{user.apiBase || '-'}</div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('usermgmt.apiKey')}</div>
                    <div className="mono mt-0.5 text-xs text-text-muted break-all">
                      {user.apiKeyMasked || '-'}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('usermgmt.password')}</div>
                    <div className="mono mt-0.5 text-xs text-text-muted">
                      {user.passwordMasked || '-'}
                    </div>
                  </div>
                </div>

                {user.createdAt && (
                  <div className="mt-3 text-xs text-text-muted">
                    {t('usermgmt.createdAt')}: {formatTime(user.createdAt)}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {dialogOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-cron-dialog"
          data-testid="usermgmt-create-overlay"
          onClick={() => !submitting && setDialogOpen(false)}
        >
          <div
            className="relative w-[420px] rounded-lg bg-card p-6 shadow-xl animate-scale-in"
            data-testid="usermgmt-create-dialog"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-5 flex items-center justify-between">
              <h3 className="text-lg font-bold text-text-strong">{t('usermgmt.dialogTitle')}</h3>
              <button
                onClick={() => !submitting && setDialogOpen(false)}
                className="text-text-muted hover:text-text"
                data-testid="usermgmt-create-close-btn"
              >
                <X size={20} />
              </button>
            </div>

            <div className="flex flex-col gap-3">
              <div>
                <label className={labelClass}>{t('usermgmt.username')} *</label>
                <input
                  className={inputClass}
                  value={form.username}
                  onChange={updateField('username')}
                  data-testid="usermgmt-form-username"
                />
              </div>
              <div>
                <label className={labelClass}>{t('usermgmt.password')} *</label>
                <input
                  className={inputClass}
                  type="password"
                  value={form.password}
                  onChange={updateField('password')}
                  data-testid="usermgmt-form-password"
                />
              </div>
              <div>
                <label className={labelClass}>{t('usermgmt.apiKey')}</label>
                <input
                  className={inputClass}
                  type="password"
                  value={form.apiKey}
                  onChange={updateField('apiKey')}
                  data-testid="usermgmt-form-apikey"
                />
              </div>
              <div>
                <label className={labelClass}>{t('usermgmt.apiBase')}</label>
                <input
                  className={inputClass}
                  value={form.apiBase}
                  onChange={updateField('apiBase')}
                  placeholder="https://api.example.com/v1"
                  data-testid="usermgmt-form-apibase"
                />
              </div>
              <div>
                <label className={labelClass}>{t('usermgmt.model')}</label>
                <input
                  className={inputClass}
                  value={form.model}
                  onChange={updateField('model')}
                  data-testid="usermgmt-form-model"
                />
              </div>
            </div>

            {submitError && (
              <div className="mt-3 text-xs text-red-500" data-testid="usermgmt-create-error">
                {submitError}
              </div>
            )}

            <div className="mt-6 flex justify-center gap-3">
              <button
                onClick={() => void handleSubmit()}
                disabled={submitting}
                className="rounded-full bg-accent px-10 py-1.5 text-sm font-bold text-accent-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="usermgmt-create-submit-btn"
              >
                {submitting ? t('usermgmt.creating') : t('usermgmt.confirmCreate')}
              </button>
              <button
                onClick={() => !submitting && setDialogOpen(false)}
                className="rounded-full border border-border bg-card px-10 py-1.5 text-sm font-bold text-text hover:bg-bg-hover"
                data-testid="usermgmt-create-cancel-btn"
              >
                {t('common.cancel')}
              </button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-cron-dialog"
          data-testid="usermgmt-delete-overlay"
          onClick={() => !deleting && setDeleteTarget(null)}
        >
          <div
            className="relative w-[420px] rounded-lg bg-card p-6 shadow-xl animate-scale-in"
            data-testid="usermgmt-delete-dialog"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-5 flex items-center justify-between">
              <h3 className="text-lg font-bold text-text-strong">{t('usermgmt.deleteTitle')}</h3>
              <button
                onClick={() => !deleting && setDeleteTarget(null)}
                className="text-text-muted hover:text-text"
                data-testid="usermgmt-delete-close-btn"
              >
                <X size={20} />
              </button>
            </div>
            <p className="mb-6 break-words text-sm text-text">
              {t('usermgmt.deleteConfirm', { username: deleteTarget })}
            </p>
            {deleteError && (
              <div className="mb-3 text-xs text-red-500" data-testid="usermgmt-delete-error">
                {deleteError}
              </div>
            )}
            <div className="flex justify-center gap-3">
              <button
                onClick={() => void handleDelete()}
                disabled={deleting}
                className="rounded-full bg-red-500 px-10 py-1.5 text-sm font-bold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="usermgmt-delete-confirm-btn"
              >
                {deleting ? t('usermgmt.deleting') : t('usermgmt.delete')}
              </button>
              <button
                onClick={() => !deleting && setDeleteTarget(null)}
                className="rounded-full border border-border bg-card px-10 py-1.5 text-sm font-bold text-text hover:bg-bg-hover"
                data-testid="usermgmt-delete-cancel-btn"
              >
                {t('common.cancel')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default UserMgmtPanel;
