import { systemPluginApi } from '../../services/extensionsV2Api';

export async function hasOtherEnabledSystemPlugin(packageId: string): Promise<boolean> {
  const packages = await systemPluginApi.catalog('mine', true);
  return packages.some(
    (pkg) => pkg.installed && pkg.enabled && (pkg.packageId || pkg.id) !== packageId,
  );
}
