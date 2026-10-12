import { useState } from 'react'
import { Button, Checkbox, Popover } from 'antd'
import { DownOutlined, RightOutlined } from '@ant-design/icons'
import type { SkillInfo } from '../api/skill'

interface SkillMultiSelectProps {
  skills: SkillInfo[]
  value?: string[]
  onChange?: (names: string[]) => void
  disabled?: boolean
}

/**
 * 技能多选器（新建 Agent 时选择附加内置技能）。
 * 交互与聊天页技能面板一致：技能包一行点击下拉展开/收起，
 * 单个技能点击即勾选/取消；包前的复选框用于整包全选/全不选。
 */
export default function SkillMultiSelect({ skills, value = [], onChange = () => {}, disabled = false }: SkillMultiSelectProps) {
  const [openPkgs, setOpenPkgs] = useState<number[]>([])

  // 分组：同 package_id 的技能归为一包，其余为独立技能
  const pkgMap = new Map<number, SkillInfo[]>()
  const standalone: SkillInfo[] = []
  for (const s of skills) {
    if (s.package_id) {
      const arr = pkgMap.get(s.package_id) || []
      arr.push(s)
      pkgMap.set(s.package_id, arr)
    } else {
      standalone.push(s)
    }
  }
  const packageGroups = Array.from(pkgMap.entries())

  const toggleOne = (name: string) => {
    onChange(value.includes(name) ? value.filter((n) => n !== name) : [...value, name])
  }

  const togglePkg = (items: SkillInfo[]) => {
    const names = items.map((i) => i.name)
    const allIn = names.every((n) => value.includes(n))
    if (allIn) {
      onChange(value.filter((n) => !names.includes(n)))
    } else {
      onChange(Array.from(new Set([...value, ...names])))
    }
  }

  const toggleOpen = (pkgId: number) => {
    setOpenPkgs((prev) => prev.includes(pkgId) ? prev.filter((x) => x !== pkgId) : [...prev, pkgId])
  }

  const displayName = (s: SkillInfo) => s.display_name_zh || s.display_name || s.name

  const rowStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 6,
    padding: '4px 6px', borderRadius: 6, cursor: 'pointer',
  }

  const content = (
    <div style={{ width: 320, maxHeight: 360, overflowY: 'auto' }}>
      {packageGroups.map(([pkgId, items]) => {
        const names = items.map((i) => i.name)
        const selCount = names.filter((n) => value.includes(n)).length
        const allIn = selCount === items.length
        const isOpen = openPkgs.includes(pkgId)
        return (
          <div key={`pkg-${pkgId}`}>
            {/* 包行：点击展开/收起；复选框整包全选/全不选 */}
            <div
              style={{ ...rowStyle, background: allIn ? 'var(--color-primary-bg)' : undefined }}
              onClick={() => toggleOpen(pkgId)}
              onMouseEnter={(e) => { if (!allIn) e.currentTarget.style.background = 'var(--bg-hover)' }}
              onMouseLeave={(e) => { if (!allIn) e.currentTarget.style.background = '' }}
            >
              <Checkbox
                checked={allIn}
                indeterminate={selCount > 0 && !allIn}
                onClick={(e) => { e.stopPropagation(); togglePkg(items) }}
              />
              <span style={{ flex: 1, fontWeight: 500, fontSize: 13 }}>
                {items[0].package_display_name || items[0].package_name}
                <span style={{ color: 'var(--text-tertiary)', fontSize: 12, marginLeft: 6 }}>
                  {items.length} 个技能
                </span>
              </span>
              {selCount > 0 && !allIn && (
                <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{selCount}/{items.length}</span>
              )}
              <span style={{ color: 'var(--text-tertiary)', fontSize: 10, padding: '0 2px' }}>
                {isOpen ? <DownOutlined /> : <RightOutlined />}
              </span>
            </div>
            {isOpen && items.map((s) => {
              const on = value.includes(s.name)
              return (
                <div
                  key={s.id}
                  style={{ ...rowStyle, padding: '3px 6px 3px 28px', background: on ? 'var(--color-primary-bg)' : undefined }}
                  onClick={() => toggleOne(s.name)}
                  onMouseEnter={(e) => { if (!on) e.currentTarget.style.background = 'var(--bg-hover)' }}
                  onMouseLeave={(e) => { if (!on) e.currentTarget.style.background = '' }}
                >
                  <Checkbox checked={on} style={{ pointerEvents: 'none' }} />
                  <span style={{ fontSize: 13 }}>{displayName(s)}</span>
                </div>
              )
            })}
          </div>
        )
      })}
      {standalone.map((s) => {
        const on = value.includes(s.name)
        return (
          <div
            key={s.id}
            style={{ ...rowStyle, background: on ? 'var(--color-primary-bg)' : undefined }}
            onClick={() => toggleOne(s.name)}
            onMouseEnter={(e) => { if (!on) e.currentTarget.style.background = 'var(--bg-hover)' }}
            onMouseLeave={(e) => { if (!on) e.currentTarget.style.background = '' }}
          >
            <Checkbox checked={on} style={{ pointerEvents: 'none' }} />
            <span style={{ fontSize: 13 }}>{displayName(s)}</span>
          </div>
        )
      })}
      {skills.length === 0 && (
        <div style={{ color: 'var(--text-tertiary)', padding: 12, textAlign: 'center' }}>暂无可选技能</div>
      )}
    </div>
  )

  return (
    <Popover content={content} trigger={disabled ? [] : 'click'} placement="bottomLeft">
      <Button disabled={disabled} style={{ width: '100%', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>{value.length > 0 ? `已选 ${value.length} 个技能` : '选择内置技能（可不选）'}</span>
        <DownOutlined style={{ fontSize: 10, color: 'var(--text-tertiary)' }} />
      </Button>
    </Popover>
  )
}
