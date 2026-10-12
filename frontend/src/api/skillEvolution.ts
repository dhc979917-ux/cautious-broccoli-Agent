import request from './request'

const base = '/skill-evolution'
export interface EvolutionPreference { enabled: boolean; paused_resources: string[] }
export interface EvolutionJob {
  id: string
  session_id: number
  status: string
  decision?: string
  reason: string
  target_resource_id?: string
  candidate_version_id?: string
  report: { risks?: string[]; switched_session?: boolean; choices?: { id: string; title: string }[]; [key: string]: unknown }
  attempts: number
  created_at: string
}
export interface EvolutionBinding { source: string; target: string }
export const evolutionPreference = () => request.get<EvolutionPreference>(`${base}/preferences`).then(r => r.data)
export const setEvolutionEnabled = (enabled: boolean) => request.put<EvolutionPreference>(`${base}/preferences`, { enabled }).then(r => r.data)
export const evolutionJobs = (resource_id?: string, session_id?: number) => request.get<EvolutionJob[]>(`${base}/jobs`, { params: { resource_id, session_id } }).then(r => r.data)
export const evolutionBindings = (sessionId: number) => request.get<EvolutionBinding[]>(`${base}/sessions/${sessionId}/bindings`).then(r => r.data)
export const evolutionAction = (id: string, action: 'confirm' | 'reject' | 'retry' | 'cancel') => request.post(`${base}/jobs/${id}/${action}`, action === 'confirm' ? { acknowledge_risk: true } : {}).then(r => r.data)
export const evolutionChooseTarget = (id: string, resource_id: string) => request.post(`${base}/jobs/${id}/target`, { resource_id })
export const evolutionDiff = (id: string) => request.get<{ path: string; diff: string }[]>(`${base}/jobs/${id}/diff`).then(r => r.data)
export const evolutionPause = (id: string, paused: boolean) => request.put<EvolutionPreference>(`${base}/resources/${id}/pause`, { paused }).then(r => r.data)
