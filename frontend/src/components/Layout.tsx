import React, { useState, useRef, useEffect } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Tooltip, Dropdown } from 'antd'
import {
  HomeOutlined,
  MessageOutlined,
  AppstoreOutlined,
  FolderOutlined,
  SettingOutlined,
  LogoutOutlined,
  UserOutlined,
  BulbOutlined,
  DatabaseOutlined,
  ApiOutlined,
  DashboardOutlined,
  ScheduleOutlined,
  ThunderboltOutlined,
  BookOutlined,
} from '@ant-design/icons'
import PlatformLogo from './PlatformLogo'
import NotificationBell from './NotificationBell'
import FeatureIntro from './FeatureIntro'
import AdminWorkspace from './AdminWorkspace'
import { useAuthStore } from '../store/authStore'
import { hasAnyPerm } from '../constants/permissions'
import { getOrgAccess } from '../api/people'
import { entranceStagger, slideIndicator } from '../lib/motion'
import { useAuthLiveness } from '../utils/useAuthLiveness'
import './Layout.css'

interface NavItem {
  key: string
  icon: React.ReactNode
  label: string
  path?: string
  disabled?: boolean
}

const Layout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, theme, toggleTheme, logout, motionEffect, setMotionEffect } = useAuthStore()
  // 登录态存活校验：管理员改了本账号的角色/权限后立刻回到登录页，并同步最新权限快照
  useAuthLiveness()
  const navRef = useRef<HTMLElement>(null)
  const indicatorRef = useRef<HTMLDivElement>(null)

  // Sidebar collapse (icon rail) state, persisted; toggled by clicking the brand area
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (window.matchMedia('(max-width: 700px)').matches) return true
    try { return localStorage.getItem('sidebar_collapsed') === '1' } catch { return false }
  })

  const [featureIntroOpen, setFeatureIntroOpen] = useState(false)

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const next = !c
      try { localStorage.setItem('sidebar_collapsed', next ? '1' : '0') } catch { /* ignore */ }
      return next
    })
  }

  const isAdmin = user?.role === 'admin'
  // 管理后台入口：admin 或拥有任一细粒度权限的用户可见
  const canEnterAdmin = isAdmin || hasAnyPerm(user)
  const [canViewPeople, setCanViewPeople] = useState(isAdmin)
  useEffect(() => {
    let active = true
    if (!user) { setCanViewPeople(false); return }
    getOrgAccess().then((result) => {
      if (active) setCanViewPeople(result.can_view_people)
    }).catch(() => { if (active) setCanViewPeople(isAdmin) })
    return () => { active = false }
  }, [user?.id, isAdmin])
  const navItems: NavItem[] = [
    { key: 'home', icon: <HomeOutlined />, label: '首页', path: '/' },
    { key: 'chat', icon: <MessageOutlined />, label: '开始问答', path: '/chat' },
    { key: 'skills', icon: <AppstoreOutlined />, label: 'Skill广场', path: '/skills' },
    { key: 'files', icon: <FolderOutlined />, label: '文件中心', path: '/files' },
    { key: 'knowledge', icon: <DatabaseOutlined />, label: '知识库', path: '/knowledge' },
    { key: 'mcp', icon: <ApiOutlined />, label: 'MCP工具', path: '/mcp' },
    { key: 'monitoring', icon: <DashboardOutlined />, label: '监控与运维', path: '/monitoring' },
    { key: 'scheduled-tasks', icon: <ScheduleOutlined />, label: '调度任务', path: '/scheduled-tasks' },
    { key: 'profile', icon: <UserOutlined />, label: '个人信息', path: '/profile' },
  ]

  const handleNavClick = (item: NavItem) => {
    if (item.disabled || !item.path) return
    navigate(item.path)
  }

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  const userMenuItems = [
    { key: 'profile', icon: <UserOutlined />, label: user?.username || 'User', disabled: true },
    { key: 'motion', icon: <ThunderboltOutlined />, label: motionEffect ? '动态效果：开' : '动态效果：关', onClick: () => setMotionEffect(!motionEffect) },
    { key: 'theme', icon: <BulbOutlined />, label: theme === 'dark' ? '\u6D45\u8272\u6A21\u5F0F' : '\u6DF1\u8272\u6A21\u5F0F', onClick: toggleTheme },
    { key: 'feature-intro', icon: <BookOutlined />, label: '\u529F\u80FD\u4ECB\u7ECD', onClick: () => setFeatureIntroOpen(true) },
    ...(canEnterAdmin || canViewPeople ? [{ key: 'admin', icon: <SettingOutlined />, label: '\u7BA1\u7406\u540E\u53F0', onClick: () => navigate('/admin') }] : []),
    { type: 'divider' as const },
    { key: 'logout', icon: <LogoutOutlined />, label: '\u9000\u51FA\u767B\u5F55', onClick: handleLogout },
  ]

  const isActive = (item: NavItem) => {
    if (item.key === 'home') return location.pathname === '/'
    if (item.key === 'chat') return location.pathname.startsWith('/chat')
    if (item.path) return location.pathname.startsWith(item.path)
    return false
  }

  useEffect(() => {
    if (navRef.current) {
      entranceStagger(Array.from(navRef.current.querySelectorAll('.sidebar-nav-item')), { each: 40 })
    }
  }, [])

  useEffect(() => {
    const nav = navRef.current
    const ind = indicatorRef.current
    if (!nav || !ind) return
    const active = nav.querySelector('.sidebar-nav-item.active') as HTMLElement | null
    if (active) {
      ind.style.opacity = '1'
      slideIndicator(ind, { top: active.offsetTop, height: active.offsetHeight })
    } else {
      ind.style.opacity = '0'
    }
  }, [location.pathname])

  return (
    <div className={`app-layout${collapsed ? ' sidebar-collapsed' : ''}`}>
      <aside className="sidebar">
        <div className="sidebar-inner">
          <div
            className="sidebar-logo"
            onClick={toggleCollapsed}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <PlatformLogo width={collapsed ? 40 : 176} variant={collapsed ? 'mark' : 'lockup'} />
          </div>

          <nav className="sidebar-nav" ref={navRef}>
            <div className="sidebar-indicator" ref={indicatorRef} />
            {navItems.map((item) => (
              <Tooltip key={item.key} title={collapsed ? item.label : ''} placement="right">
                <button type="button"
                  className={`sidebar-nav-item ${isActive(item) ? 'active' : ''} ${item.disabled ? 'disabled' : ''}`}
                  onClick={() => handleNavClick(item)}
                  disabled={item.disabled}
                >
                  <span className="sidebar-nav-icon">{item.icon}</span>
                  <span className="sidebar-nav-label">{item.label}</span>
                </button>
              </Tooltip>
            ))}
          </nav>

          <div className="sidebar-bottom">
            <Dropdown menu={{ items: userMenuItems }} placement="topRight" trigger={['click']}>
              <div className="sidebar-user">
                <Tooltip title={collapsed ? (user?.username || '') : ''} placement="right">
                  <div className="sidebar-user-avatar">
                    {user?.avatar_url
                      ? <img src={user.avatar_url} alt={user.username} />
                      : <UserOutlined />}
                  </div>
                </Tooltip>
                <span className="sidebar-user-name">{user?.username}</span>
              </div>
            </Dropdown>
            <NotificationBell collapsed={collapsed} />
          </div>
        </div>
      </aside>

      <main className="main-content">
        {location.pathname.startsWith('/admin') ? <AdminWorkspace>{children}</AdminWorkspace> : children}
      </main>
      <FeatureIntro
        open={featureIntroOpen}
        onClose={() => setFeatureIntroOpen(false)}
        isAdmin={canEnterAdmin}
        onNavigate={navigate}
      />
    </div>
  )
}

export default Layout
