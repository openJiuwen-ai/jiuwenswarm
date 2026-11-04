import { settingsNavigationIcons } from '../../../../assets/settings';
import type { SettingsModuleDefinition } from '../../registry/types';
import {
  A2UISetting,
  ExternalCliCustomAgentsItem,
  ProactiveLimitsSetting,
  RSISetting,
  TrajectoryDiagnosisSetting,
} from './ExperimentalSettings';
import { CollaborationVerifierInterval } from './MultiModelCollaborationSettings';
import { A4PSettings } from './A4PSettings';

export const experimentalModule: SettingsModuleDefinition = {
  id: 'experimental',
  titleKey: 'settingsPanel.categories.experimental',
  icon: settingsNavigationIcons.experimental,
  source: 'config',
  sections: [
    {
      id: 'external-cli-custom-agents',
      titleKey: 'settingsPanel.experimental.externalCliAgents',
      items: [{ id: 'external-cli-custom-agents', component: 'custom', render: ExternalCliCustomAgentsItem }],
    },
    {
      id: 'rsi',
      titleKey: 'settingsPanel.experimental.rsi',
      items: [{ id: 'rsi-enabled', component: 'custom', render: RSISetting }],
    },
    {
      id: 'a2ui',
      titleKey: 'settingsPanel.experimental.a2ui',
      items: [{ id: 'a2ui', component: 'custom', render: A2UISetting }],
    },
    {
      id: 'a4p',
      titleKey: 'a4pSettings.title',
      separatedRows: true,
      items: [{ id: 'a4p-settings', component: 'custom', render: A4PSettings }],
    },
    {
      id: 'trajectory-ui',
      titleKey: 'settingsPanel.experimental.trajectoryUi',
      items: [
        { id: 'trajectory-diagnosis-enabled', component: 'custom', render: TrajectoryDiagnosisSetting },
      ],
    },
    {
      id: 'skill-pack',
      titleKey: 'settingsPanel.experimental.skillPack',
      items: [
        {
          id: 'discover-skill-pack-enabled',
          component: 'switch',
          key: 'symphony_evolution_enabled',
        },
      ],
    },
    {
      id: 'kv-cache-affinity',
      titleKey: 'settingsPanel.experimental.kvCacheAffinity',
      items: [
        {
          id: 'kv-cache-affinity-enabled',
          component: 'switch',
          key: 'kv_cache_affinity_enabled',
        },
      ],
    },
    {
      id: 'team-organization',
      titleKey: 'settingsPanel.experimental.teamOrganization',
      items: [{ id: 'team-organization-ui-enabled', component: 'switch', key: 'team_organization_ui_enabled' }],
    },
    {
      id: 'team-scheduled-dispatch',
      titleKey: 'settingsPanel.experimental.teamScheduledDispatch',
      items: [
        {
          id: 'team-scheduled-dispatch-enabled',
          component: 'switch',
          key: 'team_scheduled_dispatch_enabled',
          subItems: {
            show: 'always',
            disabled: 'when-parent-unchecked',
            uncheckWithParent: true,
            items: [
              { id: 'team-task-verification-enabled', component: 'switch', key: 'team_task_verification_enabled' },
            ],
          },
        },
      ],
    },
    {
      id: 'proactive-recommendation',
      titleKey: 'settingsPanel.experimental.proactive',
      items: [
        {
          id: 'proactive-recommendation',
          component: 'switch',
          key: 'proactive_recommendation_enabled',
          subItems: {
            show: 'always',
            disabled: 'when-parent-unchecked',
            items: [{ id: 'proactive-limits', component: 'custom', render: ProactiveLimitsSetting }],
          },
        },
      ],
    },
    {
      id: 'multi-model-collaboration',
      titleKey: 'settingsPanel.experimental.multiModelCollaboration',
      items: [
        {
          id: 'multi-model-collaboration-enabled',
          component: 'switch',
          key: 'multi_model_collaboration_enabled',
          subItems: {
            show: 'when-parent-checked',
            disabled: 'never',
            items: [
              { id: 'collaboration-jev-enabled', component: 'switch', key: 'moa_jev_enabled', errorDisplay: 'inline' },
              { id: 'multi-model-verification', component: 'custom', render: CollaborationVerifierInterval },
            ],
          },
        },
      ],
    },
    {
      id: 'tool-optimize',
      titleKey: 'settingsPanel.experimental.toolOptimize',
      items: [
        {
          id: 'progressive-tool-optimize-enabled',
          component: 'switch',
          key: 'progressive_tool_optimize',
        },
      ],
    },
    {
      id: 'computer-control',
      visibleWhen: 'cua_backend_available',
      titleKey: 'settingsPanel.experimental.computerControl',
      items: [{ id: 'cua-agent-enabled', component: 'switch', key: 'cua_agent_enabled' }],
    },
    {
      id: 'design-mode',
      titleKey: 'settingsPanel.experimental.designMode',
      items: [
        {
          id: 'comfyui-workflow-enabled',
          component: 'switch',
          key: 'comfyui_workflow_enabled',
        },
      ],
    },
  ],
};
