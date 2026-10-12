import { useMemo } from 'react'
import { TreeSelect } from 'antd'
import type { SkillGrantOptions } from '../api/admin'

export interface SkillGrantValue {
  skill: number[]
  package: number[]
}

export default function SkillGrantSelect({ options, value = { skill: [], package: [] }, onChange, disabled }: {
  options: SkillGrantOptions | null
  value?: SkillGrantValue
  onChange?: (value: SkillGrantValue) => void
  disabled?: boolean
}) {
  const tree = useMemo(() => {
    const skills = options?.skills || []
    const packages = options?.packages || []
    return [
      ...packages.map((pkg) => ({ title: pkg.label, value: `package:${pkg.id}`, key: `package:${pkg.id}`,
        children: skills.filter((skill) => skill.package_id === pkg.id).map((skill) => ({
          title: skill.label, value: `skill:${skill.id}`, key: `skill:${skill.id}`,
        })) })),
      ...skills.filter((skill) => !packages.some((pkg) => pkg.id === skill.package_id)).map((skill) => ({
        title: skill.label, value: `skill:${skill.id}`, key: `skill:${skill.id}`,
      })),
    ]
  }, [options])
  const selected = [...value.skill.map((id) => `skill:${id}`), ...value.package.map((id) => `package:${id}`)]
  return <TreeSelect treeData={tree} value={selected} disabled={disabled} treeCheckable allowClear
    showCheckedStrategy={TreeSelect.SHOW_PARENT} showSearch treeNodeFilterProp="title"
    placeholder="选择技能；技能包可整包勾选或展开逐项授权" style={{ width: '100%' }}
    onChange={(keys: string[]) => {
      const checked = new Set(keys)
      const packageIds = tree.filter((node) => node.value.startsWith('package:') && checked.has(node.value)).map((node) => Number(node.value.split(':')[1]))
      const skillIds = (options?.skills || []).filter((skill) => checked.has(`skill:${skill.id}`) && !packageIds.includes(skill.package_id || 0)).map((skill) => skill.id)
      onChange?.({ skill: skillIds, package: packageIds })
    }} />
}
