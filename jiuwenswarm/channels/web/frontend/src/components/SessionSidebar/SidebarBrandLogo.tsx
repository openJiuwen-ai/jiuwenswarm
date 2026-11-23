/**
 * 侧栏 Logo（Gateway S_08 第 3.3 节）：外观标识部件的 Logo，没有提供者或加载失败时为宿主 Logo。
 */

import { DEFAULT_BRAND_LOGO, useBrandIdentity, useBrandImage } from '../../features/webui';

export function SidebarBrandLogo() {
  const identity = useBrandIdentity();
  const logo = useBrandImage(identity?.logo, DEFAULT_BRAND_LOGO);
  return (
    <img src={logo.src} onError={logo.onError} alt="Logo" width="28" height="28"
      data-testid="session-sidebar-logo-image" data-variant={logo.src === DEFAULT_BRAND_LOGO ? 'default' : 'custom'} />
  );
}
