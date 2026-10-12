import { useEffect, useRef, useState } from "react"
import { Input } from "antd"
import { MenuOutlined, SearchOutlined, CheckOutlined, CloseOutlined } from "@ant-design/icons"
import { ChatMessage } from "../types"
import { fuzzyScore, highlightMatch } from "../utils"

// 消息导航内容组件：渲染在聊天页右侧抽屉内（原右下角浮动面板已移除），功能不变。

interface MessageNavPanelProps {
  messages: ChatMessage[]
}

// 生成预览：未搜索取正文前 20 字符；搜索时取以匹配点为中心的片段并标注前后省略
function makePreview(q: string, content: string): { text: string; start: number } {
  const c = content || ""
  if (!q) return { text: c.substring(0, 20) + (c.length > 20 ? "..." : ""), start: 0 }
  const at = c.toLowerCase().indexOf(q.toLowerCase())
  if (at < 0) return { text: c.substring(0, 20) + (c.length > 20 ? "..." : ""), start: 0 }
  const from = Math.max(0, at - 12)
  const to = Math.min(c.length, at + q.length + 12)
  return { text: c.slice(from, to), start: from }
}

export default function MessageNavPanel({ messages }: MessageNavPanelProps) {
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState("")
  const navScrollRef = useRef<HTMLDivElement>(null)
  const q = query.trim()

  // 新消息到达时保持列表贴底（最新在最后）
  useEffect(() => {
    if (navScrollRef.current) {
      navScrollRef.current.scrollTop = navScrollRef.current.scrollHeight
    }
  }, [messages.length])

  const entries = messages
    .map((msg, forwardIndex) => ({ msg, forwardIndex }))
    .filter((e) => e.msg.role === "user")

  // 搜索：按匹配度降序，同分按最新优先（forwardIndex 大的在前）
  let visible = entries
  if (q) {
    const scored = entries
      .map((e) => ({ ...e, score: fuzzyScore(q, e.msg.content) }))
      .filter((e) => e.score > 0)
    scored.sort((a, b) => b.score - a.score || b.forwardIndex - a.forwardIndex)
    visible = scored
  }

  const jumpTo = (forwardIndex: number) => {
    const element = document.getElementById(`message-${forwardIndex}`)
    if (element) element.scrollIntoView({ behavior: "smooth", block: "center" })
  }

  const header = searchOpen ? (
    <div className="right-panel-header right-panel-search-header">
      <SearchOutlined style={{ color: "var(--color-primary)" }} />
      <Input
        size="small"
        bordered={false}
        autoFocus
        placeholder="搜索消息…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onPressEnter={() => setSearchOpen(false)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setQuery("")
            setSearchOpen(false)
          }
        }}
        style={{ flex: 1, minWidth: 0, background: "transparent", padding: "0 4px" }}
      />
      <span className="search-confirm" title="确认" onClick={() => setSearchOpen(false)}>
        <CheckOutlined />
      </span>
      <span className="search-clear" title="清除" onClick={() => { setQuery(""); setSearchOpen(false) }}>
        <CloseOutlined />
      </span>
    </div>
  ) : (
    <div className="right-panel-header">
      <MenuOutlined style={{ fontSize: 14, color: "var(--color-primary)" }} />
      <span className="right-panel-title">消息导航</span>
      <span className="right-panel-count">{q ? `匹配 ${visible.length}` : `${entries.length} 条`}</span>
      <span
        className={"search-trigger" + (q ? " active" : "")}
        title="搜索消息"
        onClick={() => setSearchOpen(true)}
      >
        <SearchOutlined />
      </span>
    </div>
  )

  return (
    <div className="nav-picker" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {header}
      <div
        ref={navScrollRef}
        className="message-list"
        style={{ flex: 1, overflowY: "auto", padding: "8px", display: "flex", flexDirection: "column", gap: 6 }}
      >
        {visible.length === 0 ? (
          <div className="panel-empty">{q ? "无匹配消息" : "暂无消息"}</div>
        ) : (
          visible.map(({ msg, forwardIndex }) => {
            const { text, start } = makePreview(q, msg.content)
            // 在片段内定位匹配子串的偏移，用于高亮
            let segs: { text: string; hit: boolean }[] = [{ text, hit: false }]
            if (q) {
              const rel = text.toLowerCase().indexOf(q.toLowerCase())
              if (rel >= 0) {
                segs = [
                  { text: text.slice(0, rel), hit: false },
                  { text: text.slice(rel, rel + q.length), hit: true },
                  { text: text.slice(rel + q.length), hit: false },
                ].filter((s) => s.text.length > 0)
              }
            }
            const prefix = start > 0 ? "…" : ""
            return (
              <div
                key={msg.id || forwardIndex}
                className="nav-msg-item"
                onClick={() => jumpTo(forwardIndex)}
              >
                <div className="nav-msg-preview">
                  {prefix}
                  {segs.map((seg, i) =>
                    seg.hit ? <mark key={i} className="match-hit">{seg.text}</mark> : <span key={i}>{seg.text}</span>,
                  )}
                </div>
                <div className="nav-msg-time">
                  {msg.created_at
                    ? new Date(msg.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
                    : ""}
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
