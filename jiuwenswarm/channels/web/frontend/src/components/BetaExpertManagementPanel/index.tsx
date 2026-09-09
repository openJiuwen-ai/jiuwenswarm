import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  PackageCheck,
  Play,
  Search,
  Sparkles,
  Users,
  Workflow,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';
import {
  normalizeBetaExpertCatalog,
  type BetaExpertCatalogItem,
} from '../../features/betaExpertCatalog';
import './betaExpertManagement.css';

interface BetaExpertManagementPanelProps {
  onUseExpert: (expert: BetaExpertCatalogItem, prompt?: string) => Promise<void>;
}

const VISUALS: Record<string, { icon: string; accent: string; soft: string }> = {
  'habit-dashboard-coach': { icon: '习', accent: '#5b6cf9', soft: '#eef0ff' },
  'travel-journal-designer': { icon: '旅', accent: '#f08b52', soft: '#fff2e9' },
  'xiaohongshu-content-studio': { icon: '红', accent: '#ef5a73', soft: '#fff0f3' },
  'data-dashboard-analyst': { icon: '数', accent: '#1f9a78', soft: '#e9f8f2' },
};

function visualFor(id: string) {
  return VISUALS[id] || { icon: '专', accent: '#6d5ce7', soft: '#f0edff' };
}

function fallbackAudiences(expert: BetaExpertCatalogItem): string[] {
  if (expert.audiences.length > 0) return expert.audiences;
  return ['希望用一句话快速得到可直接使用成品的用户'];
}

function fallbackDeliverables(expert: BetaExpertCatalogItem): string[] {
  if (expert.deliverables.length > 0) return expert.deliverables;
  return ['一个可直接打开、继续编辑和展示的完整成品'];
}

function fallbackWorkflow(expert: BetaExpertCatalogItem): string[] {
  if (expert.workflow.length > 0) return expert.workflow;
  return expert.skills.map((skill) => skill.name);
}

export function BetaExpertManagementPanel({ onUseExpert }: BetaExpertManagementPanelProps) {
  const panelRef = useRef<HTMLElement>(null);
  const [experts, setExperts] = useState<BetaExpertCatalogItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadExperts = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const payload = await webRequest('experts.list', {});
      setExperts(normalizeBetaExpertCatalog(payload));
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '专家列表加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadExperts();
  }, []);

  useEffect(() => {
    panelRef.current?.scrollTo({ top: 0, behavior: 'auto' });
  }, [selectedId]);

  const selected = experts.find((expert) => expert.id === selectedId) || null;
  const filteredExperts = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return experts;
    return experts.filter((expert) =>
      [expert.name, expert.description, expert.profession, ...expert.tags]
        .join(' ')
        .toLowerCase()
        .includes(keyword),
    );
  }, [experts, query]);

  const useExpert = async (expert: BetaExpertCatalogItem, prompt?: string) => {
    if (busy || !expert.available) return;
    setBusy(true);
    setActionError(null);
    try {
      await onUseExpert(expert, prompt);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '创建专家会话失败，请重试');
      setBusy(false);
    }
  };

  if (selected) {
    const visual = visualFor(selected.id);
    const audiences = fallbackAudiences(selected);
    const deliverables = fallbackDeliverables(selected);
    const workflow = fallbackWorkflow(selected);
    return (
      <section ref={panelRef} className="beta-experts beta-experts--detail">
        <div className="beta-experts__detail-wrap">
          <button className="beta-experts__back" type="button" onClick={() => setSelectedId(null)}>
            <ArrowLeft size={18} /> 返回专家广场
          </button>

          <header className="beta-experts__hero">
            <div className="beta-experts__identity">
              <div className="beta-experts__avatar beta-experts__avatar--large" style={{ color: visual.accent, background: visual.soft }}>
                {visual.icon}
              </div>
              <div>
                <div className="beta-experts__eyebrow">
                  <span>{selected.profession || '小艺 Work 专家'}</span>
                  <span className="beta-experts__installed"><CheckCircle2 size={14} /> 已安装</span>
                </div>
                <h1>{selected.name}</h1>
                <p>{selected.description}</p>
              </div>
            </div>
            <button
              className="beta-experts__use"
              type="button"
              disabled={busy || !selected.available}
              onClick={() => void useExpert(selected)}
            >
              <Play size={17} fill="currentColor" />
              {busy ? '正在创建会话…' : '使用专家'}
            </button>
          </header>

          {actionError && <div className="beta-experts__error">{actionError}</div>}

          <div className="beta-experts__summary-grid">
            <article>
              <Users size={20} />
              <div><h2>适合谁</h2>{audiences.map((item) => <p key={item}>{item}</p>)}</div>
            </article>
            <article>
              <PackageCheck size={20} />
              <div><h2>你会得到</h2>{deliverables.map((item) => <p key={item}>{item}</p>)}</div>
            </article>
            <article>
              <Workflow size={20} />
              <div><h2>完成方式</h2><p>{workflow.length} 个步骤连续协作，一句话即可启动</p></div>
            </article>
          </div>

          <div className="beta-experts__section">
            <div className="beta-experts__section-heading"><h2>擅长领域</h2></div>
            <div className="beta-experts__tags">
              {selected.tags.map((tag) => <span key={tag}>{tag}</span>)}
            </div>
          </div>

          <div className="beta-experts__section">
            <div className="beta-experts__section-heading">
              <h2>Skill 调用链</h2>
              <span>名称原样读取自包内 SKILL.md，按专家编排顺序展示</span>
            </div>
            <div className="beta-experts__skill-chain">
              {selected.skills.map((skill, index) => (
                <div className="beta-experts__skill-step" key={`${skill.name}-${index}`}>
                  <span className="beta-experts__skill-index">{index + 1}</span>
                  <div><strong>{skill.name}</strong>{(workflow[index] || skill.description) && <p>{workflow[index] || skill.description}</p>}</div>
                  {index < selected.skills.length - 1 && <ArrowRight className="beta-experts__skill-arrow" size={18} />}
                </div>
              ))}
            </div>
          </div>

          <div className="beta-experts__section beta-experts__section--prompts">
            <div className="beta-experts__section-heading"><h2>这样问我试试看</h2><span>点击即可带入专家会话</span></div>
            <div className="beta-experts__prompts">
              {selected.quickPrompts.map((prompt) => (
                <button key={prompt} type="button" disabled={busy} onClick={() => void useExpert(selected, prompt)}>
                  <span>{prompt}</span><ArrowRight size={18} />
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section ref={panelRef} className="beta-experts">
      <div className="beta-experts__catalog-wrap">
        <header className="beta-experts__catalog-header">
          <div>
            <div className="beta-experts__title-line"><Sparkles size={23} /><h1>专家广场</h1></div>
            <p>选择一个擅长交付成品的专家，用一句话开始工作。</p>
          </div>
          <label className="beta-experts__search">
            <Search size={18} />
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索专家或能力" />
          </label>
        </header>

        <div className="beta-experts__catalog-meta">
          <strong>已安装专家</strong><span>{experts.length} 个</span>
        </div>

        {loading && <div className="beta-experts__state">正在加载专家…</div>}
        {loadError && (
          <div className="beta-experts__state beta-experts__state--error">
            <span>{loadError}</span><button type="button" onClick={() => void loadExperts()}>重新加载</button>
          </div>
        )}
        {!loading && !loadError && filteredExperts.length === 0 && (
          <div className="beta-experts__state">没有找到匹配的专家</div>
        )}

        <div className="beta-experts__grid">
          {filteredExperts.map((expert) => {
            const visual = visualFor(expert.id);
            const primaryDeliverable = fallbackDeliverables(expert)[0];
            return (
              <article className="beta-experts__card" key={expert.id}>
                <div className="beta-experts__card-top">
                  <div className="beta-experts__avatar" style={{ color: visual.accent, background: visual.soft }}>{visual.icon}</div>
                  <span className="beta-experts__installed"><CheckCircle2 size={14} /> 已安装</span>
                </div>
                <h2>{expert.name}</h2>
                <div className="beta-experts__profession">{expert.profession || '专业工作助手'}</div>
                <p className="beta-experts__description">{expert.description}</p>
                <div className="beta-experts__deliverable"><PackageCheck size={16} /><span>{primaryDeliverable}</span></div>
                <div className="beta-experts__tags beta-experts__tags--compact">
                  {expert.tags.slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}
                </div>
                <button className="beta-experts__detail-button" type="button" onClick={() => setSelectedId(expert.id)}>
                  查看详情 <ArrowRight size={17} />
                </button>
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}
