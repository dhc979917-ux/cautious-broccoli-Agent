// 前端"被下线"的统一出口：REST 401 拦截器与聊天 WebSocket 的 force_logout 都走这里。
// 单独成模块（只依赖 store，不依赖 axios）是为了避免与 api/request.ts 形成循环引用。
import { useAuthStore } from '../store/authStore'
import { useDraftStore } from '../store/draftStore'

// 与后端约定的下线原因：core/deps.py 的 SESSION_REVOKED_DETAIL、services/session_guard.py
export const REASON_SESSION_REVOKED = 'session_revoked'

/** 后端 401 文案里的固定标识：带它说明是权限变更导致的吊销，而非普通登录过期 */
const REVOKED_FLAG = 'SESSION_REVOKED'

const LOGIN_PATH = '/login'

export function isRevokedDetail(detail: unknown): boolean {
  return typeof detail === 'string' && detail.includes(REVOKED_FLAG)
}

/**
 * 清本地登录态与会话级缓存，整页跳到登录页（带原因供登录页提示）。
 *
 * 用 location.href 而不是路由跳转：内存里的聊天状态、WebSocket、各处轮询定时器
 * 需要随整页刷新一起丢掉，避免换人登录后看到上一个人的残留。
 */
export function forceLogout(reason: string = REASON_SESSION_REVOKED): void {
  useAuthStore.getState().logout()
  useDraftStore.setState({ drafts: {} })
  // 已经在登录页（例如登录请求本身失败）时不再跳转，避免刷掉页面上的错误提示
  if (window.location.pathname === LOGIN_PATH) return
  window.location.href = `${LOGIN_PATH}?reason=${encodeURIComponent(reason)}`
}
