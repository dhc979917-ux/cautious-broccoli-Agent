import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button, Checkbox, Collapse, DatePicker, Drawer, Empty, Input, InputNumber,
  message, Modal, Popconfirm, Segmented, Select, Space, Spin, Switch, Table,
  Tag, TimePicker, Tooltip,
} from 'antd'
import {
  ScheduleOutlined, PlusOutlined, ReloadOutlined, PlayCircleOutlined,
  EditOutlined, HistoryOutlined, DeleteOutlined, UndoOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import dayjs, { Dayjs } from 'dayjs'
import { useAuthStore } from '../../store/authStore'
import { useModelDirectory } from '../../store/modelDirectory'
import request from '../../api/request'
import {
  listJobs, getJobStats, previewSchedule, createJob, updateJob, toggleJob,
  deleteJob, hardDeleteJob, restoreJob, runJobNow, listRuns,
  ScheduledJob, JobRun, JobStats, JobPayload, TriggerType,
} from '../../api/scheduledTasks'
import { getAllUsers } from '../../api/admin'
import { hasPerm } from '../../constants/permissions'
import SessionPreviewModal from '../../components/SessionPreview'
import './index.css'

// ---------------- 常量 ----------------

const STATUS_META: Record<string, { color: string; text: string }> = {
  running: { color: 'processing', text: '运行中' },
  success: { color: 'success', text: '成功' },
  failed: { color: 'error', text: '失败' },
  skipped: { color: 'warning', text: '已跳过' },
  timeout: { color: 'purple', text: '超时' },
}

const TRIGGER_TEXT: Record<string, string> = {
  cron: '定时', once: '一次性', manual: '手动', retry: '重试',
}

const WEEKDAY_OPTS = [
  { label: '周一', value: 1 }, { label: '周二', value: 2 }, { label: '周三', value: 3 },
  { label: '周四', value: 4 }, { label: '周五', value: 5 }, { label: '周六', value: 6 },
  { label: '周日', value: 0 },
]

type FreqKey = 'daily' | 'weekly' | 'workday' | 'monthly' | 'once'

const FREQ_OPTS = [
  { label: '每天', value: 'daily' },
  { label: '每周', value: 'weekly' },
  { label: '工作日', value: 'workday' },
  { label: '每月', value: 'monthly' },
  { label: '仅一次', value: 'once' },
]

interface SimpleSchedule {
  freq: FreqKey
  time: Dayjs
  weekdays: number[]
  monthDay: number
  onceAt: Dayjs
}

const DEFAULT_SCHEDULE: SimpleSchedule = {
  freq: 'daily',
  time: dayjs().hour(8).minute(0).second(0),
  weekdays: [1, 2, 3, 4, 5],
  monthDay: 1,
  onceAt: dayjs().add(1, 'hour'),
}

interface SkillOption {
  source_group?: 'system' | 'mine' | 'shared'; name: string; display_name?: string; display_name_zh?: string }
interface McpOption { id: number; name: string }

// 描述是表格里的一行摘要，上限与后端 schemas.scheduled_job.DESCRIPTION_MAX_LEN 一致
const DESCRIPTION_MAX_LEN = 20

// 表格最小宽度：宽屏时各列按百分比自适应撑满容器，窄屏收缩到该宽度后横向滚动，
// 避免"任务"列被挤到不可读（列宽全部用百分比，不写死像素）
const TABLE_MIN_WIDTH = 900

// ---------------- 工具 ----------------

function fmtTime(s: string | null): string {
  if (!s) return '-'
  const d = dayjs(s)
  return d.isValid() ? d.format('MM-DD HH:mm') : s
}

function relTime(s: string | null): string {
  if (!s) return '-'
  const diff = dayjs(s).diff(dayjs(), 'minute')
  if (diff < 1) return '即将'
  if (diff < 60) return `${diff} 分钟后`
  if (diff < 60 * 24) return `${Math.floor(diff / 60)} 小时后`
  return dayjs(s).format('MM-DD HH:mm')
}

function durationSec(run: JobRun): string {
  if (!run.started_at || !run.finished_at) return '-'
  const sec = Math.max(0, dayjs(run.finished_at).diff(dayjs(run.started_at), 'second'))
  return sec >= 60 ? `${Math.floor(sec / 60)}分${sec % 60}秒` : `${sec}秒`
}

function buildTrigger(s: SimpleSchedule): {
  trigger_type: TriggerType; cron_expr: string | null; run_at: string | null
} {
  const mm = s.time.minute()
  const hh = s.time.hour()
  if (s.freq === 'daily') return { trigger_type: 'cron', cron_expr: `${mm} ${hh} * * *`, run_at: null }
  if (s.freq === 'weekly') {
    const dow = [...s.weekdays].sort((a, b) => a - b).join(',')
    return { trigger_type: 'cron', cron_expr: `${mm} ${hh} * * ${dow}`, run_at: null }
  }
  if (s.freq === 'workday') return { trigger_type: 'cron', cron_expr: `${mm} ${hh} * * 1-5`, run_at: null }
  if (s.freq === 'monthly') return { trigger_type: 'cron', cron_expr: `${mm} ${hh} ${s.monthDay} * *`, run_at: null }
  return { trigger_type: 'once', cron_expr: null, run_at: s.onceAt.format('YYYY-MM-DDTHH:mm:ss') }
}

// 已有任务的触发配置 → 简单模式表单状态（无法解析时回退默认）
function jobToSchedule(job: ScheduledJob): SimpleSchedule {
  if (job.trigger_type === 'once') {
    return { ...DEFAULT_SCHEDULE, freq: 'once', onceAt: job.run_at ? dayjs(job.run_at) : dayjs().add(1, 'hour') }
  }
  const expr = (job.cron_expr || '').trim()
  const parts = expr.split(/\s+/)
  if (parts.length === 5) {
    const [m, h, dom, , dow] = parts
    if (/^\d+$/.test(m) && /^\d+$/.test(h)) {
      const time = dayjs().hour(parseInt(h, 10)).minute(parseInt(m, 10)).second(0)
      if (dom === '*' && dow === '*') return { ...DEFAULT_SCHEDULE, freq: 'daily', time }
      if (dom === '*' && dow === '1-5') return { ...DEFAULT_SCHEDULE, freq: 'workday', time }
      if (dom === '*' && /^\d+(,\d+)*$/.test(dow)) {
        return { ...DEFAULT_SCHEDULE, freq: 'weekly', time, weekdays: dow.split(',').map(Number) }
      }
      if (/^\d+$/.test(dom) && dow === '*') {
        return { ...DEFAULT_SCHEDULE, freq: 'monthly', time, monthDay: parseInt(dom, 10) }
      }
    }
  }
  return { ...DEFAULT_SCHEDULE }
}

function validateSchedule(s: SimpleSchedule, previewError: string): string | null {
  if (s.freq === 'weekly' && s.weekdays.length === 0) return '请至少选择一个星期'
  if (s.freq === 'once' && s.onceAt.isBefore(dayjs())) return '一次性任务的执行时间必须是未来时间'
  if (previewError) return previewError
  return null
}

// 触发配置预览（防抖）
function useSchedulePreview(trigger: {
  trigger_type: TriggerType; cron_expr: string | null; run_at: string | null
}, active: boolean) {
  const [desc, setDesc] = useState('')
  const [runs, setRuns] = useState<string[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    if (!active) return
    if (trigger.trigger_type === 'once') {
      setDesc(trigger.run_at ? `仅执行一次：${dayjs(trigger.run_at).format('YYYY-MM-DD HH:mm')}` : '')
      setRuns([])
      setError('')
      return
    }
    const id = setTimeout(() => {
      previewSchedule({ trigger_type: 'cron', cron_expr: trigger.cron_expr, timezone: 'Asia/Shanghai' })
        .then((r) => { setDesc(r.description); setRuns(r.next_runs); setError('') })
        .catch((e) => {
          setError(e?.response?.data?.detail || '触发配置不合法')
          setDesc(''); setRuns([])
        })
    }, 500)
    return () => clearTimeout(id)
  }, [trigger.trigger_type, trigger.cron_expr, trigger.run_at, active])
  return { desc, runs, error }
}

// 简单模式时间选择器（新建抽屉与选择弹窗共用）
const SchedulePicker: React.FC<{ value: SimpleSchedule; onChange: (v: SimpleSchedule) => void }> = ({ value, onChange }) => (
  <div className="sched-simple">
    <Select value={value.freq} onChange={(v) => onChange({ ...value, freq: v })} options={FREQ_OPTS} style={{ width: 110 }} />
    {value.freq !== 'once' && (
      <TimePicker value={value.time} onChange={(v) => v && onChange({ ...value, time: v })} format="HH:mm" allowClear={false} />
    )}
    {value.freq === 'weekly' && (
      <Checkbox.Group value={value.weekdays} onChange={(v) => onChange({ ...value, weekdays: v as number[] })} options={WEEKDAY_OPTS} />
    )}
    {value.freq === 'monthly' && (
      <InputNumber min={1} max={31} value={value.monthDay} onChange={(v) => onChange({ ...value, monthDay: v || 1 })} addonAfter="日" />
    )}
    {value.freq === 'once' && (
      <DatePicker
        showTime value={value.onceAt} onChange={(v) => v && onChange({ ...value, onceAt: v })}
        format="YYYY-MM-DD HH:mm" allowClear={false}
      />
    )}
  </div>
)

const PreviewLine: React.FC<{ desc: string; runs: string[]; error: string }> = ({ desc, runs, error }) => {
  if (error) return <div className="sched-preview-err">{error}</div>
  if (!desc) return null
  return (
    <div className="sched-preview">
      <span>{desc}</span>
      {runs.length > 0 && <span className="sched-muted"> 未来：{runs.join(' / ')}</span>}
    </div>
  )
}

// ---------------- 组件 ----------------

export default function ScheduledTasks() {
  const navigate = useNavigate()
  const { user } = useAuthStore()
  const isAdmin = user?.role === 'admin'
  const canViewAll = hasPerm(user, 'schedule_manage.view_all')

  const [jobs, setJobs] = useState<ScheduledJob[]>([])
  // 管理员"全部"分组的数据源：含他人的个人任务/实例（后端 scope=all 才返回）
  const [allJobs, setAllJobs] = useState<ScheduledJob[]>([])
  const [deletedJobs, setDeletedJobs] = useState<ScheduledJob[]>([])
  const [stats, setStats] = useState<JobStats | null>(null)
  const [loading, setLoading] = useState(true)
  // 公共/我的/回收站三个分组口径对所有人一致：公共任务 + 本人任务/实例；
  // "全部"仅管理员可见，是额外拉的一份全量数据，不影响其余分组的口径
  const [filter, setFilter] = useState<'all' | 'public' | 'mine' | 'deleted'>('public')
  const [search, setSearch] = useState('')

  // 管理员表单选项
  const { models, modelLabel } = useModelDirectory()
  const [skills, setSkills] = useState<SkillOption[]>([])
  const [mcpServers, setMcpServers] = useState<McpOption[]>([])

  // 管理员新建/编辑抽屉
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [prompt, setPrompt] = useState('')
  const [isPublic, setIsPublic] = useState(true)
  const [sched, setSched] = useState<SimpleSchedule>({ ...DEFAULT_SCHEDULE })
  const [model, setModel] = useState<string | undefined>(undefined)
  const [selSkills, setSelSkills] = useState<string[]>([])
  const [refreshSkillVersions, setRefreshSkillVersions] = useState(false)
  const [originalSkills, setOriginalSkills] = useState<string[]>([])
  const [boundSkillVersions, setBoundSkillVersions] = useState<Record<string, string>>({})
  const [selMcps, setSelMcps] = useState<number[]>([])

  // 用户选择任务 / 修改执行时间弹窗
  const [pickJob, setPickJob] = useState<ScheduledJob | null>(null)
  const [pickMode, setPickMode] = useState<'select' | 'editTime'>('select')
  const [pickSched, setPickSched] = useState<SimpleSchedule>({ ...DEFAULT_SCHEDULE })
  const [pickSaving, setPickSaving] = useState(false)

  // 执行历史抽屉
  const [historyJob, setHistoryJob] = useState<ScheduledJob | null>(null)
  const [runs, setRuns] = useState<JobRun[]>([])
  const [runsTotal, setRunsTotal] = useState(0)
  const [runsPage, setRunsPage] = useState(1)
  const [loadingRuns, setLoadingRuns] = useState(false)
  // 公共任务执行历史的人员过滤（仅管理员）：null=全部人员，聚合派生的所有个人实例
  const [runsUser, setRunsUser] = useState<number | null>(null)
  // 他人会话的只读预览：管理员在公共任务聚合视图里点别人的"查看会话"时用浮窗，
  // 直接跳转只会落到空白聊天页（会话按属主隔离）
  const [previewSessionId, setPreviewSessionId] = useState<number | null>(null)
  // 人员下拉选项（与文件中心同一数据源，仅管理员需要）
  const [users, setUsers] = useState<{ id: number; username: string }[]>([])

  const [runningIds, setRunningIds] = useState<number[]>([])

  // ---------------- 加载 ----------------

  const reload = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const [js, st] = await Promise.all([listJobs(), getJobStats()])
      setJobs(js)
      setStats(st)
      // 回收站：管理员看全量，普通用户后端只返回本人任务/实例
      listJobs('deleted').then(setDeletedJobs).catch(() => {})
      // "全部"分组（仅管理员）：与回收站同样的旁路加载，失败不影响主列表；
      // 普通用户不发这个请求（后端对非管理员返回 403）
      if (hasPerm(user, 'schedule_manage.view_all')) {
        listJobs('all').then(setAllJobs).catch(() => setAllJobs([]))
      }
    } catch {
      /* 忽略：下一次轮询自动恢复 */
    } finally {
      if (!silent) setLoading(false)
    }
  }, [canViewAll])

  useEffect(() => { reload() }, [reload])

  useEffect(() => {
    if (!hasPerm(user, 'schedule_manage.create') && !hasPerm(user, 'schedule_manage.view_all')) return
    request.get('/skills/').then((r) => setSkills((r.data || []).filter((s: any) => s.has_access))).catch(() => {})
    request.get('/mcp/servers').then((r) => setMcpServers(r.data || [])).catch(() => {})
    getAllUsers().then(setUsers).catch(() => { /* 静默 */ })
  }, [canViewAll])

  // 页面可见时 30s 轮询刷新状态
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') reload(true)
    }, 30000)
    return () => clearInterval(id)
  }, [reload])

  // ---------------- 预览 ----------------

  const drawerTrigger = useMemo(() => buildTrigger(sched), [sched])
  const drawerPreview = useSchedulePreview(drawerTrigger, drawerOpen)
  const pickTrigger = useMemo(() => buildTrigger(pickSched), [pickSched])
  const pickPreview = useSchedulePreview(pickTrigger, !!pickJob)

  // ---------------- 管理员表单 ----------------

  const resetForm = () => {
    setName(''); setDescription(''); setPrompt('')
    setIsPublic(true)
    setSched({ ...DEFAULT_SCHEDULE, time: dayjs().hour(8).minute(0).second(0) })
    setModel(undefined); setSelSkills([]); setSelMcps([])
  }

  const openCreate = () => {
    setEditingId(null)
    setRefreshSkillVersions(false); setOriginalSkills([]); setBoundSkillVersions({})
    resetForm()
    setDrawerOpen(true)
  }

  const openEdit = (job: ScheduledJob) => {
    setEditingId(job.id)
    setRefreshSkillVersions(false); setOriginalSkills(job.selected_skills || []); setBoundSkillVersions(job.skill_versions || {})
    setName(job.name)
    // 历史任务可能存过更长的描述：编辑时裁到上限，避免保存被后端校验挡下
    setDescription((job.description || '').slice(0, DESCRIPTION_MAX_LEN))
    setPrompt(job.prompt)
    setIsPublic(job.is_public)
    setSched(jobToSchedule(job))
    setModel(job.model || undefined)
    setSelSkills(job.selected_skills || [])
    setSelMcps(job.selected_mcps || [])
    setDrawerOpen(true)
  }

  const saveJob = async () => {
    if (!name.trim()) { message.warning('请填写任务名称'); return }
    if (!prompt.trim()) { message.warning('请填写任务内容'); return }
    const err = validateSchedule(sched, drawerPreview.error)
    if (err) { message.warning(err); return }
    const payload: JobPayload = {
      name: name.trim(),
      description: description.trim() || null,
      prompt,
      ...buildTrigger(sched),
      timezone: 'Asia/Shanghai',
      is_public: isPublic,
      model: model || null,
      // 不选 = 空数组 = 默认全部不可用（选了才生效）；null 才是旧的"全部可用"语义
      selected_skills: selSkills,
      selected_mcps: selMcps,
    }
    if (editingId && !refreshSkillVersions && JSON.stringify([...selSkills].sort()) === JSON.stringify([...originalSkills].sort())) delete payload.selected_skills
    setSaving(true)
    try {
      if (editingId) {
        await updateJob(editingId, payload)
        message.success('任务已更新')
      } else {
        await createJob(payload)
        message.success('任务已创建')
      }
      setDrawerOpen(false)
      reload()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // ---------------- 用户选择/改时间 ----------------

  const openSelect = (job: ScheduledJob) => {
    setPickMode('select')
    setPickSched(jobToSchedule(job))
    setPickJob(job)
  }

  const openEditTime = (job: ScheduledJob) => {
    setPickMode('editTime')
    setPickSched(jobToSchedule(job))
    setPickJob(job)
  }

  const confirmPick = async () => {
    if (!pickJob) return
    const err = validateSchedule(pickSched, pickPreview.error)
    if (err) { message.warning(err); return }
    const trig = { ...buildTrigger(pickSched), timezone: 'Asia/Shanghai' }
    setPickSaving(true)
    try {
      if (pickMode === 'select') {
        await createJob({ parent_id: pickJob.id, ...trig })
        message.success('已选择任务，将按你设定的时间执行')
      } else {
        await updateJob(pickJob.id, trig)
        message.success('执行时间已更新')
      }
      setPickJob(null)
      reload()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '操作失败')
    } finally {
      setPickSaving(false)
    }
  }

  // ---------------- 表格操作 ----------------

  const canWrite = (j: ScheduledJob) => hasPerm(user, 'schedule_manage.update') || j.user_id === user?.id

  const onToggle = async (job: ScheduledJob, checked: boolean) => {
    try {
      await toggleJob(job.id)
      message.success(checked ? '任务已启用' : '任务已停用')
      reload(true)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '操作失败')
    }
  }

  const onRunNow = async (job: ScheduledJob) => {
    setRunningIds((ids) => [...ids, job.id])
    try {
      await runJobNow(job.id)
      message.success('已触发手动执行，可在执行历史查看进度')
      reload(true)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '触发失败')
    } finally {
      setRunningIds((ids) => ids.filter((i) => i !== job.id))
    }
  }

  const onDelete = async (job: ScheduledJob) => {
    try {
      const r = await deleteJob(job.id)
      message.success(r.message || '任务已删除')
      reload()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '删除失败')
    }
  }

  const onHardDelete = async (job: ScheduledJob) => {
    try {
      await hardDeleteJob(job.id)
      message.success('任务已彻底删除')
      reload()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '删除失败')
    }
  }

  const onRestore = async (job: ScheduledJob) => {
    try {
      await restoreJob(job.id)
      message.success('任务已恢复')
      reload()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '恢复失败')
    }
  }

  // ---------------- 执行历史 ----------------

  // overrideUser：切换人员过滤的同一次调用里就用新值，避免读到还没生效的 state
  const loadRuns = useCallback(async (
    job: ScheduledJob, page: number, overrideUser?: number | null,
  ) => {
    setLoadingRuns(true)
    try {
      // 人员过滤只对管理员自己的公共任务有意义，其余情况不传，后端也只在该场景生效
      const picked = overrideUser !== undefined ? overrideUser : runsUser
      const byUser = job.is_public && job.user_id === user?.id ? picked ?? undefined : undefined
      const r = await listRuns(job.id, 20, (page - 1) * 20, byUser)
      setRuns(r.items)
      setRunsTotal(r.total)
    } catch {
      /* 忽略 */
    } finally {
      setLoadingRuns(false)
    }
  }, [runsUser, user?.id])

  const openHistory = (job: ScheduledJob) => {
    setHistoryJob(job)
    setRunsPage(1)
    setRunsUser(null)
    loadRuns(job, 1, null)
  }

  // 查看会话：本人会话跳转到对应 Agent 的聊天页；他人会话改用只读预览浮窗
  const openRunSession = (run: JobRun) => {
    if (!run.session_id || !historyJob) return
    if (run.owner_id && run.owner_id !== user?.id) {
      setPreviewSessionId(run.session_id)
      return
    }
    navigate(`/chat/${run.session_id}?agent=${historyJob.agent_id}`)
  }

  useEffect(() => {
    if (!historyJob) return
    const hasRunning = runs.some((r) => r.status === 'running')
    if (!hasRunning) return
    const id = setInterval(() => loadRuns(historyJob, runsPage), 10000)
    return () => clearInterval(id)
  }, [historyJob, runs, runsPage, loadRuns])

  // ---------------- 过滤与列 ----------------

  const visibleJobs = useMemo(() => {
    let list = filter === 'deleted' ? deletedJobs : filter === 'all' ? allJobs : jobs
    if (filter === 'public') list = list.filter((j) => j.is_public)
    // "我的"= 本人名下的非公共任务（用户实例与管理员个人任务都算属主自己）
    if (filter === 'mine') list = list.filter((j) => !j.is_public && j.user_id === user?.id)
    if (search) {
      const kw = search.toLowerCase()
      list = list.filter((j) =>
        j.name.toLowerCase().includes(kw)
        || j.prompt.toLowerCase().includes(kw)
        || (j.description || '').toLowerCase().includes(kw))
    }
    return list
  }, [jobs, allJobs, deletedJobs, filter, search, user?.id])

  const renderActions = (j: ScheduledJob) => {
    if (filter === 'deleted') {
      return (
        <Space size={4}>
          <Tooltip title="恢复">
            <Button size="small" type="text" icon={<UndoOutlined />} onClick={() => onRestore(j)} />
          </Tooltip>
          <Popconfirm
            title="彻底删除该任务？"
            description="不可恢复；用户已选择的个人实例将一并删除。"
            onConfirm={() => onHardDelete(j)} okText="彻底删除" cancelText="取消"
          >
            <Tooltip title="彻底删除">
              <Button size="small" type="text" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      )
    }
    if (!hasPerm(user, 'schedule_manage.update') && j.is_public) {
      return (
        <Button size="small" type="link" onClick={() => openSelect(j)}>选择</Button>
      )
    }
    return (
      <Space size={4}>
        <Tooltip title="立即运行">
          <Button size="small" type="text" icon={<PlayCircleOutlined />}
            loading={runningIds.includes(j.id)} onClick={() => onRunNow(j)} />
        </Tooltip>
        <Tooltip title={hasPerm(user, 'schedule_manage.update') ? '编辑' : '修改执行时间'}>
          <Button size="small" type="text" icon={<EditOutlined />}
            onClick={() => (hasPerm(user, 'schedule_manage.update') ? openEdit(j) : openEditTime(j))} />
        </Tooltip>
        {/* 执行历史只看自己的：他人创建的公共任务不给入口（后端同样按属主放行） */}
        {j.user_id === user?.id && (
          <Tooltip title="执行历史">
            <Button size="small" type="text" icon={<HistoryOutlined />} onClick={() => openHistory(j)} />
          </Tooltip>
        )}
        <Popconfirm
          title="移入回收站？"
          description={j.is_public
            ? '可在回收站恢复；用户已选择的个人实例不受影响。'
            : '可在回收站恢复；彻底删除后执行历史将一并清除。'}
          onConfirm={() => onDelete(j)} okText="移入回收站" cancelText="取消"
        >
          <Tooltip title="移入回收站">
            <Button size="small" type="text" danger icon={<DeleteOutlined />} />
          </Tooltip>
        </Popconfirm>
      </Space>
    )
  }

  const columns = [
    {
      title: '序号', key: 'seq', width: '5%', align: 'center' as const,
      render: (_: unknown, _r: ScheduledJob, idx: number) => <span className="sched-seq">{idx + 1}</span>,
    },
    {
      title: '任务', dataIndex: 'name', key: 'name', align: 'center' as const,
      render: (_: string, j: ScheduledJob) => (
        // 表头保持居中（列上的 align），单元格内容整体靠左：标签行 + 名称 + 描述
        <div className="sched-cell-task sched-cell-task--left">
          <div className="sched-tags">
            <Tag color={j.is_public ? 'blue' : 'default'}>
              {j.is_public ? '公共' : '个人'}
            </Tag>
            {/* 公共任务不显示属主；个人任务/实例显示属主用户名 */}
            {!j.is_public && j.owner_name && (
              <span className="sched-muted">属主：{j.owner_name}</span>
            )}
          </div>
          <span className="sched-cell-title">{j.name}</span>
          {/* 只展示 标签 + 名称 + 描述（有才渲染），任务内容不在表格里出现 */}
          {j.description && <span className="sched-cell-desc">{j.description}</span>}
        </div>
      ),
    },
    {
      title: '执行计划', key: 'schedule', width: '17%', align: 'center' as const,
      render: (_: unknown, j: ScheduledJob) => (
        <div className="sched-cell-task">
          <span>{j.schedule_desc}</span>
          <span className="sched-muted">
            {j.enabled && j.next_run_at ? `下次 ${relTime(j.next_run_at)}` : j.enabled ? '等待调度' : '已停用'}
          </span>
        </div>
      ),
    },
    {
      title: '上次执行', key: 'last', width: '15%', align: 'center' as const,
      render: (_: unknown, j: ScheduledJob) => {
        if (!j.last_run_at) return <span className="sched-muted">未执行过</span>
        const meta = STATUS_META[j.last_status || ''] || { color: 'default', text: j.last_status }
        return (
          <div className="sched-cell-task">
            <Tag color={meta.color}>{meta.text}</Tag>
            <span className="sched-muted">{fmtTime(j.last_run_at)}</span>
          </div>
        )
      },
    },
    {
      title: '启用', key: 'enabled', width: '7%', align: 'center' as const,
      render: (_: unknown, j: ScheduledJob) => (
        <Switch size="small" checked={j.enabled} disabled={!canWrite(j)} onChange={(c) => onToggle(j, c)} />
      ),
    },
    {
      title: '操作', key: 'actions', width: '16%', align: 'center' as const,
      render: (_: unknown, j: ScheduledJob) => renderActions(j),
    },
  ]

  // ---------------- 渲染 ----------------

  return (
    <div className="sched-page">
      <div className="sched-header">
        <div className="sched-title">
          <ScheduleOutlined />
          <span>调度任务</span>
          <span className="sched-subtitle">由任务助手按计划自动执行：读邮件、建日程、定时汇报</span>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => reload()}>刷新</Button>
          {hasPerm(user, 'schedule_manage.create') && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建任务</Button>
          )}
        </Space>
      </div>

      <div className="sched-cards">
        <div className="sched-card">
          <div className="sched-card-value">{stats?.enabled ?? '-'}</div>
          <div className="sched-card-label">启用任务</div>
        </div>
        <div className="sched-card">
          <div className="sched-card-value">{stats?.running ?? '-'}</div>
          <div className="sched-card-label">运行中</div>
        </div>
        <div className="sched-card">
          <div className="sched-card-value">
            {stats ? stats.today_total : '-'}
            {stats && stats.today_total > 0 && (
              <span className="sched-card-sub">
                {' '}成功 {Math.round((stats.today_success / stats.today_total) * 100)}%
              </span>
            )}
          </div>
          <div className="sched-card-label">今日执行</div>
        </div>
        <div className="sched-card">
          <div className="sched-card-value sched-card-next">
            {stats?.next ? relTime(stats.next.at) : '-'}
          </div>
          <div className="sched-card-label">
            {stats?.next ? `下次执行：${stats.next.name}` : '暂无待执行任务'}
          </div>
        </div>
      </div>

      <div className="sched-toolbar">
        <Segmented
          value={filter}
          onChange={(v) => setFilter(v as typeof filter)}
          options={[
            // "全部"只对管理员出现：能看到平台上所有人的公共任务与个人任务
            ...(hasPerm(user, 'schedule_manage.view_all') ? [{ label: '全部', value: 'all' as const }] : []),
            { label: '公共任务', value: 'public' },
            { label: '我的任务', value: 'mine' },
            { label: `回收站${deletedJobs.length ? ` (${deletedJobs.length})` : ''}`, value: 'deleted' },
          ]}
        />
        <Input.Search
          allowClear placeholder="搜索任务名 / 内容" style={{ width: 220 }}
          value={search} onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <Spin spinning={loading}>
        {visibleJobs.length === 0 && !loading ? (
          <div className="sched-empty">
            <Empty description={
              filter === 'deleted' ? '回收站为空'
                : filter === 'all'
                  ? (allJobs.length === 0 ? '平台上还没有任何定时任务' : '没有符合条件的任务')
                : jobs.length === 0
                  ? (hasPerm(user, 'schedule_manage.create') ? '还没有定时任务，点击右上角“新建任务”创建' : '还没有定时任务，等待管理员创建公共任务')
                  : '没有符合条件的任务'
            } />
          </div>
        ) : (
          <Table
            rowKey="id"
            size="middle"
            columns={columns}
            dataSource={visibleJobs}
            pagination={false}
            tableLayout="fixed"
            scroll={{ x: TABLE_MIN_WIDTH }}
          />
        )}
      </Spin>

      {/* 管理员：新建/编辑抽屉 */}
      <Drawer
        title={editingId ? '编辑定时任务' : '新建定时任务'}
        width={560}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose
        footer={
          <div className="sched-drawer-footer">
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button type="primary" loading={saving} onClick={saveJob}>保存</Button>
          </div>
        }
      >
        <div className="sched-form">
          {!editingId && (
            <div className="sched-field">
              <label>任务类型</label>
              <Segmented
                value={isPublic ? 'public' : 'private'}
                onChange={(v) => setIsPublic(v === 'public')}
                options={[
                  { label: '公共任务（全员可见可选）', value: 'public' },
                  { label: '个人任务（仅自己）', value: 'private' },
                ]}
              />
            </div>
          )}
          <div className="sched-field">
            <label>任务名称</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：每天 8 点读邮箱并自动回复" maxLength={100} />
          </div>
          <div className="sched-field">
            <label>描述（可选，{DESCRIPTION_MAX_LEN} 字以内）</label>
            <Input
              value={description} onChange={(e) => setDescription(e.target.value)}
              maxLength={DESCRIPTION_MAX_LEN} showCount
              placeholder="一句话说明任务用途"
            />
          </div>
          <div className="sched-field">
            <label>任务内容</label>
            <Input.TextArea
              rows={5} value={prompt} onChange={(e) => setPrompt(e.target.value)}
              placeholder="用大白话描述即可，例如：每天8:00自动读取邮箱并自动回复，会议邀请自动创建日程"
            />
          </div>
          <div className="sched-field">
            <label>执行时间</label>
            <SchedulePicker value={sched} onChange={setSched} />
            <PreviewLine desc={drawerPreview.desc} runs={drawerPreview.runs} error={drawerPreview.error} />
          </div>
          <Collapse
            ghost
            items={[{
              key: 'advanced',
              label: '执行策略（可选：模型 / 技能 / MCP）',
              children: (
                <div className="sched-form">
                  <div className="sched-field">
                    <label>模型（默认跟随任务助手）</label>
                    <Select allowClear placeholder="默认" value={model} onChange={(v) => setModel(v)}
                      options={models.map((modelId) => ({ label: modelLabel(modelId), value: modelId }))} />
                  </div>
                  <div className="sched-field">
                    <label>技能（保存时固定版本，发布新版不会自动升级）</label>
                    {editingId && <><p style={{ color: 'var(--text-secondary)', fontSize: 12 }}>已固定 {Object.keys(boundSkillVersions).length} 个技能版本。仅改时间或说明不会升级技能。</p><Checkbox checked={refreshSkillVersions} onChange={e => setRefreshSkillVersions(e.target.checked)}>确认更新所选技能到当前可用版本</Checkbox></>}
                    <Select mode="multiple" allowClear placeholder="默认不启用" value={selSkills}
                      onChange={(v) => setSelSkills(v)} optionFilterProp="label"
                      options={['system', 'mine', 'shared'].map(group => ({ label: ({ system: '系统技能', mine: '我的技能', shared: '分享给我' } as Record<string, string>)[group], options: skills.filter(s => (s.source_group || 'system') === group).map(s => ({ label: s.display_name_zh || s.display_name || s.name, value: s.name })) }))} />
                  </div>
                  <div className="sched-field">
                    <label>MCP 工具（默认不启用，选择后生效）</label>
                    <Select mode="multiple" allowClear placeholder="默认不启用" value={selMcps}
                      onChange={(v) => setSelMcps(v)} optionFilterProp="label"
                      options={mcpServers.map((m) => ({ label: m.name, value: m.id }))} />
                  </div>
                </div>
              ),
            }]}
          />
        </div>
      </Drawer>

      {/* 用户：选择任务 / 修改执行时间 */}
      <Modal
        title={pickMode === 'select' ? '选择任务' : '修改执行时间'}
        open={!!pickJob}
        onCancel={() => setPickJob(null)}
        destroyOnClose
        footer={
          <div className="sched-drawer-footer">
            <Button onClick={() => setPickJob(null)}>取消</Button>
            <Button type="primary" loading={pickSaving} onClick={confirmPick}>
              {pickMode === 'select' ? '确认选择' : '保存'}
            </Button>
          </div>
        }
      >
        {pickJob && (
          <div className="sched-form">
            <div className="sched-pick-info">
              <div className="sched-cell-title">{pickJob.name}</div>
              {pickJob.description && <div className="sched-cell-desc">{pickJob.description}</div>}
              <div className="sched-pick-prompt">{pickJob.prompt}</div>
            </div>
            <div className="sched-field">
              <label>执行时间</label>
              <SchedulePicker value={pickSched} onChange={setPickSched} />
              <PreviewLine desc={pickPreview.desc} runs={pickPreview.runs} error={pickPreview.error} />
            </div>
          </div>
        )}
      </Modal>

      {/* 执行历史抽屉 */}
      <Drawer
        title={historyJob ? `执行历史 · ${historyJob.name}` : '执行历史'}
        width={640}
        open={!!historyJob}
        onClose={() => setHistoryJob(null)}
      >
        {/* 公共任务：创建者（管理员）可按人员查看该任务派生实例的执行情况 */}
        {historyJob?.is_public && hasPerm(user, 'schedule_manage.view_all') && historyJob.user_id === user?.id && (
          <div className="sched-runs-toolbar">
            <span className="sched-muted">人员</span>
            <Select
              allowClear
              placeholder="全部人员"
              style={{ width: 160 }}
              value={runsUser ?? undefined}
              onChange={(v) => {
                const next = v ?? null
                setRunsUser(next)
                setRunsPage(1)
                loadRuns(historyJob, 1, next)
              }}
              options={users.map((u) => ({ value: u.id, label: u.username }))}
            />
          </div>
        )}
        <Spin spinning={loadingRuns}>
          <div className="sched-runs">
            {runs.length === 0 && !loadingRuns && <Empty description="还没有执行记录" />}
            {runs.map((r) => {
              const meta = STATUS_META[r.status] || { color: 'default', text: r.status }
              return (
                <div key={r.id} className="sched-run-row">
                  {/* 单行：状态 / 触发方式 / 时间 / 耗时 /（聚合视图下的属主）/ 查看会话。
                      失败原因不再占一行，收进状态标签的悬浮提示，行高保持稳定 */}
                  <Tooltip title={r.error || ''}>
                    <Tag color={meta.color}>{meta.text}</Tag>
                  </Tooltip>
                  <span className="sched-muted">
                    {TRIGGER_TEXT[r.trigger_type] || r.trigger_type}
                    {r.trigger_type === 'retry' && r.attempt > 1 ? ` #${r.attempt}` : ''}
                  </span>
                  <span className="sched-muted sched-run-time">{fmtTime(r.started_at)}</span>
                  <span className="sched-muted">耗时 {durationSec(r)}</span>
                  {r.owner_id !== user?.id && r.owner_name && (
                    <span className="sched-run-owner">{r.owner_name}</span>
                  )}
                  <Button
                    size="small" type="link" className="sched-run-link"
                    disabled={!r.session_id} onClick={() => openRunSession(r)}
                  >
                    查看会话
                  </Button>
                </div>
              )
            })}
            {runsTotal > 20 && (
              <div className="sched-runs-pager">
                <Button size="small" disabled={runsPage <= 1}
                  onClick={() => { const p = runsPage - 1; setRunsPage(p); historyJob && loadRuns(historyJob, p) }}>
                  上一页
                </Button>
                <span className="sched-muted">第 {runsPage} 页 / 共 {Math.ceil(runsTotal / 20)} 页</span>
                <Button size="small" disabled={runsPage >= Math.ceil(runsTotal / 20)}
                  onClick={() => { const p = runsPage + 1; setRunsPage(p); historyJob && loadRuns(historyJob, p) }}>
                  下一页
                </Button>
              </div>
            )}
          </div>
        </Spin>
      </Drawer>

      {/* 他人会话只读预览：从抽屉里打开，抬高 zIndex 避免被抽屉压住 */}
      <SessionPreviewModal
        sessionId={previewSessionId}
        onClose={() => setPreviewSessionId(null)}
        zIndex={1200}
      />
    </div>
  )
}
