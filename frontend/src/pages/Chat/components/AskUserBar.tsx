// ask_user 决策条：紧贴在输入框上方（与输入框等宽），用户点选/自定义后
// 答案经 WebSocket 回传，Agent 继续推理。已解决的决策在思考块内只留"问/答"两行。
import { useEffect, useMemo, useState } from 'react'
import { Button, Input, Tooltip, message } from 'antd'
import {
  QuestionCircleOutlined,
  ClockCircleOutlined,
  EditOutlined,
  MinusCircleOutlined,
} from '@ant-design/icons'
import { MessageStep } from '../types'

interface AskUserBarProps {
  step: MessageStep
  onReply?: (requestId: string, answerText: string) => void
  onCancel?: (requestId: string) => void
}

function fmtLeft(sec: number) {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

// 自定义类选项（模型偶尔生成的"自定义/其他"占位项）：点击不直接发送，
// 原地切换为内联输入框，让用户填写真实内容
const CUSTOM_OPTION_RE = /自定义|其他|其它|手动填写|自行填写/

export default function AskUserBar({ step, onReply, onCancel }: AskUserBarProps) {
  const pending = !step.resolved_by
  const [showCustom, setShowCustom] = useState(false)
  const [customText, setCustomText] = useState('')
  // 触发内联输入的选项 label（内置"其他"入口为 null）；提交时作为答案前缀
  const [customFrom, setCustomFrom] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null) // 乐观锁定，防重复点击

  // 倒计时：以本地收到时刻起算（resume 回放时会偏宽松，属可接受误差）
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

  const requestId = step.request_id || ''
  const options = step.options || []

  const handlePick = (label: string) => {
    if (!pending || picked) return
    if (CUSTOM_OPTION_RE.test(label)) {
      setCustomFrom(label)
      setCustomText('')
      setShowCustom(true)
      return
    }
    setPicked(label)
    onReply?.(requestId, label)
  }

  const handleCustomSubmit = () => {
    const text = customText.trim()
    if (!text) {
      message.warning('请输入你的方案')
      return
    }
    if (!pending || picked) return
    const answer = customFrom ? `${customFrom}：${text}` : text
    setPicked(answer)
    onReply?.(requestId, answer)
  }

  const handleSkip = () => {
    if (!pending || picked) return
    setPicked('__skip__')
    onCancel?.(requestId)
  }

  return (
    <div className="askuser-bar">
      <div className="askuser-bar-head">
        <QuestionCircleOutlined />
        <span className="askuser-bar-title">需要你的决策</span>
        {step.question && <span className="askuser-bar-question">{step.question}</span>}
        {pending && leftSec != null && (
          <span className="askuser-bar-countdown">
            <ClockCircleOutlined /> {fmtLeft(leftSec)} 后未响应将自动选择最优方案
          </span>
        )}
      </div>
      <div className="askuser-bar-opts">
        {options.map((opt) => (
          <Tooltip key={opt.key} title={opt.description || undefined} placement="top">
            <Button
              size="small"
              className={`askuser-bar-opt ${picked === opt.label ? 'picked' : ''}`}
              disabled={!!picked}
              onClick={() => handlePick(opt.label)}
            >
              <span className="askuser-bar-key">{opt.key}</span>
              {opt.label}
            </Button>
          </Tooltip>
        ))}
        {showCustom ? (
          <span className="askuser-bar-custom">
            <Input
              size="small"
              value={customText}
              onChange={(e) => setCustomText(e.target.value)}
              placeholder="写下你的方案，回车确认"
              style={{ width: 260 }}
              autoFocus
              disabled={!!picked}
              onPressEnter={handleCustomSubmit}
            />
            <Button size="small" type="primary" disabled={!!picked} onClick={handleCustomSubmit}>
              确认
            </Button>
            <Button size="small" type="text" disabled={!!picked} onClick={() => setShowCustom(false)}>
              返回
            </Button>
          </span>
        ) : (
          !picked && (
            <Button size="small" type="text" icon={<EditOutlined />} onClick={() => { setCustomFrom(null); setShowCustom(true) }}>
              其他
            </Button>
          )
        )}
        {!picked && (
          <Button size="small" type="text" className="askuser-bar-skip" icon={<MinusCircleOutlined />} onClick={handleSkip}>
            跳过，让 AI 自行决定
          </Button>
        )}
      </div>
    </div>
  )
}
