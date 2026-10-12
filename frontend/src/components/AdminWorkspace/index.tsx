import type { ReactNode } from 'react'
import { ConfigProvider } from 'antd'
import { AppstoreOutlined, ArrowLeftOutlined } from '@ant-design/icons'
import { Link, useLocation } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'
import { visibleAdminDestinations } from './navigation'
import './index.css'

export default function AdminWorkspace({ children }: { children: ReactNode }) {
  const location = useLocation()
  const user = useAuthStore((state) => state.user)
  const destinations = visibleAdminDestinations(user)
  const activePath = location.pathname.startsWith('/admin/skill-packages/') ? '/admin/skills' : location.pathname
  const active = destinations.find((item) => activePath === item.path || activePath.startsWith(item.path + '/'))
  return <ConfigProvider theme={{ token: { borderRadius: 8, controlHeight: 36, fontSize: 14 }, components: {
    Table: { cellPaddingBlock: 16, headerBorderRadius: 0 }, Tabs: { horizontalItemGutter: 28 },
  } }}>
    <div className="admin-workspace">
      <header className="workspace-bar">
        <div className="workspace-breadcrumb"><Link to="/admin"><AppstoreOutlined /> 管理后台</Link>
          {active && <><span aria-hidden="true">/</span><span>{active.label}</span></>}</div>
        <Link to="/" className="workspace-return"><ArrowLeftOutlined /> 返回工作空间</Link>
      </header>
      <nav className="workspace-navigation" aria-label="管理模块">
        <Link to="/admin" className={location.pathname === '/admin' ? 'is-active' : ''} aria-current={location.pathname === '/admin' ? 'page' : undefined}>概览</Link>
        {destinations.map((item) => <Link to={item.path} key={item.path} className={active?.path === item.path ? 'is-active' : ''}
          aria-current={active?.path === item.path ? 'page' : undefined}>{item.label}</Link>)}
      </nav>
      <div className="workspace-content">{children}</div>
    </div>
  </ConfigProvider>
}
