import { Headphones, Image as ImageIcon, Sheet, Video, X } from 'lucide-react';
import { useCallback, useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { designerAssetPreviewUrl } from '../../designerAssetUrl';
import { preferredDesignerPreviewRef } from '../../designerMaterials';
import { useDesignerAssetLibraryStore } from '../../designerAssetLibraryStore';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import {
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_VIDEO,
} from '../../executionGraphTypes';
import {
  isMediaNodeType,
  isTextLikeNodeType,
  readMediaConfig,
  writeMediaMaterials,
} from '../../mediaNodeConfig';

type DesignerMaterialStripProps = {
  nodeId: string;
  nodeType: string;
};

type LinkedMaterial = {
  kind: 'linked';
  key: string;
  edgeId: string;
  sourceNodeId: string;
  label: string;
  mediaType: string;
  previewUrl?: string | null;
};

type UploadMaterial = {
  kind: 'upload';
  key: string;
  materialId: string;
  assetId?: string;
  label: string;
  mimeType?: string;
  previewUrl?: string;
};

type DisplayMaterial = LinkedMaterial | UploadMaterial;

function mediaIcon(mediaTypeOrMime: string | undefined): ReactNode {
  const value = (mediaTypeOrMime ?? '').toLowerCase();
  if (value === DESIGNER_NODE_TYPE_VIDEO || value.startsWith('video/')) {
    return <Video size={20} aria-hidden />;
  }
  if (value === DESIGNER_NODE_TYPE_AUDIO || value.startsWith('audio/')) {
    return <Headphones size={20} aria-hidden />;
  }
  if (value === DESIGNER_NODE_TYPE_TABLE || value === DESIGNER_NODE_TYPE_TEXT) {
    return <Sheet size={20} aria-hidden />;
  }
  return <ImageIcon size={20} aria-hidden />;
}

export function DesignerMaterialStrip({ nodeId, nodeType }: DesignerMaterialStripProps) {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  const removeEdges = useDesignerStore((state) => state.removeEdges);
  const libraryAssets = useDesignerAssetLibraryStore((state) => state.assets);
  const nodeStates = useDesignerRunStore((state) => state.nodeStates);

  const config = useMemo(() => {
    const node = domainGraph?.nodes.find((item) => item.id === nodeId);
    return node?.config ?? {};
  }, [domainGraph, nodeId]);

  const uploads = readMediaConfig(config, nodeType).materials ?? [];

  const linked = useMemo((): LinkedMaterial[] => {
    if (!domainGraph) return [];
    const nodesById = new Map(domainGraph.nodes.map((node) => [node.id, node]));
    const items: LinkedMaterial[] = [];
    for (const edge of domainGraph.edges) {
      if (edge.target !== nodeId) continue;
      const source = nodesById.get(edge.source);
      if (!source) continue;
      const sourceType = String(source.type);
      const allowTextLike =
        nodeType === DESIGNER_NODE_TYPE_VIDEO && isTextLikeNodeType(sourceType);
      if (!isMediaNodeType(sourceType) && !allowTextLike) continue;
      const previewRef = preferredDesignerPreviewRef(
        nodeStates[source.id]?.output_ref,
        nodeStates[source.id]?.candidate_output_ref,
      );
      items.push({
        kind: 'linked',
        key: `linked:${edge.id}`,
        edgeId: edge.id,
        sourceNodeId: source.id,
        label: source.label || source.id,
        mediaType: sourceType,
        previewUrl: designerAssetPreviewUrl(previewRef?.uri),
      });
    }
    return items;
  }, [domainGraph, nodeId, nodeStates, nodeType]);

  const materials = useMemo((): DisplayMaterial[] => {
    const assetById = new Map(libraryAssets.map((asset) => [asset.id, asset]));
    const uploaded: UploadMaterial[] = uploads.map((slot) => {
      const asset = slot.asset_id ? assetById.get(slot.asset_id) : undefined;
      return {
        kind: 'upload',
        key: `upload:${slot.id}`,
        materialId: slot.id,
        assetId: slot.asset_id,
        label: slot.filename,
        mimeType: slot.mime_type ?? asset?.mime_type,
        previewUrl: asset?.objectUrl || designerAssetPreviewUrl(slot.uri) || undefined,
      };
    });
    return [...linked, ...uploaded];
  }, [libraryAssets, linked, uploads]);

  const onRemove = useCallback(
    (item: DisplayMaterial) => {
      if (item.kind === 'linked') {
        removeEdges([item.edgeId]);
        return;
      }
      updateNodeConfig(nodeId, (current) => {
        const existing = readMediaConfig(current, nodeType).materials ?? [];
        return writeMediaMaterials(
          current,
          existing.filter((slot) => slot.id !== item.materialId),
        );
      });
    },
    [nodeId, nodeType, removeEdges, updateNodeConfig],
  );

  if (materials.length === 0) return null;

  return (
    <div className="designer-node-toolbar__materials" data-testid="designer-node-toolbar-materials">
      {materials.map((item) => (
        <div
          key={item.key}
          className="designer-node-toolbar__material-item"
          data-testid="designer-node-toolbar-material"
          data-material-kind={item.kind}
        >
          <span className="designer-node-toolbar__material-label" title={item.label}>
            {item.label}
          </span>
          <div className="designer-node-toolbar__material-tile">
            {item.previewUrl &&
            (item.kind === 'upload'
              ? Boolean(item.mimeType?.startsWith('image/'))
              : item.mediaType !== DESIGNER_NODE_TYPE_TABLE &&
                item.mediaType !== DESIGNER_NODE_TYPE_TEXT) ? (
              <img
                className="designer-node-toolbar__material-thumb"
                src={item.previewUrl}
                alt=""
              />
            ) : (
              mediaIcon(item.kind === 'linked' ? item.mediaType : item.mimeType)
            )}
            <button
              type="button"
              className="designer-node-toolbar__material-remove"
              aria-label={t('designer.toolbar.removeMaterial')}
              title={t('designer.toolbar.removeMaterial')}
              data-testid="designer-node-toolbar-material-remove"
              onClick={(event) => {
                event.stopPropagation();
                onRemove(item);
              }}
            >
              <X size={10} strokeWidth={2.5} aria-hidden />
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
