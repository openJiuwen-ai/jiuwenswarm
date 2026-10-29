/**
 * 实例级错误边界：组件渲染异常只降级当前实例，并向挂载层反馈当前帧的失败。
 * resetKey 变化（新帧）时清除错误状态，旧帧的失败反馈不会污染新帧。
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

export interface WebUIInstanceErrorBoundaryProps {
  children: ReactNode;
  /** 变化时清除错误状态，例如实例当前帧的 revision。 */
  resetKey?: string | number;
  fallback?: ReactNode;
  /** 崩溃反馈；调用方应携带当前帧关联信息。 */
  onError?: (error: Error) => void;
}

interface State {
  error: Error | null;
}

export class WebUIInstanceErrorBoundary extends Component<WebUIInstanceErrorBoundaryProps, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError?.(error);
    console.error('[WebUI] instance render failed', error, info.componentStack);
  }

  componentDidUpdate(previous: WebUIInstanceErrorBoundaryProps): void {
    if (this.state.error && previous.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        this.props.fallback ?? (
          <div className="webui-instance-error" data-testid="webui-instance-error" data-variant="render_failed" role="alert">
            {this.state.error.message}
          </div>
        )
      );
    }
    return this.props.children;
  }
}
