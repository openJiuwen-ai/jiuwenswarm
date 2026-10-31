import { useTranslation } from 'react-i18next';
import { Button } from '../../../components/ui/Button/Button';
import { sharingTime, type GroupSharingApi, type SharingTimestamp, type SharingTarget } from '../groupSharingApi';
import { useGroupSharing } from '../useGroupSharing';
import './GroupSharingPermissions.css';

interface GroupSharingPermissionsProps {
  api: GroupSharingApi;
  configId: string;
  connectionEpoch: number;
}

export function GroupSharingPermissions({ api, configId, connectionEpoch }: GroupSharingPermissionsProps) {
  const { t, i18n } = useTranslation();
  const sharing = useGroupSharing(api, configId, connectionEpoch);
  const grants = sharing.data?.grants ?? [];
  const blocked = sharing.loading || sharing.loadError || Boolean(sharing.busy);
  const date = (value: SharingTimestamp) => {
    const time = sharingTime(value);
    return Number.isFinite(time) ? new Date(time).toLocaleString(i18n.language) : t('jiuwenmate.sharing.unknownTime');
  };
  const destination = (target: SharingTarget) =>
    [target.name || target.id, target.channel, target.app_id, target.kind, target.id].filter(Boolean).join(' · ');

  return (
    <section
      className="jiuwenmate-sharing-permissions"
      data-testid="jiuwenmate-profile-sharing-permissions"
      aria-busy={sharing.loading}
    >
      <header className="jiuwenmate-sharing-permissions__header">
        <div>
          <h3 data-testid="jiuwenmate-profile-sharing-grants-title">{t('jiuwenmate.sharing.grantsTitle')}</h3>
          <p>{t('jiuwenmate.sharing.grantsNote')}</p>
        </div>
        <Button
          type="button"
          disabled={sharing.loading || Boolean(sharing.busy)}
          onClick={sharing.refresh}
          data-testid="jiuwenmate-profile-sharing-refresh"
        >{t(sharing.loading ? 'common.loading' : 'common.refresh')}</Button>
      </header>
      {sharing.loadError ? (
        <p role="alert" data-testid="jiuwenmate-profile-sharing-load-error">{t('jiuwenmate.sharing.loadError')}</p>
      ) : null}
      {sharing.actionError ? (
        <p role="alert" data-testid="jiuwenmate-profile-sharing-action-error">{t('jiuwenmate.sharing.actionError')}</p>
      ) : null}
      {!sharing.loading && !sharing.loadError && grants.length === 0 ? (
        <p data-testid="jiuwenmate-profile-sharing-empty">{t('jiuwenmate.sharing.noGrants')}</p>
      ) : null}
      {grants.length > 0 ? (
        <div className="jiuwenmate-sharing-permissions__table-wrap" data-testid="jiuwenmate-profile-sharing-grants">
          <table aria-label={t('jiuwenmate.sharing.grantsTitle')}>
            <thead>
              <tr>
                <th>{t('jiuwenmate.sharing.source')}</th>
                <th>{t('jiuwenmate.sharing.target')}</th>
                <th>{t('jiuwenmate.sharing.contentTypes')}</th>
                <th>{t('jiuwenmate.sharing.statusLabel')}</th>
                <th>{t('jiuwenmate.sharing.createdAt')}</th>
                <th>{t('jiuwenmate.sharing.actionsLabel')}</th>
              </tr>
            </thead>
            <tbody>
              {grants.map((grant) => (
                <tr
                  key={grant.grant_id}
                  data-testid="jiuwenmate-profile-sharing-grant-row"
                  data-variant={grant.grant_id}
                  data-status={grant.status}
                  aria-busy={sharing.busy === `grant:${grant.grant_id}`}
                >
                  <td>
                    {[grant.source_platform
                      ? t(`jiuwenmate.im.${grant.source_platform}`, { defaultValue: grant.source_platform }) : '',
                    grant.source_group_name || grant.source_group_id].filter(Boolean).join(' · ')}
                    <small>{grant.source_group_id}</small>
                  </td>
                  <td>{destination(grant.target)}</td>
                  <td>{grant.content_types.map((kind) =>
                    t('jiuwenmate.sharing.contentType.' + kind)).join(', ')}</td>
                  <td data-testid="jiuwenmate-profile-sharing-grant-status" data-variant={grant.status}>
                    {t(`jiuwenmate.sharing.status.${grant.status}`)}
                  </td>
                  <td>{date(grant.created_at)}</td>
                  <td>
                    <Button
                      type="button"
                      disabled={blocked || grant.status !== 'active'}
                      onClick={() => sharing.revoke(grant.grant_id)}
                      data-testid="jiuwenmate-profile-sharing-revoke"
                    >{t('jiuwenmate.sharing.revoke')}</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
