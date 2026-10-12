import { useEffect } from 'react'
import { create } from 'zustand'
import { getAvailableModels } from '../api/model'
import { useAuthStore } from './authStore'

interface ModelDirectoryState {
  loaded: boolean
  models: string[]
  names: Record<string, string>
  defaultModel: string
}

const useDirectoryState = create<ModelDirectoryState>(() => ({ models: [], names: {}, defaultModel: '', loaded: false }))
let pending: Promise<void> | null = null
let generation = 0

export function refreshModelDirectory(): Promise<void> {
  if (pending) return pending
  const token = useAuthStore.getState().token
  if (!token) return Promise.resolve()
  const currentGeneration = generation
  const operation = getAvailableModels().then((result) => {
    if (token !== useAuthStore.getState().token || currentGeneration !== generation) return
    useDirectoryState.setState({ models: result.models, names: result.display_names || {}, defaultModel: result.default, loaded: true })
  }).catch(() => {}).finally(() => { if (pending === operation) pending = null })
  pending = operation
  return operation
}

export function invalidateModelDirectory() {
  generation += 1
  pending = null
  void refreshModelDirectory()
  try { window.localStorage.setItem('model-directory-updated', String(Date.now())) } catch {}
}

export function useModelDirectory() {
  const state = useDirectoryState()
  const token = useAuthStore((auth) => auth.token)
  useEffect(() => {
    void refreshModelDirectory()
    const refresh = () => { void refreshModelDirectory() }
    const storage = (event: StorageEvent) => {
      if (event.key !== 'model-directory-updated') return
      generation += 1; pending = null; refresh()
    }
    window.addEventListener('focus', refresh)
    window.addEventListener('storage', storage)
    return () => { window.removeEventListener('focus', refresh); window.removeEventListener('storage', storage) }
  }, [token])
  return { ...state, modelLabel: (modelId: string) => state.names[modelId] || modelId,
    modelOptions: state.models.map((modelId) => ({ value: modelId, label: state.names[modelId] || modelId })) }
}

useAuthStore.subscribe((state, previous) => {
  if (state.token === previous.token) return
  generation += 1
  pending = null
  useDirectoryState.setState({ models: [], names: {}, defaultModel: '', loaded: false })
  void refreshModelDirectory()
})
