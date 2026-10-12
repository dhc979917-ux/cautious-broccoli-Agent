import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button, Drawer, Empty, Input, Segmented, Select, Space, Spin, Table, Tag, message,
} from 'antd'
import {
  DashboardOutlined, ArrowLeftOutlined, TeamOutlined, ReloadOutlined, SearchOutlined, MessageOutlined, ArrowRightOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'
import { hasPerm } from '../../constants/permissions'
import { ROLE_COLORS, ROLE_LABELS } from '../../constants/roles'
import { PRIMARY_ACTIVE, PRIMARY_COLOR, primaryAlpha } from '../../constants/theme'
import EChart from '../../components/EChart'
import {
  getOverview, getTrend, getDayConversations, getConversationDetail, getTasks, getUsersOverview,
  MonitoringOverview, TrendPoint, DayConversation, ConversationMessageDetail, TaskInfo, UserOverview,
} from '../../api/monitoring'
import './index.css'

function fmtTokens(n: number): string {
  if (!n) return '0'
  if (n >= 1000000) return `${(n / 1000000).toFixed(2)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

export default function Monitoring() {
  const navigate = useNavigate()
  const { userId: userIdParam } = useParams<{ userId?: string }>()
  const { user } = useAuthStore()
  // monitor_manage：可看全局/进入任意用户监控页（admin 天然拥有）
  const isAdmin = hasPerm(user, 'monitor_manage')
  // 无权限强制看自己；有权限不带参数看全局，带参数看指定用户
  const targetUserId = isAdmin && userIdParam ? parseInt(userIdParam, 10) : undefined
  const viewingSelf = !targetUserId

  const [overview, setOverview] = useState<MonitoringOverview | null>(null)
  const [loadingOverview, setLoadingOverview] = useState(false)

  // 趋势图
  const [days, setDays] = useState(30)
  const [groupBy, setGroupBy] = useState<'model' | 'agent'>('model')
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [loadingTrend, setLoadingTrend] = useState(false)

  // 日历热力图（固定近 90 天）
  const [calendarData, setCalendarData] = useState<[string, number][]>([])

  // 某日下钻
  const [drillDate, setDrillDate] = useState<string | null>(null)
  const [conversations, setConversations] = useState<DayConversation[]>([])
  const [loadingConv, setLoadingConv] = useState(false)

  // 会话明细抽屉
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailConv, setDetailConv] = useState<DayConversation | null>(null)
  const [detailRows, setDetailRows] = useState<ConversationMessageDetail[]>([])
  const [loadingDetail, setLoadingDetail] = useState(false)

  // 任务
  const [tasks, setTasks] = useState<TaskInfo[]>([])

  // admin 用户总览
 const [usersOv, setUsersOv] = useState<UserOverview[]>([])
  // 用户总览搜索/角色筛选（与人员信息页一致的交互模式）
  const [userSearch, setUserSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState<string | null>(null)

  const loadOverview = useCallback(async () => {
    setLoadingOverview(true)
    try {
      setOverview(await getOverview(targetUserId, days))
    } catch {
      /* 忽略 */
    } finally {
      setLoadingOverview(false)
    }
  }, [targetUserId, days])

  const loadTrend = useCallback(async () => {
    setLoadingTrend(true)
    try {
      const data = await getTrend(days, groupBy, targetUserId)
      setTrend(data)
      // 日历数据：按日期聚合 token（固定 90 天单独取一次成本高，直接用当前窗口数据）
      const byDate = new Map<string, number>()
      data.forEach((p) => byDate.set(p.date, (byDate.get(p.date) || 0) + p.tokens))
      setCalendarData(Array.from(byDate.entries()))
    } catch {
      message.error('加载趋势失败')
    } finally {
      setLoadingTrend(false)
    }
  }, [days, groupBy, targetUserId])

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await getTasks(targetUserId))
    } catch {
      /* 忽略 */
    }
  }, [targetUserId])

  const loadUsersOv = useCallback(async () => {
    if (!isAdmin) return
    try {
      setUsersOv(await getUsersOverview())
    } catch {
      /* 忽略 */
    }
  }, [isAdmin])

  useEffect(() => {
    loadOverview()
    loadTrend()
    loadTasks()
    loadUsersOv()
    setDrillDate(null)
    setConversations([])
  }, [loadOverview, loadTrend, loadTasks, loadUsersOv])

  // ===== 下钻某日 =====
  const drillInto = useCallback(async (date: string) => {
    setDrillDate(date)
    setLoadingConv(true)
    try {
      setConversations(await getDayConversations(date, targetUserId))
    } catch {
      message.error('加载会话列表失败')
    } finally {
      setLoadingConv(false)
    }
  }, [targetUserId])

  const openDetail = async (conv: DayConversation) => {
    setDetailConv(conv)
    setDetailOpen(true)
    setDetailRows([])
    setLoadingDetail(true)
    try {
      setDetailRows(await getConversationDetail(conv.session_id))
    } catch (e: any) {
      message.error(e.response?.data?.detail || '加载明细失败')
    } finally {
      setLoadingDetail(false)
    }
  }

  // ===== 柱状图配置（日期 × 模型/Agent 堆叠）=====
  const barOption = useMemo(() => {
    const dates = Array.from(new Set(trend.map((p) => p.date))).sort()
    const keys = Array.from(new Set(trend.map((p) => p.key)))
    const series = keys.map((k) => ({
      name: k,
      type: 'bar' as const,
      stack: 'total',
      emphasis: { focus: 'series' as const },
      data: dates.map((d) => {
        const hit = trend.find((p) => p.date === d && p.key === k)
        return hit ? hit.tokens : 0
      }),
    }))
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { top: 0, type: 'scroll', textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 16, top: 36, bottom: 30 },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', name: 'tokens', axisLabel: { fontSize: 10 } },
      series,
    }
  }, [trend])

  // ===== 日历热力图配置 =====
  const calendarOption = useMemo(() => {
    const end = new Date()
    const start = new Date(end.getTime() - 89 * 24 * 3600 * 1000)
    const fmt = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    return {
      backgroundColor: 'transparent',
      tooltip: {
        formatter: (p: any) => `${p.value[0]}<br/>tokens: ${fmtTokens(p.value[1])}`,
      },
      visualMap: {
        min: 0,
        max: Math.max(1000, ...calendarData.map((d) => d[1])),
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        itemWidth: 12,
        itemHeight: 120,
        text: ['多', '少'],
        textStyle: { fontSize: 10 },
        inRange: { color: [primaryAlpha(0.15), PRIMARY_COLOR, PRIMARY_ACTIVE] },
      },
      calendar: {
        top: 30,
        left: 40,
        right: 10,
        cellSize: ['auto', 13],
        range: [fmt(start), fmt(end)],
        itemStyle: { borderWidth: 2, borderColor: 'transparent' },
        splitLine: { show: false },
        dayLabel: { fontSize: 9, nameMap: 'ZH' },
        monthLabel: { fontSize: 9, nameMap: 'ZH' },
        yearLabel: { show: false },
      },
      series: [{
        type: 'heatmap',
        coordinateSystem: 'calendar',
        data: calendarData,
      }],
    }
 }, [calendarData])

  // 用户总览筛选：搜索用户名 + 角色过滤（复用人员信息页交互模式）
  const filteredUsers = useMemo(() => {
    return usersOv.filter((u) => {
      const matchSearch = !userSearch || u.username.toLowerCase().includes(userSearch.toLowerCase())
      const matchRole = !roleFilter || u.role === roleFilter
      return matchSearch && matchRole
    })
  }, [usersOv, userSearch, roleFilter])

 const convColumns = [
    {
      title: '会话', dataIndex: 'title', key: 'title', ellipsis: true,
      render: (v: string) => <a onClick={() => {}}>{v}</a>,
    },
    { title: 'Agent', dataIndex: 'agent_name', key: 'agent_name', width: 140, ellipsis: true },
    {
      title: '模型', dataIndex: 'models', key: 'models', width: 200,
      render: (v: string[]) => (v || []).map((m) => <Tag key={m} color="blue" style={{ marginBottom: 2 }}>{m}</Tag>),
    },
    { title: '消息', dataIndex: 'messages', key: 'messages', width: 70 },
    { title: 'tokens', dataIndex: 'tokens', key: 'tokens', width: 90, render: (v: number) => fmtTokens(v) },
    { title: '用时', dataIndex: 'elapsed_seconds', key: 'elapsed_seconds', width: 80, render: (v: number) => `${v}s` },
    {
      title: '时间', dataIndex: 'first_ts', key: 'first_ts', width: 150,
      render: (v?: string) => v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—',
    },
    {
      title: '', key: 'op', width: 70,
      render: (_: any, rec: DayConversation) => (
        <Button size="small" type="link" onClick={() => openDetail(rec)}>明细</Button>
      ),
    },
  ]

  const taskColumns = [
    { title: '会话', dataIndex: 'session_title', key: 'session_title', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (v: string) => <Tag color={v === 'running' ? 'processing' : v === 'done' ? 'success' : 'error'}>{v}</Tag>,
    },
    {
      title: '用时', dataIndex: 'elapsed_seconds', key: 'elapsed_seconds', width: 90,
      render: (v?: number) => v != null ? `${v}s` : '—',
    },
  ]

  const userColumns = [
    { title: '用户', dataIndex: 'username', key: 'username', width: 160, align: 'center' as const },
    {
      title: '角色', dataIndex: 'role', key: 'role', width: 110, align: 'center' as const,
      render: (v: string) => <Tag color={ROLE_COLORS[v]}>{ROLE_LABELS[v] || v}</Tag>,
    },
    { title: '会话数', dataIndex: 'sessions', key: 'sessions', width: 100, align: 'center' as const, sorter: (a: UserOverview, b: UserOverview) => a.sessions - b.sessions },
    { title: '消息数', dataIndex: 'messages', key: 'messages', width: 100, align: 'center' as const, sorter: (a: UserOverview, b: UserOverview) => a.messages - b.messages },
    {
      title: '总 tokens', dataIndex: 'total_tokens', key: 'total_tokens', width: 120, align: 'center' as const,
      sorter: (a: UserOverview, b: UserOverview) => a.total_tokens - b.total_tokens,
      render: (v: number) => fmtTokens(v),
    },
    {
      title: '本周 Token', dataIndex: 'week_tokens', key: 'week_tokens', width: 120, align: 'center' as const,
      sorter: (a: UserOverview, b: UserOverview) => a.week_tokens - b.week_tokens,
      render: (v: number) => fmtTokens(v || 0),
    },
    {
      title: '最近活跃', dataIndex: 'last_active', key: 'last_active', width: 190, align: 'center' as const,
      render: (v?: string) => v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—',
    },
    {
      title: '', key: 'op', width: 110, align: 'center' as const,
      render: (_: any, rec: UserOverview) => (
        <Button size="small" type="link" onClick={() => navigate(`/monitoring/user/${rec.user_id}`)}>
          查看监控
        </Button>
      ),
    },
  ]

  const statCards = overview ? [
    { label: '会话数', value: String(overview.sessions) },
    { label: '消息数', value: String(overview.messages) },
    { label: '总 tokens', value: fmtTokens(overview.total_tokens) },
    { label: '工具调用', value: String(overview.tool_calls) },
    { label: '平均用时', value: `${overview.avg_elapsed_seconds}s` },
    { label: '运行中任务', value: String(overview.running_tasks) },
    { label: '异常中断', value: String(overview.errors) },
    // 质量闭环：构建型回复有多少真的走了"先问再做 / 做完再看"
    ...(overview.quality
      ? [
          { label: '构建型回复', value: String(overview.quality.artifact_replies) },
          { label: '需求采集触发率', value: `${overview.quality.intake_rate}%` },
          { label: '采集默认值占比', value: `${overview.quality.assume_rate}%` },
          { label: '交付前验证率', value: `${overview.quality.verify_rate}%` },
          { label: '验证发现错误/次', value: String(overview.quality.avg_verify_errors) },
        ]
      : []),
  ] : []

  return (
    <div className="monitoring-page">
      <div className="monitoring-header">
        <div className="monitoring-title">
          <DashboardOutlined />
          {viewingSelf ? '监控与运维' : `监控 · 用户 #${targetUserId}`}
        </div>
        <Space>
          {isAdmin && !viewingSelf && (
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/monitoring')}>返回全局视图</Button>
          )}
          <Button icon={<ReloadOutlined />} onClick={() => { loadOverview(); loadTrend(); loadTasks(); loadUsersOv() }}>
            刷新
          </Button>
        </Space>
      </div>

      {/* admin 用户总览入口（仅全局视图显示） */}
      {isAdmin && viewingSelf && (
        <div className="monitoring-section">
          <div className="monitoring-section-head">
            <span className="monitoring-section-title"><TeamOutlined /> 用户总览（点击查看指定用户的监控）</span>
            <div className="monitoring-user-toolbar">
              <Input
                allowClear
                prefix={<SearchOutlined />}
                placeholder="搜索用户名"
                value={userSearch}
                onChange={(e) => setUserSearch(e.target.value)}
                style={{ width: 180 }}
              />
              <Segmented
                value={roleFilter || 'all'}
                onChange={(v) => setRoleFilter(v === 'all' ? null : String(v))}
                options={[
                  { label: '全部', value: 'all' },
                  { label: '管理员', value: 'admin' },
                  { label: '操作员', value: 'operator' },
                  { label: '观察者', value: 'viewer' },
                ]}
              />
              <span className="monitoring-user-count">共 {filteredUsers.length} 人</span>
            </div>
          </div>
          <Table
            rowKey="user_id"
            size="small"
            tableLayout="fixed"
            columns={userColumns as any}
            dataSource={filteredUsers}
            pagination={{ pageSize: 10, showSizeChanger: false }}
          />
        </div>
      )}

      {/* 概览卡片 */}
      <Spin spinning={loadingOverview}>
        <div className="monitoring-cards">
          {statCards.map((c) => (
            <div key={c.label} className="monitoring-card stagger-item">
              <div className="monitoring-card-value">{c.value}</div>
              <div className="monitoring-card-label">{c.label}</div>
            </div>
          ))}
        </div>
      </Spin>

      {/* 趋势图 */}
      <div className="monitoring-section">
        <div className="monitoring-section-head">
          <span className="monitoring-section-title">Token 消耗趋势（点击柱子下钻当日会话）</span>
          <Space>
            <Segmented
              size="small"
              value={groupBy}
              onChange={(v) => setGroupBy(v as 'model' | 'agent')}
              options={[{ label: '按模型', value: 'model' }, { label: '按 Agent', value: 'agent' }]}
            />
            <Select
              size="small"
              value={days}
              onChange={(v) => setDays(v)}
              style={{ width: 100 }}
              options={[
                { label: '近 7 天', value: 7 },
                { label: '近 30 天', value: 30 },
                { label: '近 90 天', value: 90 },
              ]}
            />
          </Space>
        </div>
        <Spin spinning={loadingTrend}>
          {trend.length === 0 ? (
            <Empty description="该时间范围内暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <EChart
              option={barOption}
              height={300}
              onEvents={{
                click: (params: any) => {
                  if (params?.name) drillInto(params.name)
                },
              }}
            />
          )}
        </Spin>
      </div>

      {/* 日历热力图 */}
      <div className="monitoring-section">
        <div className="monitoring-section-title">活跃日历（近 90 天，颜色越深 token 越多）</div>
        {calendarData.length === 0 ? (
          <Empty description="暂无数据" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <EChart
            option={calendarOption}
            height={200}
            onEvents={{
              click: (params: any) => {
                const d = params?.value?.[0]
                if (d) drillInto(d)
              },
            }}
          />
        )}
      </div>

      {/* 当日会话下钻 */}
      {drillDate && (
        <div className="monitoring-section">
          <div className="monitoring-section-head">
            <span className="monitoring-section-title">{drillDate} 的会话</span>
            <Button size="small" onClick={() => { setDrillDate(null); setConversations([]) }}>收起</Button>
          </div>
          <Table
            rowKey="session_id"
            size="small"
            loading={loadingConv}
            columns={convColumns as any}
            dataSource={conversations}
            pagination={false}
            onRow={(rec) => ({ onClick: () => openDetail(rec), style: { cursor: 'pointer' } })}
            locale={{ emptyText: '当日无会话' }}
          />
        </div>
      )}

      {/* 查看聊天记录功能模块（仅查看指定用户时显示） */}
      {!viewingSelf && (
        <div
          className="monitoring-chat-entry"
          onClick={() => navigate(`/monitoring/user/${targetUserId}/chat`)}
        >
          <div className="monitoring-chat-entry-icon"><MessageOutlined /></div>
          <div className="monitoring-chat-entry-body">
            <div className="monitoring-chat-entry-title">查看聊天记录</div>
            <div className="monitoring-chat-entry-desc">进入该用户的完整聊天界面，查看所有对话历史</div>
          </div>
          <div className="monitoring-chat-entry-stats">
            {tasks.filter((t) => t.status === 'running').length > 0 && (
              <Tag color="processing">{tasks.filter((t) => t.status === 'running').length} 个任务运行中</Tag>
            )}
            <span>共 {overview?.sessions ?? 0} 个会话</span>
          </div>
          <ArrowRightOutlined className="monitoring-chat-entry-arrow" />
        </div>
      )}
      {/* 后台任务（仅全局视图显示） */}
      {viewingSelf && (
        <div className="monitoring-section">
          <div className="monitoring-section-title">后台任务（运行中 / 近期完成）</div>
          <Table
            rowKey={(r) => `${r.session_id}-${r.started_at}`}
            size="small"
            columns={taskColumns as any}
            dataSource={tasks}
            pagination={false}
            locale={{ emptyText: '暂无任务记录' }}
          />
        </div>
      )}

      {/* 会话明细抽屉 */}
      <Drawer
        title={`会话明细 · ${detailConv?.title || ''}`}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={560}
      >
        {loadingDetail && <div className="monitoring-loading">加载中…</div>}
        {!loadingDetail && detailRows.length === 0 && (
          <Empty description="无消息记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
        <div className="monitoring-detail">
          {detailRows.map((m) => (
            <div key={m.id} className={`monitoring-detail-row ${m.role}`}>
              <div className="monitoring-detail-meta">
                <Tag color={m.role === 'user' ? 'default' : 'blue'}>{m.role === 'user' ? '用户' : 'Agent'}</Tag>
                {m.model && <Tag color="purple">{m.model}</Tag>}
                <span>{m.ts ? new Date(m.ts).toLocaleString('zh-CN', { hour12: false }) : ''}</span>
                {m.tokens > 0 && <span>· {fmtTokens(m.tokens)} tokens</span>}
                {m.elapsed_seconds != null && <span>· {m.elapsed_seconds}s</span>}
              </div>
              <div className="monitoring-detail-preview">{m.preview}</div>
            </div>
          ))}
        </div>
      </Drawer>
    </div>
  )
}
