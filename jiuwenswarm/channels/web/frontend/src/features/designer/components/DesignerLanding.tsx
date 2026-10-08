import { Loader2, Plus, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import beeStaticIcon from '../../../assets/bee-static.png';
import AttachmentIcon from '../../../assets/agent-management/attachment.svg?react';
import sendActiveIcon from '../../../assets/send_active.svg';
import sendIcon from '../../../assets/send.svg';
import ChatModelSelector from '../../../components/ChatPanel/ChatModelSelector';
import { generateUuidV4 } from '../../../utils/uuid';
import { useWorkspaceStore } from '../../../stores';
import { useChatStore } from '../../../stores/chatStore';
import {
  resolveChatModelSelection,
  useSessionStore,
} from '../../../stores/sessionStore';
import { useDesignerChatStore } from '../designerChatStore';
import { designerWorkspaceClient } from '../designerGraphClient';
import { useDesignerStore } from '../designerStore';
import { filesToBootstrapReferences } from '../designerReferences';
import './DesignerLanding.css';

type DesignerLandingProps = {
  onCreated: (projectId: string, sessionId: string) => void;
};

export function DesignerLanding({ onCreated }: DesignerLandingProps) {
  const { t, i18n } = useTranslation();
  const [prompt, setPrompt] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const createAttemptRef = useRef<{ signature: string; token: string } | null>(null);
  const isZh = i18n.language.startsWith('zh');

  useEffect(() => {
    if (!attachMenuOpen) return;
    const closeMenu = (event: PointerEvent) => {
      if (!attachMenuRef.current?.contains(event.target as Node)) {
        setAttachMenuOpen(false);
      }
    };
    window.addEventListener('pointerdown', closeMenu);
    return () => window.removeEventListener('pointerdown', closeMenu);
  }, [attachMenuOpen]);

  const submit = useCallback(async () => {
    const content = prompt.trim();
    if (!content || submitting) return;
    setSubmitting(true);
    setError('');
    useDesignerStore.getState().beginBootstrapEntry(content);
    try {
      const converted = await filesToBootstrapReferences(files);
      if (converted.error) throw new Error(t('designer.chat.unsupported'));
      const activeSessionId = useChatStore.getState().activeSessionId;
      const sessionState = useSessionStore.getState();
      const selectedModel = resolveChatModelSelection(
        sessionState.chatAvailableModels,
        sessionState.runtimes[activeSessionId ?? '']?.selectedModelName ?? null,
        sessionState.defaultModelName,
      );
      const modelName = selectedModel?.model_name;
      const signature = JSON.stringify({
        prompt: content,
        modelName,
        files: files.map((file) => [
          file.name,
          file.size,
          file.type,
          file.lastModified,
        ]),
      });
      let attempt = createAttemptRef.current;
      if (attempt?.signature !== signature) {
        attempt = {
          signature,
          token: generateUuidV4(),
        };
        createAttemptRef.current = attempt;
      }
      const workspace = await designerWorkspaceClient.create({
        prompt: content,
        createToken: attempt.token,
        modelName,
        references: converted.refs,
      });
      createAttemptRef.current = null;
      useDesignerStore.getState().applyGraph(workspace.graph);
      useDesignerChatStore.getState().replaceMessages(
        workspace.graph.graph_id,
        workspace.messages,
      );
      await useWorkspaceStore.getState().loadProjects();
      const project = useWorkspaceStore.getState().projects.find(
        (item) => item.project_id === workspace.project.project_id,
      );
      if (project) useWorkspaceStore.getState().setSelectedProject(project);
      onCreated(workspace.project.project_id, workspace.session.session_id);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      useDesignerStore.getState().failBootstrapEntry(message);
      setError(message || t('designer.chat.bootstrapError'));
    } finally {
      setSubmitting(false);
    }
  }, [files, onCreated, prompt, submitting, t]);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }, [submit]);

  return (
    <section className="designer-landing" data-testid="designer-landing">
      <div className="designer-landing__content">
        <h1 className="designer-landing__heading">
          <span className="chat-welcome__heading-highlight">WorkSwarm</span>
          <span>{isZh ? "轻松提供设计创意！" : "makes design easier!"}</span>
        </h1>
        <div className="designer-landing__composer-stage">
          <img
            className="designer-landing__bee"
            src={beeStaticIcon}
            alt={t('chat.welcomeLogoAlt')}
          />
          <div className="designer-landing__composer">
            <div className="designer-landing__input">
              {files.length > 0 ? (
                <div className="designer-landing__files">
                  {files.map((file, index) => (
                    <span key={`${file.name}-${file.size}-${index}`}>
                      {file.name}
                      <button
                        type="button"
                        onClick={() => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                        aria-label={t('designer.chat.removeAttachment')}
                      >
                        <X size={12} />
                      </button>
                    </span>
                  ))}
                </div>
              ) : null}
              <textarea
                className='chat-input-editor'
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={onKeyDown}
                disabled={submitting}
                autoFocus
                placeholder={t('designer.chat.emptyHint')}
                data-testid="designer-landing-input"
              />
              <div className="designer-landing__toolbar">
                <div ref={attachMenuRef} className="chat-input-attach-menu-anchor">
                  <button
                    type="button"
                    className={`chat-input-btn chat-input-btn--add-file${attachMenuOpen ? ' chat-input-btn--menu-open' : ''}${submitting ? ' chat-input-btn--disabled' : ''}`}
                    onClick={() => setAttachMenuOpen((open) => !open)}
                    disabled={submitting}
                    aria-label={t('chat.addFile')}
                    aria-haspopup="menu"
                    aria-expanded={attachMenuOpen}
                    data-testid="designer-landing-attach"
                  >
                    <Plus size={17} strokeWidth={1.8} aria-hidden />
                  </button>
                  {attachMenuOpen ? (
                    <div className="designer-landing__attach-menu chat-mode-select__menu chat-input-attach-menu" role="menu">
                      <button
                        type="button"
                        className="chat-mode-select__option"
                        role="menuitem"
                        onClick={() => {
                          setAttachMenuOpen(false);
                          fileInputRef.current?.click();
                        }}
                        data-testid="designer-landing-attach-file"
                      >
                        <span className="chat-mode-select__option-main">
                          <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden>
                            <AttachmentIcon />
                          </span>
                          <span className="chat-mode-select__label">{t('chat.addFile')}</span>
                        </span>
                      </button>
                    </div>
                  ) : null}
                </div>
                <div className="designer-landing__actions">
                  <ChatModelSelector disabled={submitting} />
                  <button
                    type="button"
                    className={prompt.trim() && !submitting ? 'designer-landing__send designer-landing__send--active' : 'designer-landing__send'}
                    onClick={() => void submit()}
                    disabled={!prompt.trim() || submitting}
                    aria-label={t('chat.send')}
                    data-testid="designer-landing-submit"
                  >
                    {submitting
                      ? <Loader2 className="designer-landing__spinner" size={18} aria-hidden />
                      : (
                        <img
                          src={prompt.trim() ? sendActiveIcon : sendIcon}
                          alt=""
                          aria-hidden
                        />
                      )}
                  </button>
                </div>
              </div>
            </div>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,video/*,audio/*"
            hidden
            onChange={(event) => {
              const selected = Array.from(event.target.files ?? []);
              setFiles((current) => [...current, ...selected]);
              event.target.value = '';
            }}
          />
        </div>
      </div>
      <div className="chat-ai-disclaimer designer-landing__disclaimer" data-testid="designer-landing-ai-disclaimer">
        {t('share.aiNotice')}
      </div>
      {error ? (
        <div className="app-toast-wrapper app-toast-wrapper--top-center" data-testid="designer-landing-error-toast">
          <div className="app-connection-toast" role="alert">{error}</div>
        </div>
      ) : null}
    </section>
  );
}
