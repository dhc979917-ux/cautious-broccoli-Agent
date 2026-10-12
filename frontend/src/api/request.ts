import axios from 'axios'
import { useAuthStore } from '../store/authStore'
import { forceLogout, isRevokedDetail, REASON_SESSION_REVOKED } from '../utils/forceLogout'

const PASSWORD_CHANGE_REQUIRED = 'PASSWORD_CHANGE_REQUIRED'

const request = axios.create({
  baseURL: '/api/v1',
  timeout: 30000
})

request.interceptors.request.use(
  (config) => {
    const token = useAuthStore.getState().token
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

request.interceptors.response.use(
  (response) => response,
  (error) => {
    // 只有非公开接口才触发401跳转（/register 已改为管理员专属，不再豁免）
    const url = error.config?.url || ''
    // 登录接口自身的 401（账号或密码错误）不做跳转，交给页面展示友好提示
    if (error.response?.status === 403 && String(error.response?.data?.detail || '').includes(PASSWORD_CHANGE_REQUIRED)
      && !url.includes('/users/me/password')) {
      if (window.location.pathname !== '/force-password-change') window.location.replace('/force-password-change')
    }
    if (error.response?.status === 401 && !url.includes('/public') && !url.includes('/auth/login')) {
      // 管理员改动权限导致的吊销带上专属原因，登录页据此提示；其余 401 按登录过期处理
      forceLogout(isRevokedDetail(error.response?.data?.detail) ? REASON_SESSION_REVOKED : 'session_expired')
    }
    return Promise.reject(error)
  }
)

export default request
