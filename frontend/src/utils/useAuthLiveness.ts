// 全局登录态存活校验：挂在 Layout 上，整个登录期间生效。
//
// 为什么需要它：权限快照存在 localStorage（store/authStore.ts），管理员改了角色/权限后
// 后端会吊销该用户的 token（users.token_version 自增），但前端只有在"发出下一个请求"时
// 才会发现。用户停在某个不发请求的页面上就会一直挂着旧界面，所以这里做一个轻量轮询：
//   - 401 → api/request.ts 的响应拦截器统一 forceLogout，用户立刻回到登录页；
//   - 200 → 顺手用服务端返回的角色/权限刷新本地快照（仅在真的变化时写 store，避免无谓重渲染）。
import { useEffect } from 'react'
import request from '../api/request'
import { useAuthStore } from '../store/authStore'

const POLL_MS = 10_000

export function useAuthLiveness(): void {
  const token = useAuthStore((state) => state.token)

  useEffect(() => {
    if (!token) return
    let disposed = false

    const check = async () => {
      // 后台标签页不轮询：与通知铃铛、调度任务页的做法保持一致
      if (document.visibilityState !== 'visible') return
      try {
        const { data } = await request.get('/auth/me')
        if (disposed || !data) return
        const current = useAuthStore.getState().user
        const next = {
          role: data.role as string,
          permissions: (data.permissions || []) as string[],
          agent_permissions: (data.agent_permissions || []) as string[],
        }
        const changed =
          current?.role !== next.role ||
          JSON.stringify(current?.permissions || []) !== JSON.stringify(next.permissions) ||
          JSON.stringify(current?.agent_permissions || []) !== JSON.stringify(next.agent_permissions)
        if (changed) useAuthStore.getState().updateUser(next)
      } catch {
        // 401/网络异常都不在这里处理：401 由响应拦截器统一下线，网络抖动等下一轮
      }
    }

    check()
    const timer = window.setInterval(check, POLL_MS)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [token])
}
