import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface DraftFile {
  file_path: string
  stored_name: string
  original_name: string
  size?: number
}

// 草稿里保存的排队（延时发送）项：任务运行中点发送挂起的内容，
// 刷新/重进会话后据此恢复待发送区
export interface DraftPendingItem {
  content: string
  files: DraftFile[]
}

export interface SessionDraft {
  text: string
  files: DraftFile[]
  model: string
  pending?: DraftPendingItem[]
  updatedAt: number
}

interface DraftState {
  drafts: Record<string, SessionDraft>
  saveDraft: (sessionId: number, draft: Omit<SessionDraft, 'updatedAt'>) => void
  clearDraft: (sessionId: number) => void
}

// 每个会话的输入草稿独立保存：文本 / 待发送附件 / 所选模型。
// 持久化到 localStorage：切换会话、切去别的页面再回来都不丢；发送成功后清除。
export const useDraftStore = create<DraftState>()(
  persist(
    (set) => ({
      drafts: {},
      saveDraft: (sessionId, draft) =>
        set((state) => ({
          drafts: {
            ...state.drafts,
            [String(sessionId)]: { ...draft, updatedAt: Date.now() },
          },
        })),
      clearDraft: (sessionId) =>
        set((state) => {
          if (!(String(sessionId) in state.drafts)) return state
          const next = { ...state.drafts }
          delete next[String(sessionId)]
          return { drafts: next }
        }),
    }),
    { name: 'chat-drafts', version: 1 }
  )
)
