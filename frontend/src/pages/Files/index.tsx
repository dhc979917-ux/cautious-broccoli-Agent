import { useCallback, useEffect, useState } from 'react'
import {
  Tabs, Table, Input, Select, Button, Tag, Modal, Progress, Tooltip, Space, Empty, message,
  Segmented,
} from 'antd'
import {
  FolderOutlined, DownloadOutlined, DeleteOutlined, EditOutlined, UndoOutlined,
  StopOutlined, ReloadOutlined, LinkOutlined, FileTextOutlined, SyncOutlined,
  ExclamationCircleOutlined, UploadOutlined, CloudUploadOutlined,
} from '@ant-design/icons'
import { useSearchParams } from 'react-router-dom'
import {
  listFiles, fileStats, renameFile, softDeleteFile, restoreFile, hardDeleteFile,
  syncFiles, batchDownload, FileInfo, FileStats,
} from '../../api/file'
import { getAllUsers } from '../../api/admin'
import { useAuthStore } from '../../store/authStore'
import { hasPerm, hasModulePerm } from '../../constants/permissions'
import SessionPreviewModal from '../../components/SessionPreview'
import { PRIMARY_COLOR } from '../../constants/theme'
import './index.css'

type TabKey = 'all' | 'upload' | 'output' | 'trash'
// 全部/回收站视图下的类型过滤（我的上传、分析产出两个 Tab 自身即类型，不再显示该控件）
type KindKey = 'all' | 'upload' | 'output'

// 扩展名 → 图标颜色（按文件大类区分）
const EXT_COLORS: Record<string, string> = {
  xlsx: PRIMARY_COLOR, xls: PRIMARY_COLOR, csv: PRIMARY_COLOR,
  doc: '#2563EB', docx: '#2563EB',
  pdf: '#DC2626',
  ppt: '#D97706', pptx: '#D97706',
  zip: '#78716C', rar: '#78716C', '7z': '#78716C',
  png: '#0891B2', jpg: '#0891B2', jpeg: '#0891B2', gif: '#0891B2',
  txt: '#6B7280', md: '#6B7280', json: '#6B7280', log: '#6B7280',
}

function extColor(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  return EXT_COLORS[ext] || '#64748b'
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

function formatTime(s?: string): string {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

// 表格最小宽度：宽屏时各列按百分比自适应撑满，窄屏收缩到该宽度后横向滚动，
// 保证文件名列始终有可读宽度（列宽全部用百分比，不写死像素）
const TABLE_MIN_WIDTH = 1000

export default function Files() {
  // 视图状态（Tab + 类型过滤）落在 URL query 上：刷新或分享链接后仍停在同一视图
  const [searchParams, setSearchParams] = useSearchParams()
  const { user, token } = useAuthStore()
  // file_all 模块级 + 各操作叶子权限（本人文件永远可操作，叶子只约束他人文件）
  const isAdmin = hasModulePerm(user, 'file_all')
  const canView = hasPerm(user, 'file_all.view')
  const canSync = hasPerm(user, 'file_all.sync')
  const canDownloadOne = hasPerm(user, 'file_all.download.single')
  const canRename = hasPerm(user, 'file_all.manage.rename')
  const canSoftDelete = hasPerm(user, 'file_all.manage.soft_delete')
  const canRestore = hasPerm(user, 'file_all.manage.restore')
  const canHardDelete = hasPerm(user, 'file_all.hard_delete')
  const isOwn = (f: FileInfo) => f.user_id === user?.id
  // 能否预览他人会话：与后端 /sessions/{id}/preview 的放行口径保持一致
  const canPreviewOthers = canView || hasPerm(user, 'monitor_manage')

  const activeTab = (searchParams.get('tab') as TabKey) || 'all'
  const kindFilter = (searchParams.get('kind') as KindKey) || 'all'
  // 人员选择与类型过滤只在管理员的全量视图（全部 / 回收站）出现
  const showOwnerFilter = canView && (activeTab === 'all' || activeTab === 'trash')
  const [files, setFiles] = useState<FileInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [q, setQ] = useState('')
  const [filterUserId, setFilterUserId] = useState<number | null>(null)
  const [stats, setStats] = useState<FileStats | null>(null)
  const [selectedKeys, setSelectedKeys] = useState<number[]>([])
  const [users, setUsers] = useState<{ id: number; username: string }[]>([])
  // 来源会话预览浮窗（null=关闭）
  const [previewSessionId, setPreviewSessionId] = useState<number | null>(null)

  // 重命名弹窗
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameId, setRenameId] = useState(0)
  const [renameName, setRenameName] = useState('')
  // 彻底删除弹窗（两步密码确认，与后台管理一致）
  const [destroyOpen, setDestroyOpen] = useState(false)
  const [destroyId, setDestroyId] = useState(0)
  const [destroyName, setDestroyName] = useState('')
  const [destroyStep, setDestroyStep] = useState<1 | 2>(1)
  const [destroyUsername, setDestroyUsername] = useState('')
  const [destroyPassword, setDestroyPassword] = useState('')
  const [destroyLoading, setDestroyLoading] = useState(false)

  const fetchFiles = useCallback(async () => {
    setLoading(true)
    try {
      // 视图口径：
      //   我的上传 / 分析产出 → 一律只看本人（mine），管理员也不再看全平台
      //   全部 / 回收站      → 管理员按人员选择过滤，类型由三段控件决定
      const mineTab = activeTab === 'upload' || activeTab === 'output'
      const data = await listFiles({
        kind: mineTab ? activeTab : (kindFilter === 'all' ? undefined : kindFilter),
        deleted: activeTab === 'trash',
        q: q || undefined,
        mine: mineTab || undefined,
        user_id: !mineTab && canView && filterUserId ? filterUserId : undefined,
      })
      setFiles(data)
    } catch {
      message.error('获取文件列表失败')
    } finally {
      setLoading(false)
    }
  }, [activeTab, kindFilter, q, filterUserId, canView])

  const fetchStats = useCallback(async () => {
    try { setStats(await fileStats()) } catch { /* 静默 */ }
  }, [])

  useEffect(() => { fetchFiles() }, [fetchFiles])
  useEffect(() => { fetchStats() }, [fetchStats])
  useEffect(() => {
    if (canView) {
      getAllUsers().then(setUsers).catch(() => { /* 静默 */ })
    }
  }, [canView])
  // 视图或过滤条件变化后旧勾选已无意义，统一清空批量选择
  useEffect(() => { setSelectedKeys([]) }, [activeTab, kindFilter, filterUserId])

  const setView = (patch: { tab?: TabKey; kind?: KindKey }) => {
    const next = new URLSearchParams(searchParams)
    if (patch.tab !== undefined) {
      if (patch.tab === 'all') next.delete('tab')
      else next.set('tab', patch.tab)
      // 换 Tab 时清掉类型过滤：我的上传/分析产出由 Tab 自身决定类型
      next.delete('kind')
    }
    if (patch.kind !== undefined) {
      if (patch.kind === 'all') next.delete('kind')
      else next.set('kind', patch.kind)
    }
    setSearchParams(next, { replace: true })
  }

  const refresh = () => { fetchFiles(); fetchStats(); setSelectedKeys([]) }

  // ===== 下载（fetch + blob 携带 token）=====
  const downloadOne = (f: FileInfo) => {
    fetch(`/api/v1/files/${f.id}/download`, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => { if (!res.ok) throw new Error(); return res.blob() })
      .then((blob) => {
        const link = document.createElement('a')
        const objUrl = URL.createObjectURL(blob)
        link.href = objUrl
        link.download = f.original_name
        link.click()
        URL.revokeObjectURL(objUrl)
      })
      .catch(() => message.error('下载失败'))
  }

  const handleBatchDownload = async () => {
   try {
     // 上传类型不支持下载：批量下载只取产出文件
     const outputIds = files.filter(f => selectedKeys.includes(f.id) && f.kind === 'output').map(f => f.id)
      if (outputIds.length === 0) {
        message.warning('仅产出文件支持下载')
        return
      }
      const blob = await batchDownload(outputIds)
      const link = document.createElement('a')
      const objUrl = URL.createObjectURL(blob)
      link.href = objUrl
      link.download = 'files.zip'
      link.click()
      URL.revokeObjectURL(objUrl)
    } catch {
      message.error('批量下载失败')
    }
  }

  const handleBatchDelete = async () => {
    try {
      for (const id of selectedKeys) await softDeleteFile(id)
      message.success(`已将 ${selectedKeys.length} 个文件移入回收站`)
      refresh()
    } catch {
      message.error('批量删除失败')
    }
  }

  // ===== 单文件操作 =====
  const openRename = (f: FileInfo) => { setRenameId(f.id); setRenameName(f.original_name); setRenameOpen(true) }
  const handleRename = async () => {
    try {
      await renameFile(renameId, renameName)
      message.success('重命名成功')
      setRenameOpen(false)
      fetchFiles()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '重命名失败')
    }
  }

  const handleDelete = async (f: FileInfo) => {
    try {
      await softDeleteFile(f.id)
      message.success('已移入回收站')
      refresh()
    } catch {
      message.error('删除失败')
    }
  }

  const handleRestore = async (f: FileInfo) => {
    try {
      await restoreFile(f.id)
      message.success('文件已恢复')
      refresh()
    } catch {
      message.error('恢复失败')
    }
  }

  const openDestroy = (f: FileInfo) => {
    setDestroyId(f.id); setDestroyName(f.original_name)
    // 默认填本人账号；删他人文件时后端要求凭据方也具备 file_all.hard_delete
    setDestroyStep(1); setDestroyUsername(user?.account || user?.username || '')
    setDestroyPassword(''); setDestroyOpen(true)
  }
  const handleDestroyConfirm = async () => {
    setDestroyLoading(true)
    try {
      await hardDeleteFile(destroyId, destroyUsername, destroyPassword)
      message.success('文件已彻底删除')
      setDestroyOpen(false)
      refresh()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '删除失败')
    } finally {
      setDestroyLoading(false)
    }
  }

  const handleSync = async () => {
    try {
      const res = await syncFiles()
      message.success(res.message)
      refresh()
    } catch {
      message.error('对账失败')
    }
  }

  // ===== 表格列 =====
  // 表头与内容整体居中：列上声明 align，单元格内的 flex 容器由 CSS 同步居中；
  // 唯一例外是"文件名"列——表头保持居中，内容由 CSS 靠左（.file-name-cell）
  const baseColumns: any[] = [
    {
      title: '文件名', dataIndex: 'original_name', key: 'name', ellipsis: true, align: 'center',
      render: (name: string) => (
        <div className="file-name-cell">
          <span className="file-ext-icon" style={{ background: `${extColor(name)}1f`, color: extColor(name) }}>
            <FileTextOutlined />
          </span>
          <Tooltip title={name}><span className="file-name-text">{name}</span></Tooltip>
        </div>
      ),
    },
    {
      title: '类型', dataIndex: 'kind', key: 'kind', width: '7%', align: 'center',
      render: (kind: string) => kind === 'upload'
        ? <Tag icon={<UploadOutlined />} color="blue">上传</Tag>
        : <Tag icon={<CloudUploadOutlined />} color="green">产出</Tag>,
    },
    { title: '大小', dataIndex: 'size', key: 'size', width: '8%', align: 'center', render: (v: number) => formatSize(v) },
    {
      title: '来源', key: 'source', width: '18%', align: 'center',
      render: (_: any, r: FileInfo) => {
        if (r.kind !== 'output') return <span className="file-source-empty">—</span>
        // 本人会话可预览；他人会话需具备跨用户查看权限（后端 preview 接口同样把关）
        const previewable = !!r.session_id && !r.session_deleted
          && (r.session_owner_id === user?.id || canPreviewOthers)
        const linkBody = (
          <>
            <LinkOutlined />
            <span className="file-source-text">{r.session_title}</span>
          </>
        )
        return (
          <div className="file-source-cell">
            {r.agent_name && <Tag>{r.agent_name}</Tag>}
            {/* 会话已删除时标题取自快照：照常展示，但置灰不可点，不再是死链 */}
            {r.session_title && (previewable ? (
              <Tooltip title="预览生成该文件的会话">
                <button type="button" className="file-source-link"
                  onClick={() => setPreviewSessionId(r.session_id as number)}>
                  {linkBody}
                </button>
              </Tooltip>
            ) : (
              <Tooltip title={r.session_deleted ? '原会话已删除，无法预览' : '无权查看该会话'}>
                <span className="file-source-link is-disabled" aria-disabled="true">
                  {linkBody}
                </span>
              </Tooltip>
            ))}
          </div>
        )
      },
    },
  ]
  if (canView) {
    baseColumns.push({
      title: '所有者', dataIndex: 'username', key: 'owner', width: '9%', align: 'center',
      render: (v: string) => v || '—',
    })
  }
  baseColumns.push({
    title: '时间', dataIndex: 'created_at', key: 'time', width: '14%', align: 'center',
    render: (v: string) => formatTime(v),
  })
  baseColumns.push({
    title: '操作', key: 'actions', width: '11%', align: 'center',
    render: (_: any, r: FileInfo) => {
      // 本人文件永远显示操作；他人文件按叶子权限显隐
      const own = isOwn(r)
      return activeTab === 'trash' ? (
        <Space size={2}>
          {(own || canRestore) && <Tooltip title="恢复"><Button type="text" size="small" icon={<UndoOutlined />} onClick={() => handleRestore(r)} /></Tooltip>}
          {(own || canHardDelete) && <Tooltip title="彻底删除"><Button type="text" size="small" danger icon={<StopOutlined />} onClick={() => openDestroy(r)} /></Tooltip>}
        </Space>
      ) : (
       <Space size={2}>
         {r.kind === 'output' && (own || canDownloadOne) && <Tooltip title="下载"><Button type="text" size="small" icon={<DownloadOutlined />} onClick={() => downloadOne(r)} /></Tooltip>}
         {(own || canRename) && <Tooltip title="重命名"><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openRename(r)} /></Tooltip>}
          {(own || canSoftDelete) && <Tooltip title="删除"><Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(r)} /></Tooltip>}
        </Space>
      )
    },
  })

  // ===== 用量与统计 =====
  const my = stats?.my_usage
  const quotaPct = my && my.quota_bytes > 0 ? Math.min(100, Math.round((my.used_bytes / my.quota_bytes) * 100)) : 0

  const tabItems = [
    { key: 'all', label: '全部' },
    { key: 'upload', label: '我的上传' },
    { key: 'output', label: '分析产出' },
    { key: 'trash', label: '回收站' },
  ]

  return (
    <div className="files-page">
      <div className="files-header">
        <div className="files-header-left">
          <div className="files-icon-wrapper"><FolderOutlined /></div>
          <div>
            <h1 className="files-title">文件中心</h1>
            <p className="files-subtitle">
              {canView ? '管理全部用户的上传与分析产出文件' : '管理我的上传与分析产出文件'}
            </p>
          </div>
        </div>
        <div className="files-header-right">
          {my && (
            <Tooltip title="存储配额按每位用户独立计算，互不占用；平台总量见管理员统计">
              <div className="files-quota">
                <div className="files-quota-label">
                  我的用量 {formatSize(my.used_bytes)} / {formatSize(my.quota_bytes)}（{my.count} 个文件 · 每人配额）
                </div>
                <Progress percent={quotaPct} size="small" showInfo={false}
                  strokeColor={quotaPct > 90 ? '#DC2626' : quotaPct > 70 ? '#D97706' : PRIMARY_COLOR} />
              </div>
            </Tooltip>
          )}
          {canSync && (
            <Tooltip title="扫描磁盘，补登记缺失的历史文件元数据">
              <Button icon={<SyncOutlined />} onClick={handleSync}>对账同步</Button>
            </Tooltip>
          )}
          <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
        </div>
      </div>

      {canView && stats?.global && (
        <div className="files-stats-strip">
          <div className="files-stat-item">
            <span className="files-stat-value">{stats.global.total_count}</span>
            <span className="files-stat-label">平台文件总数</span>
          </div>
          <div className="files-stat-item">
            <span className="files-stat-value">{formatSize(stats.global.total_bytes)}</span>
            <span className="files-stat-label">平台总用量</span>
          </div>
        </div>
      )}

      <div className="files-toolbar">
        <Tabs activeKey={activeTab} items={tabItems}
          onChange={(k) => setView({ tab: k as TabKey })}
          className="files-tabs" />
        <div className="files-toolbar-right">
          {/* 类型三段过滤：只在管理员的全量视图出现，与人员选择并排 */}
          {showOwnerFilter && (
            <Segmented
              value={kindFilter}
              onChange={(v) => setView({ kind: v as KindKey })}
              options={[
                { label: '全部', value: 'all' },
                { label: '上传', value: 'upload' },
                { label: '产出', value: 'output' },
              ]}
            />
          )}
          {showOwnerFilter && (
            <Select allowClear placeholder="全部用户" style={{ width: 150 }}
              value={filterUserId ?? undefined}
              onChange={(v) => setFilterUserId(v ?? null)}
              options={users.map((u) => ({ value: u.id, label: u.username }))} />
          )}
          <Input.Search allowClear placeholder="搜索文件名" style={{ width: 220 }}
            onSearch={(v) => setQ(v)} />
        </div>
      </div>

      {activeTab !== 'trash' && selectedKeys.length > 0 && (
        <div className="files-batch-bar">
          <span>已选 {selectedKeys.length} 个文件</span>
          <Space>
            <Button size="small" icon={<DownloadOutlined />} onClick={handleBatchDownload}>批量下载</Button>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={handleBatchDelete}>批量删除</Button>
            <Button size="small" type="text" onClick={() => setSelectedKeys([])}>取消选择</Button>
          </Space>
        </div>
      )}

      <Table
        rowKey="id"
        columns={baseColumns}
        dataSource={files}
        loading={loading}
        rowSelection={activeTab === 'trash' ? undefined : {
          selectedRowKeys: selectedKeys,
          onChange: (keys) => setSelectedKeys(keys as number[]),
        }}
        pagination={{ pageSize: 15, showSizeChanger: false, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无文件" /> }}
        tableLayout="fixed"
        scroll={{ x: TABLE_MIN_WIDTH }}
      />

      {/* 重命名弹窗 */}
      <Modal title="重命名文件" open={renameOpen} onCancel={() => setRenameOpen(false)}
        onOk={handleRename} okText="保存" cancelText="取消" okButtonProps={{ disabled: !renameName.trim() }}>
        <Input value={renameName} onChange={(e) => setRenameName(e.target.value)}
          placeholder="请输入新的文件名" maxLength={255} />
      </Modal>

      {/* 彻底删除两步确认（与后台管理一致） */}
      <Modal
        title={<span style={{ color: '#ef4444' }}><ExclamationCircleOutlined style={{ marginRight: 8 }} />彻底删除文件</span>}
        open={destroyOpen}
        onCancel={() => { setDestroyOpen(false); setDestroyStep(1); setDestroyPassword('') }}
        closable={false}
        footer={destroyStep === 1 ? (
          <Space>
            <Button onClick={() => { setDestroyOpen(false); setDestroyStep(1) }}>取消</Button>
            <Button type="primary" danger disabled={!destroyUsername || !destroyPassword} onClick={() => setDestroyStep(2)}>下一步</Button>
          </Space>
        ) : (
          <Space>
            <Button onClick={() => setDestroyStep(1)}>返回</Button>
            <Button type="primary" danger loading={destroyLoading} onClick={handleDestroyConfirm}>确认删除</Button>
          </Space>
        )}
      >
        {destroyStep === 1 ? (
          <div className="destroy-step">
            <p>即将彻底删除 <b>{destroyName}</b>，磁盘文件与记录都会被清除且不可恢复。请输入凭据确认身份：</p>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Input placeholder="账号" value={destroyUsername} onChange={(e) => setDestroyUsername(e.target.value)} />
              <Input.Password placeholder="密码" value={destroyPassword} onChange={(e) => setDestroyPassword(e.target.value)} />
          </Space>
        </div>
      ) : (
        <div className="destroy-step">
          <p>确认以 <b>{destroyUsername}</b> 的身份彻底删除该文件？此操作不可撤销。</p>
        </div>
      )}
      </Modal>

      {/* 来源会话只读预览：本人可在浮窗里跳转聊天页，管理员可看他人会话 */}
      <SessionPreviewModal sessionId={previewSessionId} onClose={() => setPreviewSessionId(null)} />
    </div>
  )
}
