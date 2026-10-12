import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Alert, Button, Result, Skeleton } from 'antd'
import { ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons'
import { useAuthStore } from '../../store/authStore'
import { hasAnyPerm, hasPerm } from '../../constants/permissions'
import { dashboard, type Dashboard } from '../../api/operations'
import { readableError } from '../../api/governance'
import { visibleAdminDestinations } from '../../components/AdminWorkspace/navigation'
import './index.css'

export default function AdminHome() {
  const user = useAuthStore((state) => state.user)
  const destinations = visibleAdminDestinations(user)
  const [metrics, setMetrics] = useState<Dashboard | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const load = async () => {
    if (!hasPerm(user, 'ops_manage.view')) return
    setLoading(true); setError('')
    try { setMetrics(await dashboard()) } catch (cause) { setError(readableError(cause)) } finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [user?.id])
  if (user?.role !== 'admin' && !hasAnyPerm(user)) return <Result status="403" title="无权访问管理后台" />
  const pending = Object.values(metrics?.pending || {}).reduce((sum, value) => sum + value, 0)
  return <div className="admin-overview">
    <header className="overview-heading"><div><h1>管理后台</h1><p>管理人员与 AI 资源，让每一项配置清晰可控。</p></div>
      {hasPerm(user, 'ops_manage.view') && <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新概览</Button>}</header>
    {error && <Alert type="warning" showIcon message="概览暂时无法加载" description={error} />}
    <div className="overview-grid"><section className="overview-destinations" aria-label="管理入口">
      {[...new Set(destinations.map((item) => item.group))].map((group) => <section className="overview-group" key={group} aria-label={group}>
        <h2>{group}</h2><div className="overview-module-list">{destinations.filter((item) => item.group === group).map((item) => <Link key={item.path} to={item.path} className="overview-module">
          <span className="overview-module-icon"><item.icon /></span><span><strong>{item.label}</strong><small>{item.description}</small></span><ArrowRightOutlined />
        </Link>)}</div></section>)}
      {!destinations.length && <Result status="403" title="暂无可管理的模块" />}
    </section><aside className="overview-aside">
      <section className="overview-queue"><div className="overview-section-heading"><h2>待我处理</h2><span>{metrics ? pending : '—'}</span></div>
        <p>{metrics ? pending > 0 ? '有待审核与待处理事项，请进入运营管理查看。' : '当前可见范围暂无待处理事项。' : '待处理事项将在概览加载后显示。'}</p>
        {hasPerm(user, 'ops_manage.view') && <Link to="/admin/operations?tab=pending">进入待处理 <ArrowRightOutlined /></Link>}</section>
      <section className="overview-activity"><h2>运行概况</h2>{loading && !metrics ? <Skeleton active paragraph={{ rows: 3 }} /> : <dl>
        <div><dt>今日 Agent 回复</dt><dd>{metrics?.usage.agent_replies ?? '—'}</dd></div>
        <div><dt>模型调用</dt><dd>{metrics?.usage.model_calls ?? '—'}</dd></div>
        <div><dt>Token 用量</dt><dd>{metrics ? (metrics.usage.tokens.input + metrics.usage.tokens.output).toLocaleString() : '—'}</dd></div>
        <div><dt>阈值告警</dt><dd>{metrics ? metrics.alerts.filter((item) => item.active).length : '—'}</dd></div></dl>}
        {hasPerm(user, 'ops_manage.view') && <Link to="/admin/operations">查看完整统计 <ArrowRightOutlined /></Link>}
        {metrics && <small className="overview-updated">北京时间 · 更新 {new Date(metrics.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</small>}</section>
      {hasPerm(user, 'ops_manage.audit') && <section className="overview-audit"><h2>最近管理操作</h2>
        {metrics?.recent.length ? <ul>{metrics.recent.slice(0, 4).map((item, index) => <li key={index}><span>{item.action}</span><small>{item.result}</small></li>)}</ul> : <p>当前范围暂无操作记录。</p>}
        <Link to="/admin/audit">查看管理审计 <ArrowRightOutlined /></Link></section>}
    </aside></div>
  </div>
}
