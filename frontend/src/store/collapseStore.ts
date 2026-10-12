import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface CollapseState {
  // key -> 展开(true)/收起(false)。只记录用户手动切换过的项；
  // 没有记录的项走各自默认值（总开关/思考段默认展开，工具输出默认收起）
  states: Record<string, boolean>
  setExpanded: (key: string, expanded: boolean) => void
}

// key 命名约定（消息用落库后的 DB id，保证刷新/切会话后仍能对上）：
//   steps:{msgId}          消息级"推理过程"总开关
//   think:{msgId}:{idx}    第 idx 段思考
//   tool:{msgId}:{idx}     第 idx 个工具调用的输出
// localStorage 容量保护：超过上限时按插入顺序删最旧的
const MAX_ENTRIES = 2000

export const useCollapseStore = create<CollapseState>()(
  persist(
    (set) => ({
      states: {},
      setExpanded: (key, expanded) =>
        set((state) => {
          const next = { ...state.states, [key]: expanded }
          const keys = Object.keys(next)
          while (keys.length > MAX_ENTRIES) {
            const oldest = keys.shift()
            if (oldest === undefined) break
            delete next[oldest]
          }
          return { states: next }
        }),
    }),
    { name: 'chat-collapse', version: 1 }
  )
)
