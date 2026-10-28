// 独立气泡入口：由 Electron 主进程以 WebContentsView 形式悬浮在内置浏览器
// 内容之上（原生层级，DOM 面板会被 WebContentsView 遮挡，因此复用同一组件
// 的渲染被搬到这个独立文档里）。只引主题 token 与面板样式，不加载主应用。
import '../i18n';
import ReactDOM from 'react-dom/client';
import { DownloadPanel } from '../components/DesktopBrowserPane/DownloadPanel';
import '../styles/foundation.css';
import '../styles/themes/default/light.css';
import './bubble.css';

const container = document.getElementById('root');
if (container) {
  ReactDOM.createRoot(container).render(
    <DownloadPanel onClose={() => void window.jiuwenDesktop?.browser.hideDownloadBubble()} />,
  );
}
