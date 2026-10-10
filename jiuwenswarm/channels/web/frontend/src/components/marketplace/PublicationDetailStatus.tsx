import { useTranslation } from 'react-i18next';
import { useAssetPublication } from '../../hooks/useAssetPublication';
import { publicationDetailLabel } from '../../features/assetPublication';
import type { PublishAssetKind } from '../../types/assetPublish';
export function PublicationDetailStatus({ kind, localId }: { kind: PublishAssetKind; localId: string }) {
 const { i18n } = useTranslation();
 const lookup = useAssetPublication([{ kind, local_id: localId }]);
 const label = publicationDetailLabel(lookup({ kind, local_id: localId }), i18n.language);
 return label ? <span className="rounded bg-secondary px-2 py-1 text-xs text-text-muted" data-testid="marketplace-publication-detail-status">{i18n.language.startsWith('zh') ? '发布状态：' : 'Publication: '}{label}</span> : null;
}
