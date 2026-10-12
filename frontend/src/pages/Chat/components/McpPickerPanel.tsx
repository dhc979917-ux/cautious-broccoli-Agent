import { useState } from "react"
import { Input } from "antd"
import { ApiOutlined, SearchOutlined, CheckOutlined, CloseOutlined } from "@ant-design/icons"
import { McpServerInfo } from "../../../api/mcp"
import { fuzzyScore, highlightMatch } from "../utils"

// MCP 选择内容组件：渲染在聊天页右侧抽屉内。
// 可选范围 = 当前 Agent 已绑定且启用的 MCP 服务；选择结果随下一条消息发给后端。

interface McpPickerPanelProps {
  servers: McpServerInfo[]        // 已过滤：Agent 绑定 ∩ enabled
  selectedMcps: number[] | null   // null = 全部绑定项启用
  onChange: (mcps: number[] | null) => void
}

const Dot = ({ on }: { on: boolean }) => (
  <span
    style={{
      width: 6,
      height: 6,
      borderRadius: "50%",
      background: on ? "var(--color-primary)" : "var(--text-tertiary)",
      flexShrink: 0,
    }}
  />
)

function serverLabel(s: McpServerInfo): string {
  return `${s.name} ${s.description || ""}`
}

export default function McpPickerPanel({ servers, selectedMcps, onChange }: McpPickerPanelProps) {
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState("")
  const q = query.trim()

  const allIds = servers.map((s) => s.id)
  const effective = selectedMcps === null ? allIds : selectedMcps.filter((id) => allIds.includes(id))

  const toggle = (id: number) => {
    const next = effective.includes(id)
      ? effective.filter((x) => x !== id)
      : [...effective, id]
    // 全选等价于 null（后端语义：不传 = Agent 绑定的全部 MCP）
    onChange(next.length === allIds.length ? null : next)
  }

  let results: { server: McpServerInfo; score: number }[] = []
  if (q) {
    results = servers
      .map((s) => ({ server: s, score: fuzzyScore(q, serverLabel(s)) }))
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score)
  }

  const header = searchOpen ? (
    <div className="right-panel-header right-panel-search-header">
      <SearchOutlined style={{ color: "var(--color-primary)" }} />
      <Input
        size="small"
        bordered={false}
        autoFocus
        placeholder="搜索 MCP 服务…"
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
      <ApiOutlined />
      <span className="right-panel-title">MCP 服务</span>
      <span className="right-panel-count">
        {servers.length > 0 ? `${effective.length}/${servers.length}` : 0}
      </span>
      <span
        className={"search-trigger" + (q ? " active" : "")}
        title="搜索 MCP 服务"
        onClick={() => setSearchOpen(true)}
      >
        <SearchOutlined />
      </span>
    </div>
  )

  const renderServer = (s: McpServerInfo) => {
    const on = effective.includes(s.id)
    return (
      <div
        key={`m-${s.id}`}
        className="nav-item"
        onClick={() => toggle(s.id)}
        title={s.description || s.name}
        style={{
          background: on ? "var(--color-primary-bg)" : undefined,
          border: on ? "1px solid var(--color-primary-border)" : "1px solid transparent",
        }}
      >
        <Dot on={on} />
        <span className="nav-item-text">
          {q
            ? highlightMatch(q, s.name).map((seg, i) =>
                seg.hit ? <mark key={i} className="match-hit">{seg.text}</mark> : <span key={i}>{seg.text}</span>,
              )
            : s.name}
        </span>
        <span style={{ fontSize: 10, color: "var(--text-tertiary)", flexShrink: 0 }}>
          {s.type}
        </span>
      </div>
    )
  }

  const list = q ? (
    results.length === 0 ? (
      <div className="panel-empty">无匹配 MCP 服务</div>
    ) : (
      <div className="right-panel-dots">{results.map((r) => renderServer(r.server))}</div>
    )
  ) : servers.length === 0 ? (
    <div className="panel-empty">当前 Agent 未绑定 MCP 服务</div>
  ) : (
    <div className="right-panel-dots">{servers.map(renderServer)}</div>
  )

  return (
    <div className="mcp-picker" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {header}
      <div className="right-panel-content" style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
        {list}
      </div>
    </div>
  )
}
