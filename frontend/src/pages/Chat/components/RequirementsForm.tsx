// 需求采集表单：CollectRequirements 外部执行的渲染端。
//
// 与 AskUserBar 的分工：AskUserBar 是"从若干已知方案里挑一个"的单选条；
// 这里是"一次补齐多项未知事实"的表单。一次提交回传全部答案，
// 走的是同一条 external_reply WebSocket 通道（answer_text 放 JSON 字符串），
// 因此后端桥接层无需为表单另开协议。
//
// 未作答的题由后端用模型填的 assume 兜底并记为「假设，待确认」，
// 所以这里不强制校验必填——留空直接提交是合法且常见的操作。
import { useEffect, useMemo, useState } from 'react'
import { Button, Checkbox, Input, InputNumber, Tooltip } from 'antd'
import {
  FormOutlined,
  ClockCircleOutlined,
  ThunderboltOutlined,
  CheckOutlined,
} from '@ant-design/icons'
import { MessageStep, IntakeQuestion } from '../types'

interface RequirementsFormProps {
  step: MessageStep
  onReply?: (requestId: string, answerText: string) => void
  onCancel?: (requestId: string) => void
}

type Answers = Record<string, string | string[]>

function fmtLeft(sec: number) {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

// 历史消息里 answer 存的是提交时的 JSON 字符串，还原成"答了几项"的摘要
function summarize(answer: string | undefined, questions: IntakeQuestion[]) {
  if (!answer) return null
  let parsed: Answers = {}
  try {
    const obj = JSON.parse(answer)
    if (obj && typeof obj === 'object') parsed = obj as Answers
  } catch {
    return { filled: 0, total: questions.length, raw: answer }
  }
  const filled = Object.values(parsed).filter((v) =>
    Array.isArray(v) ? v.length > 0 : String(v ?? '').trim() !== '',
  ).length
  return { filled, total: questions.length, raw: '' }
}

function QuestionControl({
  q,
  value,
  disabled,
  onChange,
}: {
  q: IntakeQuestion
  value: string | string[] | undefined
  disabled: boolean
  onChange: (v: string | string[]) => void
}) {
  if (q.type === 'choice') {
    const opts = q.options || []
    // 不用 Segmented：它不换行，选项一多就把后面的裁掉（实测裁断 200+px）。
    // 换成与 AskUserBar 同款的换行小按钮，窄屏也能自然折行。
    const cur = typeof value === 'string' ? value : ''
    return (
      <span className="intake-opts">
        <Button
          size="small"
          className={`intake-opt ${cur === '' ? 'picked' : ''}`}
          disabled={disabled}
          onClick={() => onChange('')}
        >
          默认
        </Button>
        {opts.map((o) => (
          <Button
            key={o}
            size="small"
            className={`intake-opt ${cur === o ? 'picked' : ''}`}
            disabled={disabled}
            onClick={() => onChange(o)}
          >
           {o}
         </Button>
       ))}
        {q.allow_other && (
          <Input
            size="small"
            allowClear
            className="intake-other"
            placeholder="其他（自定义）"
            disabled={disabled}
            value={cur && !opts.includes(cur) ? cur : ''}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
     </span>
   )
 }
 if (q.type === 'multi') {
    const opts = q.options || []
    const arr = Array.isArray(value) ? value : []
    const selected = arr.filter((v) => opts.includes(v))
    const customVal = arr.find((v) => !opts.includes(v)) || ''
    return (
      <span className="intake-opts">
        <Checkbox.Group
          disabled={disabled}
          value={selected}
          onChange={(v) => onChange([...(v as string[]), ...(customVal ? [customVal] : [])])}
          options={opts.map((o) => ({ label: o, value: o }))}
        />
        {q.allow_other && (
          <Input
            size="small"
            allowClear
            className="intake-other"
            placeholder="其他（自定义）"
            disabled={disabled}
            value={customVal}
            onChange={(e) => {
              const t = e.target.value
              onChange([...selected, ...(t ? [t] : [])])
            }}
          />
        )}
      </span>
    )
 }
  if (q.type === 'number') {
    return (
      <InputNumber
        size="small"
        disabled={disabled}
        style={{ width: 160 }}
        placeholder={q.placeholder || q.assume || undefined}
        addonAfter={q.unit || undefined}
        value={value === undefined || value === '' ? null : Number(value)}
        onChange={(v) => onChange(v === null || v === undefined ? '' : String(v))}
      />
    )
  }
  if (q.type === 'text' && q.multiline) {
    return (
      <Input.TextArea
        size="small"
        disabled={disabled}
        allowClear
        autoSize={{ minRows: 2, maxRows: 6 }}
        style={{ maxWidth: 380 }}
        placeholder={q.placeholder || q.assume || '留空则采用默认值'}
        value={typeof value === 'string' ? value : ''}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
 return (
   <Input
     size="small"
     disabled={disabled}
     allowClear
     style={{ maxWidth: 380 }}
      placeholder={q.placeholder || q.assume || '留空则采用默认值'}
      value={typeof value === 'string' ? value : ''}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

export default function RequirementsForm({ step, onReply, onCancel }: RequirementsFormProps) {
  const pending = !step.resolved_by
  const questions = useMemo(() => step.questions || [], [step.questions])
  const [answers, setAnswers] = useState<Answers>({})
  const [sent, setSent] = useState(false)

  const requestId = step.request_id || ''

  // 倒计时：与 AskUserBar 同款本地起算（resume 回放会偏宽松，属可接受误差）
  const deadline = useMemo(
    () => (pending && step.timeout_seconds ? Date.now() + step.timeout_seconds * 1000 : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [step.request_id],
  )
  const [leftSec, setLeftSec] = useState<number | null>(null)
  useEffect(() => {
    if (!deadline) return
    const tick = () => {
      const left = Math.max(0, Math.round((deadline - Date.now()) / 1000))
      setLeftSec(left)
      if (left <= 0) clearInterval(timer)
    }
    const timer = setInterval(tick, 1000)
    tick()
    return () => clearInterval(timer)
  }, [deadline])

  const filledCount = Object.values(answers).filter((v) =>
    Array.isArray(v) ? v.length > 0 : String(v ?? '').trim() !== '',
  ).length

  const submit = (payload: Answers) => {
    if (!pending || sent) return
    setSent(true)
    onReply?.(requestId, JSON.stringify(payload))
  }

  const skip = () => {
    if (!pending || sent) return
    setSent(true)
    onCancel?.(requestId)
  }

  // 已解决：折叠成一行摘要，不占输入区空间（与决策卡的处理一致）
  if (!pending) {
    const s = summarize(step.answer, questions)
    const tail =
      step.resolved_by === 'timeout'
        ? '超时未填，已全部采用默认值'
        : step.resolved_by === 'cancel'
          ? '已跳过，已全部采用默认值'
          : step.resolved_by === 'interrupt'
            ? '任务已停止'
            : s
              ? `已提交 ${s.filled} / ${s.total} 项${s.total - s.filled > 0 ? `，其余 ${s.total - s.filled} 项用默认值` : ''}`
              : '已提交'
    return (
      <div className="askuser-bar intake-bar resolved">
        <div className="askuser-bar-head">
          <FormOutlined />
          <span className="askuser-bar-title">需求采集</span>
          <span className="intake-resolved-tail">{tail}</span>
        </div>
      </div>
    )
  }

  return (
    <div className="askuser-bar intake-bar">
      <div className="askuser-bar-head">
        <FormOutlined />
        <span className="askuser-bar-title">开工前需要确认几项</span>
        {step.question && <span className="askuser-bar-question">{step.question}</span>}
        {leftSec != null && (
          <span className="askuser-bar-countdown">
            <ClockCircleOutlined /> {fmtLeft(leftSec)} 后未提交将全部采用默认值
          </span>
        )}
      </div>

      <div className="intake-scroll">
        <div className="intake-grid">
          {questions.map((q) => (
            <div className="intake-row" key={q.id}>
              <div className="intake-label">
                <Tooltip
                  title={q.assume ? `默认值：${q.assume}` : '未提供默认值'}
                  placement="topLeft"
                >
                  <span className="intake-label-text">
                    {q.label}
                    {q.required && <em className="intake-req">*</em>}
                  </span>
                </Tooltip>
              </div>
              <div className="intake-control">
                <QuestionControl
                  q={q}
                  disabled={sent}
                  value={answers[q.id]}
                  onChange={(v) => setAnswers((prev) => ({ ...prev, [q.id]: v }))}
                />
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="intake-actions">
        <span className="intake-filled">{filledCount > 0 ? `已填 ${filledCount} 项` : ''}</span>
        <Tooltip title="全部题目采用模型给出的默认值，直接开工" placement="top">
          <Button size="small" icon={<ThunderboltOutlined />} disabled={sent} onClick={() => submit({})}>
            全部用默认
          </Button>
        </Tooltip>
        <Button
          size="small"
          type="primary"
          icon={<CheckOutlined />}
          disabled={sent}
          onClick={() => submit(answers)}
        >
          提交
        </Button>
        <Button size="small" type="text" className="askuser-bar-skip" disabled={sent} onClick={skip}>
          跳过
        </Button>
      </div>
    </div>
  )
}
