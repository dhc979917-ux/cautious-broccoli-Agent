import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface UserInfo {
  id: number
  username: string
  account?: string
  email?: string
  phone?: string
  avatar_url?: string | null
  agent_avatar_url?: string | null
  role: string
  agent_permissions: string[]
  permissions?: string[]
  must_change_password?: boolean
}

interface AuthState {
  token: string | null
  user: UserInfo | null
  theme: 'light' | 'dark'
  motionEffect: boolean
  selectedAgentId: number | null
  selectedSkills: string[]
  // null = all MCPs bound to the current Agent; number[] = explicit subset (may be empty)
  selectedMcps: number[] | null

  setAuth: (token: string, user: UserInfo) => void
  updateUser: (patch: Partial<UserInfo>) => void
  logout: () => void
  toggleTheme: () => void
  setMotionEffect: (v: boolean) => void
  setSelectedAgentId: (id: number | null) => void
  setSelectedSkills: (skills: string[]) => void
  setSelectedMcps: (mcps: number[] | null) => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      theme: 'light' as const,
      motionEffect: true,
      selectedAgentId: null,
      selectedSkills: [],
      selectedMcps: null,

      setAuth: (token, user) => set({ token, user }),
      updateUser: (patch) => set((state) => ({
        user: state.user ? { ...state.user, ...patch } : state.user
      })),
      logout: () => set({ token: null, user: null }),
      toggleTheme: () => set((state) => ({
        theme: state.theme === 'light' ? 'dark' : 'light'
      })),
      setMotionEffect: (v) => set({ motionEffect: v }),
      setSelectedAgentId: (id) => set({ selectedAgentId: id }),
      setSelectedSkills: (skills) => set({ selectedSkills: skills }),
      setSelectedMcps: (mcps) => set({ selectedMcps: mcps })
    }),
    {
      name: 'auth-storage',
      version: 6,
      migrate: (persistedState: any, version: number) => {
        let state = persistedState || {}
        if (version < 3) {
          state = { ...state, theme: 'dark', selectedAgentId: null }
        }
        if (version < 6) {
          state = { ...state, theme: 'light' }
        }
        if (version < 4 && state.user) {
          state = { ...state, user: { ...state.user, permissions: state.user.permissions || [] } }
        }
        if (version < 5) {
          state = { ...state, motionEffect: state.motionEffect ?? true }
        }
        return state
      }
    }
  )
)
