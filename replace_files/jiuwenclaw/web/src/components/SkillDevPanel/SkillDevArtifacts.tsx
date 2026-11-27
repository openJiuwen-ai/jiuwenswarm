/**
 * SkillDevArtifacts - 产物列表组件
 */

import { useTranslation } from 'react-i18next';
import type { SkillDevArtifact } from '../../types/skilldev';

interface SkillDevArtifactsProps {
  artifacts: SkillDevArtifact[];
  onDownload: (artifactId: string) => void;
}

export function SkillDevArtifacts({ artifacts, onDownload }: SkillDevArtifactsProps) {
  const { t } = useTranslation();

  const getArtifactIcon = (type: SkillDevArtifact['type']) => {
    switch (type) {
      case 'skill_package':
        return (
          <svg className="w-5 h-5 text-primary" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
          </svg>
        );
      case 'skill_md':
        return (
          <svg className="w-5 h-5 text-info" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
        );
      case 'test_result':
        return (
          <svg className="w-5 h-5 text-success" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
        );
      case 'report':
        return (
          <svg className="w-5 h-5 text-warning" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
        );
      default:
        return (
          <svg className="w-5 h-5 text-text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
          </svg>
        );
    }
  };

  const formatSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <div className="flex-1 p-4 overflow-y-auto">
      <h3 className="text-sm font-semibold text-text mb-3 flex items-center gap-2">
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4" />
        </svg>
        {t('skilldev.artifactsTitle')}
      </h3>
      <div className="space-y-2">
        {artifacts.length === 0 ? (
          <p className="text-xs text-text-muted italic">{t('skilldev.noArtifacts')}</p>
        ) : (
          artifacts.map((artifact) => (
            <div
              key={artifact.id}
              className="flex items-center gap-3 p-3 bg-hover rounded-lg group hover:bg-secondary transition-colors"
            >
              {getArtifactIcon(artifact.type)}
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-text truncate">{artifact.name}</p>
                <p className="text-xs text-text-muted">{formatSize(artifact.size_bytes)}</p>
              </div>
              {artifact.downloadable && (
                <button
                  onClick={() => onDownload(artifact.id)}
                  className="p-1.5 text-text-muted hover:text-primary opacity-0 group-hover:opacity-100 transition-opacity"
                  title={t('common.download')}
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                  </svg>
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
