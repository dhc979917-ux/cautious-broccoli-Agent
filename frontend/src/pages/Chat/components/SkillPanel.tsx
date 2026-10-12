import { useState } from "react"
import { Input } from "antd"
import {
  BulbOutlined,
  DownOutlined,
  RightOutlined,
  SearchOutlined,
  CheckOutlined,
  CloseOutlined,
} from "@ant-design/icons"
import { SkillInfo } from "../../../api/skill"
import { fuzzyScore, highlightMatch } from "../utils"

// 技能选择内容组件：渲染在聊天页右侧抽屉内（原右上角浮动面板已移除）。
// 分组/搜索/勾选逻辑与旧浮动版一致。

interface SkillPanelProps {
  skills: SkillInfo[]
  selectedSkills: string[]
  onToggle: (name: string) => void
  // 批量选择（包级全选/全不选）：一次调用整体生效，避免逐个 toggle 的状态覆盖
  onBatchToggle?: (names: string[], select: boolean) => void
}

interface PkgGroup {
  pkgId: number
  pkgName: string
  displayName: string
  items: SkillInfo[]
}

type Row = { kind: "skill"; skill: SkillInfo } | { kind: "pkg"; pkg: PkgGroup }

// 搜索结果：扁平化、带得分、可排序
type SearchResult =
  | { kind: "skill"; skill: SkillInfo; score: number; pkgName?: string }
  | { kind: "pkg"; pkg: PkgGroup; score: number }

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

function skillLabel(s: SkillInfo): string {
  return s.display_name_zh || s.display_name || s.name
}

export default function SkillPanel({ skills, selectedSkills, onToggle, onBatchToggle }: SkillPanelProps) {
  const [openPkgs, setOpenPkgs] = useState<number[]>([])
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [source, setSource] = useState("system")
  const q = query.trim()

  // 分组：同 package_id 的技能折叠为一个包行，独立技能保持单行
  const rows: Row[] = []
  const pkgMap = new Map<number, PkgGroup>()
  for (const s of skills.filter(s => (s.source_group || "system") === source)) {
    if (s.package_id) {
      let g = pkgMap.get(s.package_id)
      if (!g) {
        g = {
          pkgId: s.package_id,
          pkgName: s.package_name || "",
          displayName: s.package_display_name || s.package_name || "",
          items: [],
        }
        pkgMap.set(s.package_id, g)
        rows.push({ kind: "pkg", pkg: g })
      }
      g.items.push(s)
    } else {
      rows.push({ kind: "skill", skill: s })
    }
  }

  // 包级全选/全不选：只切换需要变化的成员（复用单技能 toggle 协议）
  const togglePkg = (pkg: PkgGroup) => {
    const allSelected = pkg.items.every((i) => selectedSkills.includes(i.name))
    const names = pkg.items.map((i) => i.name)
    if (onBatchToggle) {
      onBatchToggle(names, !allSelected)
      return
    }
    pkg.items.forEach((i) => {
      if (selectedSkills.includes(i.name) === allSelected) onToggle(i.name)
    })
  }

  const toggleOpen = (pkgId: number) => {
    setOpenPkgs((prev) => (prev.includes(pkgId) ? prev.filter((x) => x !== pkgId) : [...prev, pkgId]))
  }

  // 搜索结果：包名命中则整包置顶展开；成员命中则只显示命中成员（带所属包名上下文）
  let results: SearchResult[] = []
  if (q) {
    for (const row of rows) {
      if (row.kind === "skill") {
        const sc = fuzzyScore(q, skillLabel(row.skill))
        if (sc > 0) results.push({ kind: "skill", skill: row.skill, score: sc })
      } else {
        const pkg = row.pkg
        const pkgScore = fuzzyScore(q, pkg.displayName)
        const memberScores = pkg.items.map((s) => ({ s, sc: fuzzyScore(q, skillLabel(s)) }))
        const maxMember = memberScores.reduce((m, x) => Math.max(m, x.sc), 0)
        if (pkgScore > 0 || maxMember > 0) {
          if (pkgScore >= maxMember && pkgScore > 0) {
            results.push({ kind: "pkg", pkg, score: pkgScore })
          } else {
            for (const ms of memberScores) {
              if (ms.sc > 0) results.push({ kind: "skill", skill: ms.s, score: ms.sc, pkgName: pkg.displayName })
            }
          }
        }
      }
    }
    results.sort((a, b) => b.score - a.score)
  }

  const header = searchOpen ? (
    <div className="right-panel-header right-panel-search-header">
      <SearchOutlined style={{ color: "var(--color-primary)" }} />
      <Input
        size="small"
        bordered={false}
        autoFocus
        placeholder="搜索技能…"
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
      <BulbOutlined />
      <span className="right-panel-title">技能</span>
      <span className="right-panel-count">{skills.length}</span>
      <span
        className={"search-trigger" + (q ? " active" : "")}
        title="搜索技能"
        onClick={() => setSearchOpen(true)}
      >
        <SearchOutlined />
      </span>
    </div>
  )

  // 单技能行（搜索态下高亮匹配子串，命中成员带所属包名）
  const renderSkill = (s: SkillInfo, indent: boolean, pkgName?: string, key?: string) => {
    const on = selectedSkills.includes(s.name)
    return (
      <div
        key={key || `s-${s.id}`}
        className="nav-item"
        onClick={() => onToggle(s.name)}
        style={{
          paddingLeft: indent ? 24 : undefined,
          background: on ? "var(--color-primary-bg)" : undefined,
          border: on ? "1px solid var(--color-primary-border)" : "1px solid transparent",
        }}
      >
        <Dot on={on} />
        {pkgName && <span className="nav-item-pkg" title={pkgName}>{pkgName}</span>}
        <span className="nav-item-text">
          {q
            ? highlightMatch(q, skillLabel(s)).map((seg, i) =>
                seg.hit ? <mark key={i} className="match-hit">{seg.text}</mark> : <span key={i}>{seg.text}</span>,
              )
            : skillLabel(s)}
        </span>
      </div>
    )
  }

  // 搜索态列表
  const searchList = q ? (
    results.length === 0 ? (
      <div className="panel-empty">无匹配技能</div>
    ) : (
      <div className="right-panel-dots">
        {results.map((r) => {
          if (r.kind === "skill") {
            return renderSkill(r.skill, false, r.pkgName, `rs-${r.skill.id}-${r.pkgName || ""}`)
          }
          const pkg = r.pkg
          const selCount = pkg.items.filter((i) => selectedSkills.includes(i.name)).length
          const allOn = selCount === pkg.items.length
          return (
            <div key={`rp-${pkg.pkgId}`}>
              <div
                className="nav-item"
                onClick={() => togglePkg(pkg)}
                style={{
                  background: allOn ? "var(--color-primary-bg)" : undefined,
                  border: allOn ? "1px solid var(--color-primary-border)" : "1px solid transparent",
                }}
              >
                <Dot on={allOn} />
                <span className="nav-item-text" style={{ fontWeight: 500 }}>
                  {highlightMatch(q, pkg.displayName).map((seg, i) =>
                    seg.hit ? <mark key={i} className="match-hit">{seg.text}</mark> : <span key={i}>{seg.text}</span>,
                  )}
                </span>
              </div>
              {pkg.items.map((s) => renderSkill(s, true))}
            </div>
          )
        })}
      </div>
    )
  ) : null

  // 默认列表（未搜索）
  const defaultList = !q ? (
    skills.length === 0 ? (
      <div className="panel-empty">暂无可用技能</div>
    ) : (
      <div className="right-panel-dots">
        {rows.map((row) => {
          if (row.kind === "skill") {
            return renderSkill(row.skill, false)
          }
          const pkg = row.pkg
          const selCount = pkg.items.filter((i) => selectedSkills.includes(i.name)).length
          const allOn = selCount === pkg.items.length
          const isOpen = openPkgs.includes(pkg.pkgId)
          return (
            <div key={`p-${pkg.pkgId}`}>
              <div
                className="nav-item"
                onClick={() => togglePkg(pkg)}
                style={{
                  background: allOn ? "var(--color-primary-bg)" : undefined,
                  border: allOn ? "1px solid var(--color-primary-border)" : "1px solid transparent",
                }}
              >
                <Dot on={allOn} />
                <span className="nav-item-text" style={{ fontWeight: 500 }}>{pkg.displayName}</span>
                {selCount > 0 && !allOn && (
                  <span style={{ fontSize: 10, color: "var(--text-tertiary)", flexShrink: 0 }}>
                    {selCount}/{pkg.items.length}
                  </span>
                )}
                <span
                  onClick={(e) => { e.stopPropagation(); toggleOpen(pkg.pkgId) }}
                  style={{
                    cursor: "pointer", color: "var(--text-tertiary)", fontSize: 10,
                    marginLeft: "auto", flexShrink: 0,
                    display: "inline-flex", alignItems: "center",
                    // 加大感应区：往左扩一截、往右扩到行尾（吃到 nav-item 的右内边距）
                    padding: "8px 10px 8px 14px", marginRight: -10,
                  }}
                >
                  {isOpen ? <DownOutlined /> : <RightOutlined />}
                </span>
              </div>
              {isOpen && pkg.items.map((s) => renderSkill(s, true))}
            </div>
          )
        })}
      </div>
    )
  ) : null

  return (
    <div className="skill-picker" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {header}
      <div style={{ display: 'flex', gap: 6, padding: '8px 12px', flexWrap: 'wrap' }}>
        {[['system', '系统技能'], ['mine', '我的技能'], ['shared', '分享给我']].map(([key, label]) => <button key={key} type="button" onClick={() => setSource(key)} aria-pressed={source === key} style={{ border: '1px solid var(--border-color)', borderRadius: 6, padding: '5px 9px', color: source === key ? 'var(--color-primary)' : 'var(--text-secondary)', background: source === key ? 'var(--color-primary-bg)' : 'transparent', cursor: 'pointer' }}>{label}</button>)}
      </div>
      <div className="right-panel-content skill-list" style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
        {searchList || defaultList}
      </div>
    </div>
  )
}
