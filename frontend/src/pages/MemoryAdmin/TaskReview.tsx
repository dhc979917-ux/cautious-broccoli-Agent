import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Collapse, Drawer, Empty, Input, Modal, Space, Spin, Tag, Typography } from 'antd'
import { memoryError, MemoryItem, MemorySource, MemoryTaskDetail, publishMemoryTask, readMemoryTask, retryMemoryTask, reviewMemoryItem, ReviewDecision } from '../../api/memory'
import { dateLabel, IN_PROGRESS, memoryTitle, methodLabel, sourceLabel, statusColor, statusLabel, taskFailure } from './labels'
import MemoryMarkdown from './MemoryMarkdown'

export default function TaskReview({ taskId, onClose, onChanged }: { taskId: string | null; onClose: () => void; onChanged: () => void }) {
  const [task, setTask] = useState<MemoryTaskDetail | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [source, setSource] = useState<MemorySource | null>(null)
  const [review, setReview] = useState<{ item: MemoryItem; decision: ReviewDecision; edit: boolean } | null>(null)
  const [content, setContent] = useState('')
  const [reason, setReason] = useState('')
  const lock = useRef(false)
  const generation = useRef(0)
  const current = useRef(taskId)
  current.current = taskId
  const load = useCallback(async () => {
    const requestId = ++generation.current
    if (!taskId) return
    try {
      const detail = await readMemoryTask(taskId)
      if (requestId === generation.current && current.current === taskId) setTask(detail)
    } catch (cause) { if (requestId === generation.current && current.current === taskId) setError(memoryError(cause)) }
  }, [taskId])
  useEffect(() => { setTask(null); setError(''); setSource(null); setReview(null); void load(); return () => { generation.current++ } }, [load])
  useEffect(() => {
    if (!task || !IN_PROGRESS.includes(task.status)) return
    const timer = window.setInterval(() => { void load() }, 5000)
    return () => window.clearInterval(timer)
  }, [task, load])
  const act = async (operation: () => Promise<unknown>) => {
    if (lock.current) return false
    lock.current = true; setBusy(true); setError('')
    const target = taskId
    try {
      await operation()
      if (current.current === target) { await load(); onChanged() }
      return true
    } catch (cause) { if (current.current === target) setError(memoryError(cause)); return false }
    finally { lock.current = false; setBusy(false) }
  }
  const decide = (item: MemoryItem, decision: ReviewDecision, edit = false) => {
    if (!task) return
    if (edit || task.review_access.requires_reason && !task.review_access.reason_recorded) {
      setContent(item.content); setReason(''); setReview({ item, decision, edit })
    } else void act(() => reviewMemoryItem(task.id, item, decision))
  }
  const confirmReview = async () => {
    if (!review || !task) return
    if (task.review_access.requires_reason && !task.review_access.reason_recorded && reason.trim().length < 10) {
      setError('请填写至少10字的审核说明。'); return
    }
    const ok = await act(() => reviewMemoryItem(task.id, review.item, review.decision, review.edit ? content : undefined, reason))
    if (ok) setReview(null)
  }
  const canPublish = task?.status === 'review' && task.review_access.can_publish && task.items.length > 0 &&
    task.items.every((item) => item.status !== 'pending') && task.items.some((item) => ['accepted', 'keep_both'].includes(item.status))
  return <>
    <Drawer title="候选审核" open={!!taskId} onClose={onClose} width={780} rootClassName="memory-overlay" destroyOnClose
      extra={<Button onClick={() => { setError(''); void load() }} disabled={busy}>刷新</Button>}
      footer={task && <div className="memory-review-footer"><span>{task.status === 'review' ? '处理所有候选后，确认发布至用户记忆。' : statusLabel(task.status)}</span>
        <Button type="primary" disabled={!canPublish} loading={busy} onClick={() => void act(() => publishMemoryTask(task))}>确认发布</Button></div>}>
      {error && !review && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
      {!task ? <Spin /> : <>
        <div className="memory-task-meta"><Tag color={statusColor(task.status)}>{statusLabel(task.status)}</Tag><span>{sourceLabel(task.source_type)}</span><span>{methodLabel(task.method_code)}</span><span>{dateLabel(task.created_at)}</span></div>
        {task.status === 'failed' && <Alert type="warning" showIcon message={taskFailure(task.error_code)} action={<Button loading={busy} onClick={() => void act(() => retryMemoryTask(task))}>重新萃取</Button>} />}
        {IN_PROGRESS.includes(task.status) && <Alert type="info" showIcon message={task.status === 'index_pending' ? '正在同步索引；索引就绪后才会标记为完成。' : '正在处理，请稍候；此窗口会自动更新。'} />}
        {task.content_removed && <Empty description="候选已过期，临时内容已清理；已发布记忆不受影响。" />}
        {!task.items.length && !task.content_removed && <Empty description={task.status === 'failed' ? '尚未生成可审核的候选' : '候选生成后会显示在这里'} />}
        {task.items.map((item, position) => <section className="memory-candidate" key={item.id}>
          <div className="memory-candidate-heading"><Typography.Text strong>候选 {position + 1}</Typography.Text><Tag color={statusColor(item.status)}>{statusLabel(item.status)}</Tag></div>
          {item.conflict_group && <Alert type="warning" message="此候选存在冲突，请选择其中一条，或明确保留两者。" />}
          <MemoryMarkdown content={item.content} />
          <Collapse ghost items={[{ key: 'sources', label: `来源依据 · ${item.source_ids.length} 条`, children: <div className="memory-evidence">{item.evidence.map((evidence, index) => {
            const origin = task.sources.find((entry) => entry.id === evidence.source_id)
            return <div key={index}><Button type="link" disabled={!origin} onClick={() => setSource(origin || null)}>{memoryTitle(origin?.path, index)}</Button><MemoryMarkdown content={evidence.quote} /></div>
          })}</div> }]} />
          {task.status === 'review' && item.status === 'pending' && task.review_access.can_review && <Space wrap>
            <Button type="primary" disabled={busy} onClick={() => decide(item, 'accepted')}>接受</Button>
            <Button disabled={busy} onClick={() => decide(item, 'rejected')}>驳回</Button>
            {!task.source_type.startsWith('lifecycle_') && <Button disabled={busy} onClick={() => decide(item, 'accepted', true)}>编辑后接受</Button>}
            {item.conflict_group && <Button disabled={busy} onClick={() => decide(item, 'keep_both')}>保留两者</Button>}
          </Space>}
        </section>)}
        {task.status === 'review' && !task.review_access.can_review && <Alert type="info" message="您没有审核权限，或不能审核自己提交的候选。请交由其他审核人处理。" />}
      </>}
    </Drawer>
    <Drawer title="来源内容" open={!!source} onClose={() => setSource(null)} width={620} rootClassName="memory-overlay">
      {source && <><Tag>{sourceLabel(source.source_type)}</Tag><Typography.Title level={4}>{memoryTitle(source.path)}</Typography.Title><MemoryMarkdown content={source.content} /></>}
    </Drawer>
    <Modal title={review?.edit ? '编辑后接受' : '填写审核说明'} open={!!review} onCancel={() => { if (!busy) { setReview(null); setError('') } }} onOk={() => void confirmReview()} confirmLoading={busy} okText="确认审核" cancelText="取消" rootClassName="memory-overlay" width={720}>
      {error && <Alert type="error" showIcon message={error} />}
      {review?.edit && <><label className="memory-field-label" htmlFor="memory-edited-content">编辑内容</label><Input.TextArea id="memory-edited-content" value={content} onChange={(event) => setContent(event.target.value)} rows={7} maxLength={4000} />
        <div className="memory-field-label">预览</div><MemoryMarkdown content={content} /></>}
      {task?.review_access.requires_reason && !task.review_access.reason_recorded && <><label className="memory-field-label" htmlFor="memory-review-reason">管理员自审说明（至少10字）</label><Input.TextArea id="memory-review-reason" rows={3} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} /></>}
    </Modal>
  </>
}
