<<<<<<< HEAD
import { useTranslation } from 'react-i18next';

import { useChatStore } from '../../../stores/chatStore';
import { useSessionStore } from '../../../stores/sessionStore';
import { designerModelLabel } from '../designerModelLabel';
import './DesignerModelBadge.css';

/**
 * Read-only model label for the canvas chat dock.
 *
 * The canvas dock used to render the shared chat model selector, but a Design
 * round always runs on the model the session was created with, so picking a
 * different model there changed nothing (#5266). The landing keeps its picker
 * because that choice becomes the session's model; inside the canvas the model
 * is shown, never switched.
 */
export default function DesignerModelBadge({ sessionId }: { sessionId?: string }): JSX.Element {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const boundSessionId = sessionId || activeSessionId || '';
  const models = useSessionStore((state) => state.chatAvailableModels);
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[boundSessionId]?.selectedModelName ?? null,
  );
  const defaultModelName = useSessionStore((state) => state.defaultModelName);
  const label = designerModelLabel(models, selectedModelName, defaultModelName);
  return (
    <span
      className="designer-model-badge"
      title={t('designer.models.lockedHint')}
      data-testid="designer-model-badge"
    >
      {label || t('designer.models.notSelected')}
    </span>
  );
}
||||||| e487e9a30
=======
import { useTranslation } from 'react-i18next';

import { useChatStore } from '../../../stores/chatStore';
import { useSessionStore } from '../../../stores/sessionStore';
import { useDesignerStore } from '../designerStore';
import { designerModelLabel } from '../designerModelLabel';
import './DesignerModelBadge.css';

/**
 * Read-only model label for the canvas chat dock.
 *
 * The canvas dock used to render the shared chat model selector, but a Design
 * round always runs on the model the session was created with, so picking a
 * different model there changed nothing (#5266). The landing keeps its picker
 * because that choice becomes the session's model; inside the canvas the model
 * is shown, never switched.
 */
export default function DesignerModelBadge({ sessionId }: { sessionId?: string }): JSX.Element {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const boundSessionId = sessionId || activeSessionId || '';
  const models = useSessionStore((state) => state.chatAvailableModels);
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[boundSessionId]?.selectedModelName ?? null,
  );
  const selectedModelId = useSessionStore((state) => {
    const selection = state.runtimes[boundSessionId]?.selectedModelSelection;
    return selection?.type === 'model' ? selection.id : null;
  });
  const graphModelId = useDesignerStore((state) => {
    const raw = state.domainGraph?.metadata?.model_id;
    return typeof raw === 'string' ? raw : null;
  });
  const defaultModelName = useSessionStore((state) => state.defaultModelName);
  const label = designerModelLabel(
    models,
    selectedModelName,
    defaultModelName,
    selectedModelId || graphModelId,
  );
  return (
    <span
      className="designer-model-badge"
      title={t('designer.models.lockedHint')}
      data-testid="designer-model-badge"
    >
      {label || t('designer.models.notSelected')}
    </span>
  );
}
>>>>>>> origin/develop
