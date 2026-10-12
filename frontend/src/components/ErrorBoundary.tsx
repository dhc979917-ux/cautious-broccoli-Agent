import { Component, ErrorInfo, ReactNode } from 'react'
import { PRIMARY_COLOR } from '../constants/theme'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

/**
 * 全局错误边界：捕获子树渲染异常，避免整站白屏。
 * 提供错误摘要与一键重载；错误详情输出到 console 便于排查。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary] 渲染异常:', error, info.componentStack)
  }

  handleReload = () => {
    window.location.reload()
  }

  handleHome = () => {
    window.location.href = '/'
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children
    }
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 16,
          padding: 24,
          background: 'var(--bg-primary, #fff)',
          color: 'var(--text-primary, #1f2937)',
          fontFamily: 'inherit',
        }}
      >
        <div style={{ fontSize: 20, fontWeight: 600 }}>页面渲染出现异常</div>
        <div style={{ fontSize: 13, opacity: 0.7, maxWidth: 560, wordBreak: 'break-all' }}>
          {this.state.error?.message || '未知错误'}
        </div>
        <div style={{ display: 'flex', gap: 12, marginTop: 8 }}>
          <button
            onClick={this.handleReload}
            style={{
              padding: '6px 18px',
              borderRadius: 8,
              border: 'none',
              background: PRIMARY_COLOR,
              color: '#fff',
              cursor: 'pointer',
            }}
          >
            重新加载
          </button>
          <button
            onClick={this.handleHome}
            style={{
              padding: '6px 18px',
              borderRadius: 8,
              border: '1px solid #d9d9d9',
              background: 'transparent',
              color: 'inherit',
              cursor: 'pointer',
            }}
          >
            返回首页
          </button>
        </div>
      </div>
    )
  }
}
