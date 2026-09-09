import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Loader2, ExternalLink } from 'lucide-react';
import { useConnectorStore } from '../../stores/connectorStore';
import type { ConnectorConnectResponse } from '../../types/connector';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { EntityAvatar } from './EntityAvatar';
import logoIcon from '/logo.svg';

interface ConnectTokenModalProps {
  name: string;
  displayName: string;
  /** 后端下发的真实图标地址（connector.icon），跟卡片上的 EntityAvatar 同一套：有就用真图标，
   * 没有/加载失败回退成首字符色块——之前这里漏传，弹窗里恒显示色块，跟卡片上的真图标对不上。 */
  iconUrl?: string;
  // credentials_required 分支的完整响应，把 title/description/doc_url/doc_label/fields 都传进来
  // 渲染，而不是只取 requiredTokens——见文档 §5.3.2，这些字段是后端给的富文案，不是可选装饰。
  response: ConnectorConnectResponse;
  onCancel: () => void;
  onConnected: () => void;
}

// 对应高保真 2.1 MCP输入API KEY连接。
// 2026-08-10 按新接口文档 §5.3.2 补齐富文案：per-token 的 label/placeholder/type(text|password)/
// description，外加弹窗级别的 title/description/doc_url/doc_label。旧版只用 token key 当 label、
// 通用 placeholder、没有密码框、没有外部文档链接——不是接口不兼容，是没利用上后端已经给的字段。
export function ConnectTokenModal({ name, displayName, iconUrl, response, onCancel, onConnected }: ConnectTokenModalProps) {
  const { t } = useTranslation();
  const [tokens, setTokens] = useState<Record<string, string>>({});
  const [connecting, setConnecting] = useState(false);
  const saveCredentialsAndConnect = useConnectorStore((s) => s.saveCredentialsAndConnect);

  const requiredTokens = response.requiredTokens ?? [];
  const fields = response.fields ?? {};
  const allFilled = requiredTokens.length > 0 && requiredTokens.every((key) => (tokens[key] ?? '').trim());
  const avatar = getSkillAvatar(displayName);

  async function handleSubmit() {
    if (!allFilled || connecting) return;
    setConnecting(true);
    const result = await saveCredentialsAndConnect(name, tokens);
    setConnecting(false);
    if (result?.type === 'connected') {
      onConnected();
    } else {
      // 失败（含请求超时，result 为 null）或非预期的 type：这个弹窗只处理纯 token 提交，没有
      // 续接下一步的分支，留在原地只会让用户对着一个已经点不出效果的"连接"按钮干等——真实的
      // 失败原因已经由 connectorStore 写进 store.error，顶层 index.tsx 会弹红色 Toast 告知，
      // 这里直接关闭弹窗即可，不需要重复展示错误（2026-08-11 用户实测发现：提交后弹窗不关闭）。
      onCancel();
    }
  }

  // z-[10100]：这个弹窗可能从 ChatPanel/ExtensionPickerPanel.tsx 的"+"扩展面板里弹出，那个
  // 面板自身是 zIndex:9999 的 fixed 浮层，弹窗必须盖在它上面（2026-08-25 用户反馈：连接弹窗
  // 之前用 z-50，被扩展面板整个压在下面，弹窗形同虚设）。
  // data-connector-auth-modal：InputArea.tsx（"+"一级菜单）和 ExtensionPickerPanel.tsx（扩展
  // 二级面板）各自都有一份"点击外部即关闭"的 pointerdown 监听，这个弹窗是单独 portal 到
  // document.body 的兄弟节点，不在它们任何一个的 ref 范围内——两处监听都要靠这个属性识别"点的
  // 是弹窗内部"从而跳过关闭，否则点弹窗任何地方都会被误判成"点了外面"，把外层菜单和面板一起
  // 带崩（2026-08-25 用户反馈：点连接弹窗，整个"+"扩展下拉框直接退出）。
  return (
    <div data-connector-auth-modal="true" data-testid="connector-market-token-modal" className="fixed inset-0 z-[10100] flex items-center justify-center bg-overlay-cron-dialog">
      <div className="relative w-[400px] rounded-2xl bg-card p-6 shadow-xl">
        <button type="button" onClick={onCancel} className="absolute right-5 top-5 text-text-muted hover:text-text" data-testid="connector-market-token-modal-close">
          <X size={18} />
        </button>

        <div className="mb-4 flex items-center justify-center gap-8">
          <img src={logoIcon} alt="JiuwenSwarm" className="h-11 w-11 shrink-0 rounded-xl" />
          {/* 用户反馈两个图标离得太近——把 flex gap 拉大（4→8），并把原来的 lucide ArrowRight
              实心箭头换成"虚线+实心箭头"的自绘样式（虚线用 border-dashed，箭头用零尺寸 div 的
              边框三角形技巧），比单纯拉大 gap 更明确地表达"两者之间有一段连接过程"的观感。 */}
          <div className="flex w-10 shrink-0 items-center" aria-hidden="true">
            <div className="h-0 flex-1 border-t-2 border-dashed border-text" />
            <div className="h-0 w-0 border-y-[4px] border-y-transparent border-l-[6px] border-l-text" />
          </div>
          <EntityAvatar
            iconUrl={iconUrl}
            avatar={avatar}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[16px] font-semibold"
          />
        </div>

        <h2 className="mb-1 text-center text-[16px] font-semibold text-text">
          {response.title ?? t('connectorMarket.tokenModal.title', { name: displayName })}
        </h2>
        {response.description && (
          <p className="mb-4 text-center text-[12px] leading-[18px] text-text-muted">{response.description}</p>
        )}
        {!response.description && <div className="mb-4" />}

        {requiredTokens.map((key) => {
          const field = fields[key];
          return (
            <div key={key} className="mb-4">
              <label className="mb-1.5 block text-[13px] font-medium text-text">{field?.label ?? key}</label>
              <input
                type={field?.type === 'password' ? 'password' : 'text'}
                name={`connector-token-${key}`}
                autoComplete={field?.type === 'password' ? 'new-password' : 'off'}
                value={tokens[key] ?? ''}
                onChange={(event) => setTokens((prev) => ({ ...prev, [key]: event.target.value }))}
                placeholder={field?.placeholder ?? t('connectorMarket.tokenModal.placeholder', { name: displayName })}
                className="h-9 w-full rounded-lg border border-border bg-bg px-3 text-[13px] text-text outline-none placeholder:text-[color:var(--color-text-placeholder)] focus:border-border-hover"
                data-testid="connector-market-token-modal-field"
                data-variant={key}
              />
              {field?.description && <p className="mt-1 text-[12px] leading-[18px] text-text-muted">{field.description}</p>}
            </div>
          );
        })}

        {response.docUrl ? (
          <a
            href={response.docUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mb-4 flex items-center gap-1 text-[12px] text-[color:var(--color-chat-accent)] hover:underline"
            data-testid="connector-market-token-modal-doc-link"
          >
            <ExternalLink size={12} />
            {response.docLabel ?? response.docUrl}
          </a>
        ) : (
          response.docLabel && <p className="mb-4 text-[12px] leading-[18px] text-text-muted">{response.docLabel}</p>
        )}

        <button
          type="button"
          disabled={!allFilled || connecting}
          onClick={handleSubmit}
          className="flex h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-text text-[13px] text-text-inverse disabled:opacity-40"
          data-testid="connector-market-token-modal-submit"
        >
          {connecting && <Loader2 size={14} className="animate-spin" />}
          {t('connectorMarket.tokenModal.saveAndConnect')}
        </button>
      </div>
    </div>
  );
}
