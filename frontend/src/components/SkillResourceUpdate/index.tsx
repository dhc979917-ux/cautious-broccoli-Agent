import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Modal, Space, Table, Tag, message } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { resourceCancel, resourceConfirm, resourceHistory, resourceJob, resourceUpload, type ChangeJob, type ResourceKind } from '../../api/skillResources'
import { readableError } from '../../api/governance'

export default function SkillResourceUpdate({ kind, id, onDone, disabled = false }: { kind: ResourceKind; id: number; onDone?: () => void; disabled?: boolean }) {
  const done = useRef(onDone)
  done.current = onDone
  const input = useRef<HTMLInputElement>(null)
  const [job, setJob] = useState<ChangeJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const active = !!job && !['done', 'failed', 'cancelled', 'planned'].includes(job.status)
  useEffect(() => {
    let alive = true
    resourceHistory(kind, id).then((history) => {
      const pending = history.jobs.find((item) => !['done', 'failed', 'cancelled'].includes(item.status))
      if (alive) setJob(pending || null)
    }).catch(() => {})
    return () => { alive = false }
  }, [kind, id])
  useEffect(() => {
    if (!active || !job?.id) return
    let alive = true
    let timer: number
    const poll = async () => {
      try {
        const next = await resourceJob(kind, id, job.id!)
        if (!alive) return
        setJob(next)
        if (next.status === 'done') { message.success(next.message); done.current?.() }
        if (next.status === 'failed') setError(next.message)
      } catch (cause) { if (alive) setError(readableError(cause)) }
      if (alive) timer = window.setTimeout(poll, 1000)
    }
    timer = window.setTimeout(poll, 1000)
    return () => { alive = false; window.clearTimeout(timer) }
  }, [active, job?.id, kind, id])
  async function upload(file: File) {
    setBusy(true); setError('')
    try { setJob(await resourceUpload(kind, id, file)) }
    catch (cause) { setError(readableError(cause)) }
    finally { setBusy(false) }
  }
  async function confirm() {
    if (!job?.id) return
    setBusy(true)
    try { setJob(await resourceConfirm(kind, id, job.id)) }
    catch (cause) { setError(readableError(cause)) }
    finally { setBusy(false) }
  }
  async function cancel() {
    if (!job?.id || job.status !== 'planned') return
    try { await resourceCancel(kind, id, job.id); setJob(null) }
    catch (cause) { setError(readableError(cause)) }
  }
  return <>
    <Button icon={<UploadOutlined />} loading={busy || active} disabled={disabled || job?.status === 'planned'} onClick={() => input.current?.click()}>上传更新包</Button>
    <input ref={input} hidden type="file" accept=".zip" aria-label="选择ZIP更新包" onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void upload(file) }} />
    {active && <Tag color="processing">{job?.message}</Tag>}
    <Modal title={kind === 'package' ? '确认技能包更新' : '确认Skill更新'} open={job?.status === 'planned'} width={720} onCancel={() => void cancel()} onOk={() => void confirm()} confirmLoading={busy} okText="确认并生效" cancelText="取消更新">
      <Alert type="warning" showIcon message="校验成功后立即更新服务器文件并发布；校验失败保留原文件与原版本。" />
      {kind === 'package' && <Space direction="vertical" style={{ width: '100%', marginBlock: 16 }}>{Object.entries(job?.members || {}).map(([label, paths]) => <div key={label}><strong>{label}成员（{paths.length}）</strong><div>{paths.join('、') || '无'}</div></div>)}<div>相同路径保留身份和授权；移除成员仅停用，重命名按移除＋新增处理。</div></Space>}
      <Table size="small" rowKey="path" dataSource={job?.files || []} pagination={{ pageSize: 8 }} columns={[{ title: '文件路径', dataIndex: 'path', ellipsis: true }, { title: '变化', dataIndex: 'change', width: 80 }]} />
      {error && <Alert type="error" showIcon message={error} />}
    </Modal>
    {error && job?.status !== 'planned' && <Alert type="error" closable onClose={() => setError('')} message={error} />}
  </>
}
