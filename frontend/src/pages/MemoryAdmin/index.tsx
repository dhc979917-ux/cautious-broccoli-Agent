import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Empty, Input, Modal, Select, Space, Table, Tabs, Tag, Typography } from 'antd'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { changeMemoryStatus, createMemoryExtraction, ExtractionMethod, listExtractionMethods, memoryError, MemoryEntry, MemorySource, MemoryTask, readMemory } from '../../api/memory'
import { dateLabel, memoryTitle, methodLabel, SOURCE_LABELS, sourceLabel, statusColor, statusLabel } from './labels'
import MemoryMarkdown from './MemoryMarkdown'
import TaskReview from './TaskReview'
import useMemoryAdmin from './useMemoryAdmin'
import './index.css'

export default function MemoryAdmin() {
  const data = useMemoryAdmin()
  const [tab, setTab] = useState('contents')
  const [selected, setSelected] = useState<React.Key[]>([])
  const [taskId, setTaskId] = useState<string | null>(null)
  const [entry, setEntry] = useState<MemorySource | null>(null)
  const [entryLoading, setEntryLoading] = useState(false)
  const [extracting, setExtracting] = useState(false)
  const [methods, setMethods] = useState<ExtractionMethod[]>([])
  const [method, setMethod] = useState('summary')
  const [busy, setBusy] = useState(false)
  const [modalError, setModalError] = useState('')
  const [modalLoading, setModalLoading] = useState(false)
  const currentOwner = useRef(data.userId)
  const readTicket = useRef(0)
  const lock = useRef(false)
  currentOwner.current = data.userId
  useEffect(() => { readTicket.current++; setSelected([]); setTaskId(null); setEntry(null); setEntryLoading(false); setExtracting(false) }, [data.userId])
  useEffect(() => { setSelected([]) }, [data.page, data.keyword, data.source, data.status])
  const showEntry = async (row: MemoryEntry) => {
    if (!data.userId) return
    const owner = data.userId
    const requestId = ++readTicket.current
    setEntryLoading(true); setEntry(null)
    try {
      const result = await readMemory(row.id, owner)
      if (requestId === readTicket.current && currentOwner.current === owner) setEntry({ ...result, source_type: row.source_type })
    } catch (cause) { if (requestId === readTicket.current && currentOwner.current === owner) data.setError(memoryError(cause)) }
    finally { if (requestId === readTicket.current) setEntryLoading(false) }
  }
  const openExtraction = async () => {
    if (!data.userId) { data.setError('请先选择人员。'); return }
    if (!data.access.create) { data.setError('您没有此用户记忆的萃取权限。'); return }
    if (!selected.length) { setTab('contents'); data.setStatus('published'); data.setError('请勾选需要整理的记忆，再点击“萃取记忆”。'); return }
    const owner = data.userId
    setModalError(''); setMethods([]); setMethod('summary'); setModalLoading(true); setExtracting(true)
    try {
      const result = await listExtractionMethods(owner)
      if (currentOwner.current === owner) { setMethods(result); setMethod(result.some((item) => item.code === 'summary') ? 'summary' : result[0]?.code || '') }
    } catch (cause) { if (currentOwner.current === owner) setModalError(memoryError(cause)) }
    finally { if (currentOwner.current === owner) setModalLoading(false) }
  }
  const submitExtraction = async () => {
    if (lock.current || !data.userId || !methods.length || modalLoading) return
    lock.current = true; setBusy(true); setModalError('')
    const owner = data.userId
    try {
      const task = await createMemoryExtraction(owner, selected.map(String), method)
      if (currentOwner.current === owner) { setExtracting(false); setSelected([]); setTab('tasks'); setTaskId(task.id); void data.reload() }
    } catch (cause) { if (currentOwner.current === owner) setModalError(memoryError(cause)) }
    finally { lock.current = false; setBusy(false) }
  }
  const lifecycle = (row: MemoryEntry) => Modal.confirm({
    title: row.status === 'published' ? '申请停用此记忆？' : '申请恢复此记忆？', content: '操作将生成审核候选；审核通过并完成索引同步后才会生效。原始内容不会被直接删除。', okText: '提交申请', cancelText: '取消',
    onOk: async () => {
      if (!data.userId) return
      const owner = data.userId
      try {
        const task = await changeMemoryStatus(row, owner, row.status === 'published' ? 'disable' : 'restore')
        if (currentOwner.current === owner) { setTaskId(task.id); setTab('tasks'); void data.reload() }
      } catch (cause) { data.setError(memoryError(cause)); throw cause }
    },
  })
  const contents = <>
    <div className="memory-toolbar"><Input.Search placeholder="搜索记忆路径" allowClear onSearch={(value) => { data.setKeyword(value); data.setPage(1) }} />
      <Select aria-label="记忆状态" value={data.status} onChange={(value) => { data.setStatus(value); data.setPage(1) }} options={['published', 'disabled'].map((value) => ({ value, label: statusLabel(value) }))} />
      <Select aria-label="记忆来源" placeholder="全部来源" allowClear value={data.source} onChange={(value) => { data.setSource(value); data.setPage(1) }} options={Object.entries(SOURCE_LABELS).filter(([value]) => !value.startsWith('lifecycle_')).map(([value, label]) => ({ value, label }))} />
    </div>
    {selected.length > 0 && <div className="memory-selection"><span>已选 {selected.length} 条记忆</span><Button type="link" onClick={() => void openExtraction()}>萃取所选记忆</Button><Button type="text" onClick={() => setSelected([])}>取消选择</Button></div>}
    <Table<MemoryEntry> rowKey="id" dataSource={data.entries} loading={data.loading} scroll={{ x: 720 }} locale={{ emptyText: <Empty description="暂无记忆内容；用户会话产生的记忆会显示在这里。" /> }}
      rowSelection={data.status === 'published' && data.access.create ? { selectedRowKeys: selected, onChange: setSelected, getCheckboxProps: (row) => ({ disabled: row.status !== 'published' }) } : undefined}
      pagination={{ current: data.page, total: data.total, pageSize: 20, showSizeChanger: false, onChange: data.setPage, showTotal: (count) => `共 ${count} 条` }}
      columns={[
        { title: '记忆内容', key: 'title', render: (_, row) => <Button type="link" className="memory-title-link" onClick={() => void showEntry(row)}>{memoryTitle(row.path)}</Button> },
        { title: '来源', key: 'source', width: 115, render: (_, row) => sourceLabel(row.source_type) },
        { title: '状态', key: 'status', width: 100, render: (_, row) => <Tag color={statusColor(row.status)}>{statusLabel(row.status)}</Tag> },
        { title: '记录时间', key: 'date', width: 185, render: (_, row) => dateLabel(row.created_at) },
        { title: '操作', key: 'actions', width: 175, render: (_, row) => <Space><Button type="link" onClick={() => void showEntry(row)}>查看</Button>{data.access.create && ['published', 'disabled'].includes(row.status) && <Button type="link" onClick={() => lifecycle(row)}>{row.status === 'published' ? '申请停用' : '申请恢复'}</Button>}</Space> },
      ]} />
  </>
  const candidates = <>
    <div className="memory-toolbar"><Select aria-label="候选状态" placeholder="全部状态" allowClear value={data.taskStatus} onChange={(value) => { data.setTaskStatus(value); data.setTaskPage(1) }} options={['review', 'queued', 'generating', 'index_pending', 'completed', 'failed', 'expired'].map((value) => ({ value, label: statusLabel(value) }))} />
      <Typography.Text type="secondary">萃取不会直接覆盖原记忆，需审核后发布。</Typography.Text></div>
    <Table<MemoryTask> rowKey="id" dataSource={data.tasks} loading={data.loading} scroll={{ x: 620 }} locale={{ emptyText: <Empty description="暂无候选；在记忆内容中勾选来源并点击“萃取记忆”。" /> }}
      pagination={{ current: data.taskPage, total: data.taskTotal, pageSize: 20, showSizeChanger: false, onChange: data.setTaskPage, showTotal: (count) => `共 ${count} 个任务` }}
      columns={[
        { title: '来源', key: 'source', render: (_, row) => sourceLabel(row.source_type) },
        { title: '萃取方式', key: 'method', render: (_, row) => row.source_type.startsWith('lifecycle_') ? '记忆状态变更' : methodLabel(row.method_code) },
        { title: '状态', key: 'status', render: (_, row) => <Tag color={statusColor(row.status)}>{statusLabel(row.status)}</Tag> },
        { title: '创建时间', key: 'date', width: 185, render: (_, row) => dateLabel(row.created_at) },
        { title: '操作', key: 'action', width: 105, render: (_, row) => <Button type="link" onClick={() => setTaskId(row.id)}>{row.status === 'review' ? '审核候选' : '查看任务'}</Button> },
      ]} />
  </>
  return <div className="memory-page">
    <header className="memory-page-heading"><div><Typography.Title level={2}>用户记忆</Typography.Title><Typography.Paragraph type="secondary">查看记忆、萃取重点，并审核候选内容。</Typography.Paragraph></div>
      <Space><Button icon={<ReloadOutlined />} onClick={() => void data.reload()} loading={data.loading} disabled={!data.userId}>刷新</Button><Button type="primary" icon={<ThunderboltOutlined />} onClick={() => void openExtraction()}>萃取记忆</Button></Space></header>
    <div className="memory-owner"><label htmlFor="memory-owner-select">所属人员</label><Select id="memory-owner-select" showSearch filterOption={false} onSearch={(value) => void data.searchOwners(value)} placeholder="搜索姓名或账号，选择人员" value={data.userId || undefined} onChange={data.changeOwner} options={data.owners.map((owner) => ({ value: owner.id, label: `${owner.username} · ${owner.account}` }))} /></div>
    {data.error && <Alert type="error" showIcon message={data.error} closable onClose={() => data.setError('')} />}
    {!data.userId ? <Empty description="选择人员后，查看其记忆和待审核候选。" /> : <Tabs activeKey={tab} onChange={setTab} items={[{ key: 'contents', label: '记忆内容', children: contents }, { key: 'tasks', label: '候选审核', children: candidates }]} />}
    <Drawer title="记忆内容" open={!!entry || entryLoading} onClose={() => { readTicket.current++; setEntry(null); setEntryLoading(false) }} width={680} rootClassName="memory-overlay">
      {entryLoading ? <Typography.Text>正在加载记忆…</Typography.Text> : entry && <><Tag>{sourceLabel(entry.source_type)}</Tag><Typography.Title level={4}>{memoryTitle(entry.path)}</Typography.Title><MemoryMarkdown content={entry.content} /><details className="memory-technical"><summary>文件位置</summary>{entry.path}</details></>}
    </Drawer>
    <TaskReview taskId={taskId} onClose={() => setTaskId(null)} onChanged={() => void data.reload()} />
    <Modal title="萃取记忆" open={extracting} onCancel={() => { if (!busy) setExtracting(false) }} onOk={() => void submitExtraction()} okText="开始萃取" cancelText="取消" confirmLoading={busy} okButtonProps={{ disabled: modalLoading || !methods.length }} rootClassName="memory-overlay">
      <Typography.Paragraph>已选择 {selected.length} 条记忆作为来源。生成的候选需要审核，不会直接修改来源。</Typography.Paragraph>
      {modalError && <Alert type="error" message={modalError} showIcon />}
      <label className="memory-field-label" htmlFor="memory-method-select">萃取方式</label><Select id="memory-method-select" value={method} loading={modalLoading} onChange={setMethod} style={{ width: '100%' }} options={methods.map((item) => ({ value: item.code, label: item.title }))} />
    </Modal>
  </div>
}
