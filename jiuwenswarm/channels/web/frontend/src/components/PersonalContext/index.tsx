/**
 * PersonalContextPanel — 个人上下文信息页容器。
 *
 * 位于左侧导航主区（技能之后）。
 * 图谱页自带工具栏（标题 + 刷新 + 添加知识按钮），无顶部 header/tab。
 * 既无图谱数据又无采集任务时，显示介绍页 + 去完成 → 跳转内容采集页。
 * 有任一内容（采集任务或图谱信息）时，直接进入图谱页。
 */

import { useEffect } from 'react';
import { usePersonalContextStore } from '../../stores';
import { PersonalContextGraphPanel } from './GraphPanel';
import { PersonalContextServicesPanel } from './ServicesPanel';
import { PersonalContextIntro } from './Intro';
import './index.css';

interface PersonalContextPanelProps {
  isConnected: boolean;
  isActive: boolean;
}

export function PersonalContextPanel({ isConnected, isActive }: PersonalContextPanelProps) {
  const { config, graph, infoTab, setInfoTab, loadAll } = usePersonalContextStore();

  useEffect(() => {
    if (!isConnected || !isActive) return;
    void loadAll().catch(() => {});
  }, [isConnected, isActive, loadAll]);

  // 介绍页显示条件：既无图谱数据又无采集任务时展示（不再依赖开关状态）
  const hasFetchServices = config.fetch_services.length > 0;
  const hasGraphNodes = (graph?.nodes.length ?? 0) > 0;
  const hasContent = hasFetchServices || hasGraphNodes;
  const showIntro = !hasContent;

  // infoTab === 'services' 优先：用户点击"去完成"后直接进入内容采集页，
  // 不被 showIntro 拦截（已开启但暂无内容时仍可进入采集页配置）
  if (infoTab === 'services') {
    return (
      <div className="pc-panel" data-testid="personal-context-panel">
        <PersonalContextServicesPanel
          isConnected={isConnected}
          isActive={isActive}
          onBackToGraph={() => setInfoTab('graph')}
        />
      </div>
    );
  }

  if (showIntro) {
    return (
      <PersonalContextIntro
        onStart={() => {
          // 无论是否开启，直接跳转内容采集页引导用户配置采集源和任务
          setInfoTab('services');
        }}
      />
    );
  }

  return (
    <div className="pc-panel" data-testid="personal-context-panel">
      <PersonalContextGraphPanel
        isConnected={isConnected}
        isActive={isActive}
        onNavigateServices={() => setInfoTab('services')}
      />
    </div>
  );
}
