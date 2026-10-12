import {
  ScheduleOutlined,
  LoadingOutlined,
  CheckCircleOutlined,
} from "@ant-design/icons"
import { ChatMessage, OfficialTaskItem } from "../types"

// 计划面板：只渲染官方 Task 工具（TaskCreate/TaskUpdate/TaskList）维护的实时任务列表。
// 自适应行为由框架自带 Task 工具驱动：Agent 在动手前把任务置为 in_progress、做完置为
// completed；后端在每个工具结果后快照 tasks_context 并推送 tasks 事件，前端只负责呈现。
// 不再展示"执行明细"（按轮次分组的工具调用流水）。

interface PlanPanelProps {
  messages: ChatMessage[]
}

const rowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 8,
  padding: "8px 10px",
  borderRadius: 8,
  fontSize: 13,
  lineHeight: 1.5,
  wordBreak: "break-word",
}

const activeRowStyle: React.CSSProperties = {
  ...rowStyle,
  background: "var(--bg-secondary)",
  border: "1px solid var(--color-primary)",
  fontWeight: 600,
}

const doneRowStyle: React.CSSProperties = {
  ...rowStyle,
  color: "var(--text-tertiary)",
}

const pendingRowStyle: React.CSSProperties = {
  ...rowStyle,
  color: "var(--text-secondary)",
}

const taskIcon = (state: OfficialTaskItem["state"]) => {
  if (state === "completed")
    return <CheckCircleOutlined style={{ color: "var(--color-primary)", flexShrink: 0, marginTop: 2 }} />
  if (state === "in_progress")
    return <LoadingOutlined spin style={{ color: "var(--color-primary)", flexShrink: 0, marginTop: 2 }} />
  return (
    <span
      style={{
        width: 14,
        height: 14,
        borderRadius: "50%",
        border: "1.5px solid var(--text-tertiary)",
        display: "inline-block",
        boxSizing: "border-box",
        flexShrink: 0,
        marginTop: 2,
      }}
    />
  )
}

// 会话内最新的任务列表快照（后端每次任务变化都覆盖更新，取最后一条 tasks step）
function latestTasks(messages: ChatMessage[]): OfficialTaskItem[] | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const steps = messages[i].steps || []
    for (let j = steps.length - 1; j >= 0; j--) {
      const st = steps[j]
      if (st.type === "tasks" && st.tasks && st.tasks.length > 0) {
        return st.tasks
      }
    }
  }
  return null
}

export default function PlanPanel({ messages }: PlanPanelProps) {
  const tasks = latestTasks(messages)

  if (tasks && tasks.length > 0) {
    const activeIndex = tasks.findIndex((t) => t.state === "in_progress")
    return (
      <div className="plan-picker" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
        <div style={{ flex: 1, overflowY: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 6 }}>
          {tasks.map((t, i) => {
            const style =
              t.state === "completed"
                ? doneRowStyle
                : t.state === "in_progress"
                ? activeRowStyle
                : pendingRowStyle
            return (
              <div key={t.id || i} style={style} title={t.description || undefined}>
                {taskIcon(t.state)}
                <span
                  style={{
                    textDecoration: t.state === "completed" ? "line-through" : "none",
                    flex: 1,
                  }}
                >
                  {t.subject}
                </span>
              </div>
            )
          })}
          {activeIndex < 0 && (
            <div style={{ ...pendingRowStyle, justifyContent: "center", color: "var(--text-tertiary)" }}>
              <ScheduleOutlined style={{ opacity: 0.4 }} />
              <span style={{ fontSize: 12 }}>当前无进行中任务</span>
            </div>
          )}
        </div>
      </div>
    )
  }

  // 空态：Agent 还没建任务（简单问答不建任务属正常）
  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        color: "var(--text-tertiary)",
        padding: 24,
        textAlign: "center",
        fontSize: 13,
      }}
    >
      <ScheduleOutlined style={{ fontSize: 36, opacity: 0.35 }} />
      <div>暂无任务</div>
      <div style={{ fontSize: 12 }}>复杂任务会自动拆解为任务列表；当前任务会高亮，做完自动推进到下一个</div>
    </div>
  )
}